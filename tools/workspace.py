import subprocess
import threading
from pathlib import Path
from ..infrastructure.platform import stop_process_group


class WorkspaceTools:
    def __init__(self, workdir, *, memory_allowed=None):
        self.workdir = workdir.resolve()
        self._memory_allowed = memory_allowed
        self._init_tools()

    def _init_tools(self):
        self._shell_processes: set[subprocess.Popen] = set()
        self._shell_process_lock = threading.RLock()

    def _path_allowed(self, requested: Path, resolved: Path, base: Path, *, write=False):
        if self._memory_allowed is None or self._memory_allowed(write=write):
            return True
        return not any(
            requested.is_relative_to(memory) or resolved.is_relative_to(memory.resolve())
            for memory in {self.workdir / ".memory", base / ".memory"}
        )

    def safe_path(self, path: str, cwd: Path | None = None, *, write=False) -> Path:
        base = (cwd or self.workdir).resolve()
        requested = base / path
        resolved = requested.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"Path escapes workspace: {path}")
        if not self._path_allowed(requested, resolved, base, write=write):
            raise PermissionError("Session memory settings disallow access to this path")
        return resolved

    def _stop_process_group(self, process: subprocess.Popen):
        """Stop processes that remain in the command's original process group."""
        stop_process_group(process)

    def _stop_all_shell_processes(self):
        with self._shell_process_lock:
            processes = list(self._shell_processes)
        for process in processes:
            self._stop_process_group(process)

    def _run_bash_process(self, command: str, cwd: Path | None = None) -> tuple[str, int | None]:
        process = None
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd or self.workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
            )
            with self._shell_process_lock:
                self._shell_processes.add(process)
            stdout, stderr = process.communicate(timeout=120)
            out = (stdout + stderr).strip()
            return (out[:50000] if out else "(no output)", process.returncode)
        except subprocess.TimeoutExpired:
            return ("Error: Timeout (120s)", None)
        except OSError as exc:
            return (f"Error: {type(exc).__name__}: {exc}", None)
        finally:
            if process is not None:
                self._stop_process_group(process)
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
                with self._shell_process_lock:
                    self._shell_processes.discard(process)

    def _format_bash_result(self, output: str, exit_code: int | None) -> str:
        if exit_code == 0:
            return output
        if exit_code is None:
            return output
        return f"Error: command exited with status {exit_code}\n{output}"

    def run_bash(
        self, command: str, cwd: Path | None = None, run_in_background: bool = False
    ) -> str:
        return self._format_bash_result(*self._run_bash_process(command, cwd))

    def run_read(
        self,
        path: str,
        limit: int | None = None,
        offset: int = 0,
        cwd: Path | None = None,
    ) -> str:
        try:
            file_path = self.safe_path(path, cwd)
            lines = file_path.read_text(encoding="utf-8").splitlines()
            offset = max(int(offset or 0), 0)
            limit = int(limit) if limit is not None else None
            lines = lines[offset:]
            if limit is not None and limit < len(lines):
                lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def run_write(self, path: str, content: str, cwd: Path | None = None) -> str:
        try:
            fp = self.safe_path(path, cwd, write=True)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_edit(self, path: str, old_text: str, new_text: str, cwd: Path | None = None) -> str:
        try:
            fp = self.safe_path(path, cwd, write=True)
            text = fp.read_text(encoding="utf-8")
            if old_text not in text:
                return f"Error: text not found in {path}"
            fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            return f"Edited {path}"
        except Exception as e:
            return f"Error: {e}"

    def run_glob(self, pattern: str, cwd: Path | None = None) -> str:
        import glob as g

        try:
            base = (cwd or self.workdir).resolve()
            matches = set()
            for match in g.glob(pattern, root_dir=base, recursive=True):
                requested = base / match
                resolved = requested.resolve()
                if resolved.is_relative_to(base) and self._path_allowed(requested, resolved, base):
                    matches.add(match)
            shown = sorted(matches)[:200]
            if len(matches) > 200:
                shown.append("... (more matches omitted; narrow the pattern)")
            return "\n".join(shown) if shown else "(no matches)"
        except Exception as e:
            return f"Error: {e}"
