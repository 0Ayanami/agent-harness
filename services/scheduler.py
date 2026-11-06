import json
import os
import random
import threading
from dataclasses import asdict, replace
from datetime import datetime, timedelta

from ..core.models import CronJob


class Scheduler:
    def __init__(self, state_dir, stop_event):
        self.state_dir = state_dir
        self.stop_event = stop_event
        self._init_scheduler()

    def _init_scheduler(self):
        self.DURABLE_PATH = self.state_dir / ".scheduled_tasks.json"
        self.scheduled_jobs: dict[str, CronJob] = {}
        self.cron_queue: list[CronJob] = []
        self.cron_lock = threading.RLock()
        self._last_fired: dict[str, str] = {}
        self._due_generation = 0
        self._runtime_services_started = False
        self._runtime_services_lock = threading.Lock()

    def _cron_field_matches(self, field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return step > 0 and value % step == 0
        if "," in field:
            return any((self._cron_field_matches(part.strip(), value) for part in field.split(",")))
        if "-" in field:
            lo, hi = field.split("-", 1)
            return int(lo) <= value <= int(hi)
        return value == int(field)

    def cron_matches(self, cron_expr: str, dt: datetime) -> bool:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        return (
            self._cron_field_matches(minute, dt.minute)
            and self._cron_field_matches(hour, dt.hour)
            and self._cron_date_matches(dom, month, dow, dt)
        )

    def _cron_date_matches(self, dom, month, dow, dt):
        dow_val = (dt.weekday() + 1) % 7
        dom_ok = self._cron_field_matches(dom, dt.day)
        dow_ok = self._cron_field_matches(dow, dow_val)
        if not self._cron_field_matches(month, dt.month):
            return False
        if dom == "*" and dow == "*":
            return True
        if dom == "*":
            return dow_ok
        if dow == "*":
            return dom_ok
        return dom_ok or dow_ok

    def next_cron_run(self, cron_expr, now, last_fired=None):
        minute, hour, dom, month, dow = cron_expr.split()
        minutes = [value for value in range(60) if self._cron_field_matches(minute, value)]
        hours = [value for value in range(24) if self._cron_field_matches(hour, value)]
        if not minutes or not hours:
            return None
        start = now.replace(second=0, microsecond=0)
        # Gregorian leap days can be eight years apart around a skipped century.
        for offset in range(8 * 366 + 1):
            day = start + timedelta(days=offset)
            if not self._cron_date_matches(dom, month, dow, day):
                continue
            for hour_value in hours:
                for minute_value in minutes:
                    candidate = day.replace(hour=hour_value, minute=minute_value)
                    if candidate < start or candidate.strftime("%Y-%m-%d %H:%M") == last_fired:
                        continue
                    if datetime.fromtimestamp(candidate.timestamp()) == candidate:
                        return max(now, candidate)
        return None

    def _validate_cron_field(self, field: str, lo: int, hi: int) -> str | None:
        if field == "*":
            return None
        if field.startswith("*/"):
            step = field[2:]
            if not (step.isascii() and step.isdecimal()) or int(step) <= 0:
                return f"Invalid step: {field}"
            return None
        if "," in field:
            for part in field.split(","):
                err = self._validate_cron_field(part.strip(), lo, hi)
                if err:
                    return err
            return None
        if "-" in field:
            left, right = field.split("-", 1)
            if not (left.isascii() and left.isdecimal() and right.isascii() and right.isdecimal()):
                return f"Invalid range: {field}"
            a, b = (int(left), int(right))
            if a < lo or a > hi or b < lo or (b > hi):
                return f"Range {field} out of bounds [{lo}-{hi}]"
            if a > b:
                return f"Range start > end: {field}"
            return None
        if not (field.isascii() and field.isdecimal()):
            return f"Invalid field: {field}"
        value = int(field)
        if value < lo or value > hi:
            return f"Value {value} out of bounds [{lo}-{hi}]"
        return None

    def validate_cron(self, cron_expr: str) -> str | None:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return f"Expected 5 fields, got {len(fields)}"
        bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
        for field, (lo, hi), name in zip(fields, bounds, names):
            err = self._validate_cron_field(field, lo, hi)
            if err:
                return f"{name}: {err}"
        return None

    def save_durable_jobs(self):
        with self.cron_lock:
            durable = [asdict(job) for job in self.scheduled_jobs.values() if job.durable]
            temporary = self.DURABLE_PATH.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(durable, indent=2), encoding="utf-8")
            os.replace(temporary, self.DURABLE_PATH)

    def read_durable_jobs(self):
        try:
            items = json.loads(self.DURABLE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        jobs = []
        for item in items if isinstance(items, list) else []:
            try:
                job = CronJob(**item)
                if (
                    job.durable is True
                    and type(job.recurring) is bool
                    and type(job.pending_delivery) is bool
                    and isinstance(job.id, str)
                    and isinstance(job.prompt, str)
                    and not self.validate_cron(job.cron)
                ):
                    jobs.append(job)
            except (AttributeError, TypeError, ValueError):
                continue
        return jobs

    def load_durable_jobs(self):
        with self.cron_lock:
            for job in self.read_durable_jobs():
                self.scheduled_jobs[job.id] = job
                if job.pending_delivery:
                    self.cron_queue.append(job)

    def list_jobs(self):
        with self.cron_lock:
            return [asdict(job) for job in self.scheduled_jobs.values()]

    def describe_jobs(self, now, job_id=None):
        with self.cron_lock:
            queued = {job.id for job in self.cron_queue}
            jobs = [
                (replace(job), self._last_fired.get(job.id))
                for job in self.scheduled_jobs.values()
                if job_id is None or job.id == job_id
            ]
        items = []
        for job, last_fired in jobs:
            waiting = job.pending_delivery or job.id in queued
            next_run = None if waiting else self.next_cron_run(job.cron, now, last_fired)
            items.append(
                {
                    **asdict(job),
                    "status": "queued" if waiting else "scheduled",
                    "next_run_at": next_run.astimezone().isoformat() if next_run else None,
                }
            )
        return items

    def due_generation(self):
        with self.cron_lock:
            return self._due_generation

    def has_new_due_job(self, since):
        with self.cron_lock:
            return self._due_generation > since and bool(self.cron_queue)

    def schedule_job(
        self, cron: str, prompt: str, recurring: bool = True, durable: bool = True
    ) -> CronJob | str:
        err = self.validate_cron(cron)
        if err:
            return err
        with self.cron_lock:
            job_id = f"cron_{random.randint(0, 999999):06d}"
            while job_id in self.scheduled_jobs:
                job_id = f"cron_{random.randint(0, 999999):06d}"
            job = CronJob(job_id, cron, prompt, recurring, durable)
            self.scheduled_jobs[job.id] = job
            try:
                if durable:
                    self.save_durable_jobs()
            except OSError:
                self.scheduled_jobs.pop(job.id)
                raise
        return job

    def remove_job(self, job_id: str) -> bool:
        with self.cron_lock:
            job = self.scheduled_jobs.pop(job_id, None)
            if job is None:
                return False
            try:
                if job.durable:
                    self.save_durable_jobs()
            except OSError:
                self.scheduled_jobs[job.id] = job
                raise
            self.cron_queue[:] = [queued for queued in self.cron_queue if queued.id != job_id]
            self._last_fired.pop(job_id, None)
            return True

    def cancel_job(self, job_id: str) -> str:
        return f"Cancelled {job_id}" if self.remove_job(job_id) else f"Job {job_id} not found"

    def _enqueue_due_job(self, job: CronJob):
        """Persist a one-shot delivery before exposing it through the queue."""
        if not job.recurring:
            job.pending_delivery = True
            try:
                if job.durable:
                    self.save_durable_jobs()
            except Exception:
                job.pending_delivery = False
                raise
        self.cron_queue.append(job)
        self._due_generation += 1

    def cron_scheduler_loop(self):
        while not self.stop_event.wait(1):
            now = datetime.now()
            marker = now.strftime("%Y-%m-%d %H:%M")
            with self.cron_lock:
                for job in list(self.scheduled_jobs.values()):
                    try:
                        if job.pending_delivery:
                            continue
                        if (
                            self.cron_matches(job.cron, now)
                            and self._last_fired.get(job.id) != marker
                        ):
                            self._enqueue_due_job(job)
                            self._last_fired[job.id] = marker
                    except Exception as e:
                        print(f"  \x1b[31m[cron error] {job.id}: {e}\x1b[0m")

    def consume_cron_queue(self) -> list[CronJob]:
        with self.cron_lock:
            fired = list(self.cron_queue)
            self.cron_queue.clear()
        return fired

    def acknowledge_cron_jobs(self, jobs: list[CronJob]):
        """Remove one-shot jobs after a model call accepts their prompts."""
        durable_changed = False
        with self.cron_lock:
            for job in jobs:
                current = self.scheduled_jobs.get(job.id)
                if current and (not current.recurring) and current.pending_delivery:
                    self.scheduled_jobs.pop(job.id, None)
                    durable_changed = durable_changed or current.durable
            if durable_changed:
                self.save_durable_jobs()

    def restore_cron_jobs(self, jobs: list[CronJob]):
        """Put unacknowledged deliveries back after a failed model call."""
        with self.cron_lock:
            queued_ids = {job.id for job in self.cron_queue}
            for job in jobs:
                current = self.scheduled_jobs.get(job.id)
                if current and current.id not in queued_ids:
                    self.cron_queue.append(current)
                    queued_ids.add(current.id)

    def run_schedule_cron(
        self, cron: str, prompt: str, recurring: bool = True, durable: bool = True
    ) -> str:
        result = self.schedule_job(cron, prompt, recurring, durable)
        if isinstance(result, str):
            return f"Error: {result}"
        return f"Scheduled {result.id}: '{cron}' -> {prompt}"

    def run_list_crons(self) -> str:
        jobs = self.list_jobs()
        if not jobs:
            return "No cron jobs."
        return "\n".join(
            (
                f"  {job['id']}: '{job['cron']}' -> {job['prompt'][:40]} [{('recurring' if job['recurring'] else 'one-shot')}, {('durable' if job['durable'] else 'session')}]"
                for job in jobs
            )
        )

    def run_cancel_cron(self, job_id: str) -> str:
        return self.cancel_job(job_id)

    def start_runtime_services(self):
        """Start durable scheduling once when a CLI host becomes active."""
        with self._runtime_services_lock:
            if self._runtime_services_started:
                return
            self.load_durable_jobs()
            self.thread = threading.Thread(target=self.cron_scheduler_loop, daemon=True)
            self.thread.start()
            self._runtime_services_started = True
