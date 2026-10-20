import json
import os
import re
import secrets
import threading
from pathlib import Path
from contextlib import contextmanager
from dataclasses import asdict
from ..core.models import Task
from .platform import lock_file, unlock_file


class TaskRepository:
    def __init__(self, config, directory):
        self.config = config
        self.directory = directory
        self.root = directory.resolve()
        self.id_pattern = re.compile("^task_[0-9a-f]{8}$")
        self.lock_path = directory / ".lock"
        self.lock = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def transaction(self):
        """Serialize task mutations across threads and host processes."""
        with self.lock:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self.directory.mkdir(parents=True, exist_ok=True)
                handle = self.lock_path.open("a+", encoding="utf-8")
                lock_file(handle)
                self._local.handle = handle
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
                if self._local.depth == 0:
                    handle = self._local.handle
                    unlock_file(handle)
                    handle.close()
                    del self._local.handle

    def path_for(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not self.id_pattern.fullmatch(task_id):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        path = (self.directory / f"{task_id}.json").resolve()
        if not self.root.is_relative_to(self.config.workdir.resolve()) or not path.is_relative_to(
            self.root
        ):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        return path

    def create_task(self, subject: str, description: str = "") -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject cannot be empty")
        with self.transaction():
            for _ in range(100):
                task = Task(
                    id=f"task_{secrets.token_hex(4)}",
                    subject=subject,
                    description=description,
                    status="pending",
                    owner=None,
                    blockedBy=[],
                )
                try:
                    with self.path_for(task.id).open("x", encoding="utf-8") as handle:
                        json.dump(asdict(task), handle, indent=2)
                    return task
                except FileExistsError:
                    continue
        raise RuntimeError("Could not allocate a unique task ID")

    def save_task(self, task: Task):
        with self.transaction():
            path = self.path_for(task.id)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary.write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def load_task(self, task_id: str) -> Task:
        with self.lock:
            data = json.loads(self.path_for(task_id).read_text(encoding="utf-8"))
            task = Task(**data)
            if task.id != task_id:
                raise ValueError(f"Task file ID does not match {task_id}")
            if task.status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"Invalid task status: {task.status}")
            return task

    def list_tasks(self) -> list[Task]:
        with self.lock:
            if not self.directory.exists():
                return []
            if not self.root.is_relative_to(self.config.workdir.resolve()):
                raise ValueError("Tasks directory escapes workspace")
            return [
                self.load_task(path.stem) for path in sorted(self.directory.glob("task_*.json"))
            ]

    def get_task_json(self, task_id: str) -> str:
        return json.dumps(asdict(self.load_task(task_id)), indent=2)
