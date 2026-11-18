"""Backend adapter for the container scheduler. Agent execution stays local."""

import logging
from dataclasses import asdict
from hashlib import sha256

import httpx

from ..core.models import CronJob
from ..services.scheduler import Scheduler


logger = logging.getLogger(__name__)


class CronUnavailable(OSError):
    pass


class CronClient:
    def __init__(self, url, data_dir):
        self.url = url.rstrip("/") + "/cron"
        self.scope = sha256(str(data_dir).encode()).hexdigest()[:32]
        self.client = httpx.Client(timeout=2, trust_env=False, headers={"X-Agent-Harness": "cron"})

    def call(self, action, session_id=None, **fields):
        try:
            result = self.client.post(
                self.url,
                json={"scope": self.scope, "action": action, "session_id": session_id, **fields},
            )
        except httpx.HTTPError as error:
            raise CronUnavailable("Cron service is unavailable") from error
        if result.status_code == 422:
            raise ValueError(result.json().get("error", "Invalid scheduled task"))
        if result.status_code != 200:
            raise CronUnavailable("Cron service could not complete the operation")
        try:
            return result.json()["result"]
        except (KeyError, TypeError, ValueError) as error:
            raise CronUnavailable("Cron service returned an invalid response") from error

    def close(self):
        self.client.close()


class RemoteScheduler(Scheduler):
    managed_externally = True

    def __init__(self, state_dir, stop_event, client, session_id):
        super().__init__(state_dir, stop_event)
        self.client = client
        self.session_id = session_id
        self.tokens = {}

    def _call(self, action, **fields):
        return self.client.call(action, self.session_id, **fields)

    def start_runtime_services(self):
        # The container owns detection. Only the shared backend pump stages deliveries.
        pass

    def list_jobs(self):
        return self._call("list")

    def describe_jobs(self, now, job_id=None):
        return [job for job in self.list_jobs() if job_id is None or job["id"] == job_id]

    def schedule_job(self, cron, prompt, recurring=True, durable=True):
        error = self.validate_cron(cron)
        if error:
            return error
        if self.DURABLE_PATH.is_file() and self.DURABLE_PATH.resolve() == self.DURABLE_PATH:
            self._call(
                "migrate", active=True, jobs=[asdict(job) for job in self.read_durable_jobs()]
            )
        return CronJob(
            **self._call("create", cron=cron, prompt=prompt, recurring=recurring, durable=durable)
        )

    def remove_job(self, job_id):
        removed = self._call("remove", job_id=job_id)
        if removed:
            with self.cron_lock:
                self.cron_queue[:] = [job for job in self.cron_queue if job.id != job_id]
                self.tokens.pop(job_id, None)
        return removed

    def due_generation(self):
        return self.client.call("generation")

    def stage_due(self):
        with self.cron_lock:
            if self.cron_queue:
                return
            row = self._call("claim")
            if row is not None:
                job = CronJob(
                    row["job_id"],
                    row["cron"],
                    row["prompt"],
                    bool(row["recurring"]),
                    bool(row["durable"]),
                    True,
                )
                self.tokens[job.id] = row["token"]
                self.cron_queue.append(job)

    def _settle(self, jobs, action):
        with self.cron_lock:
            for job in jobs:
                token = self.tokens.pop(job.id, None)
                if token:
                    try:
                        self._call(action, token=token)
                    except OSError:
                        logger.warning(
                            "Cron delivery confirmation failed; the lease allows recovery"
                        )

    def acknowledge_cron_jobs(self, jobs):
        self._settle(jobs, "ack")

    def restore_cron_jobs(self, jobs):
        self._settle(jobs, "release")
