import re
import subprocess
from pathlib import Path


class GitWorktreeAdapter:
    def __init__(self, config):
        self.config = config
        self.directory = config.workdir / ".worktrees"
        self.root = self.directory.resolve()
        self.name_pattern = re.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    def validate_name(self, name: str) -> str | None:
        if not isinstance(name, str) or not self.name_pattern.fullmatch(name):
            return "worktree name must be 1-64 letters, digits, dots, underscores, or dashes, and start with a letter or digit"
        if name in {".", ".."} or ".." in name:
            return "worktree name cannot contain '..'"
        return None

    def path_for(self, name: str) -> Path:
        path = (self.directory / name).resolve()
        if (
            not self.root.is_relative_to(self.config.workdir.resolve())
            or not path.is_relative_to(self.root)
            or path == self.root
        ):
            raise ValueError(f"Worktree path escapes directory: {name!r}")
        return path

    def branch_for(self, name: str) -> str:
        return f"wt/{name}"

    def run(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        """Run Git without shell interpolation and return (ok, combined output)."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd or self.config.workdir,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return (False, f"{type(exc).__name__}: {exc}")
        output = (result.stdout + result.stderr).strip()
        return (result.returncode == 0, output or "(no output)")

    def run_checked(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        """Run Git and bound only the text returned to the model."""
        ok, output = self.run(args, cwd)
        return (ok, output[:5000])

    def list_registered(self) -> tuple[dict[Path, dict[str, str]], str | None]:
        ok, output = self.run(["worktree", "list", "--porcelain"])
        if not ok:
            return ({}, f"cannot read Git worktree registry: {output}")
        entries: dict[Path, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in output.splitlines() + [""]:
            if not line:
                raw_path = current.get("worktree")
                if raw_path:
                    entries[Path(raw_path).resolve()] = current
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return (entries, None)

    def registered(self, name: str) -> tuple[Path | None, str | None]:
        try:
            path = self.path_for(name)
        except ValueError as exc:
            return (None, str(exc))
        entries, error = self.list_registered()
        if error:
            return (None, error)
        if path not in entries:
            return (None, f"worktree '{name}' is not registered with Git")
        if not path.is_dir():
            return (None, f"worktree '{name}' is missing at {path}")
        expected_branch = f"refs/heads/{self.branch_for(name)}"
        if entries[path].get("branch") != expected_branch:
            return (
                None,
                f"worktree '{name}' is not registered on expected branch '{self.branch_for(name)}'",
            )
        return (path, None)
