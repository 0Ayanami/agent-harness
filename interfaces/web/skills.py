"""Read-only browser for SKILL.md manifests in the selected workdir."""

import os
import re
import stat
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ...services.context import parse_skill_frontmatter


MAX_SKILL_BYTES = 256 * 1024
SKILL_ID = re.compile(r"[0-9a-f]{64}")


def _plain_status(path, check_mode):
    try:
        details = path.lstat()
    except OSError:
        return None
    if not check_mode(details.st_mode) or (
        getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        return None
    return details


def _manifests(root):
    if _plain_status(root, stat.S_ISDIR) is None:
        return
    for directory, children, files in os.walk(root, followlinks=False):
        current = Path(directory)
        if current != root and "SKILL.md" in files:
            relative = current.relative_to(root)
            # Auxiliary files in a skill directory are not part of the browser.
            children.clear()
            try:
                key = relative.as_posix().encode("utf-8")
            except UnicodeEncodeError:
                continue
            yield sha256(key).hexdigest(), relative, current / "SKILL.md"
        else:
            children[:] = sorted(
                name
                for name in children
                if not name.startswith(".") and _plain_status(current / name, stat.S_ISDIR)
            )


def _manifest_status(root, path):
    try:
        if not path.resolve(strict=True).is_relative_to(root):
            return None
    except (OSError, RuntimeError):
        return None
    for parent in path.parents:
        if _plain_status(parent, stat.S_ISDIR) is None:
            return None
        if parent == root:
            break
    details = _plain_status(path, stat.S_ISREG)
    if details is None or details.st_nlink > 1 or details.st_size > MAX_SKILL_BYTES:
        return None
    return details


def _open_regular(path, flags):
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))


def _read_manifest(root, path):
    before = _manifest_status(root, path)
    if before is None:
        return None
    try:
        with open(path, "rb", opener=_open_regular) as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink > 1
                or not os.path.samestat(before, opened)
            ):
                return None
            raw = source.read(MAX_SKILL_BYTES + 1)
            after = _manifest_status(root, path)
            if len(raw) > MAX_SKILL_BYTES or after is None or not os.path.samestat(opened, after):
                return None
        return raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _metadata(skill_id, relative, markdown):
    try:
        meta, _ = parse_skill_frontmatter(markdown)
    except RecursionError:
        meta = {}
    name, description = meta.get("name"), meta.get("description")
    return {
        "id": skill_id,
        "name": (name.strip() or relative.name)[:160] if isinstance(name, str) else relative.name,
        "category": relative.parent.as_posix() if len(relative.parts) > 1 else None,
        "description": " ".join(description.split())[:500] if isinstance(description, str) else "",
    }


def skills_router(workdir):
    router = APIRouter(prefix="/skills", tags=["skills"])
    root = Path(workdir).resolve() / "skills"

    @router.get("")
    def list_skills():
        entries = []
        for skill_id, relative, path in _manifests(root):
            markdown = _read_manifest(root, path)
            if markdown is not None:
                entries.append(_metadata(skill_id, relative, markdown))
        return {"skills": entries}

    @router.get("/{skill_id}")
    def read_skill(skill_id: str):
        if SKILL_ID.fullmatch(skill_id):
            for candidate_id, relative, path in _manifests(root):
                if candidate_id == skill_id:
                    markdown = _read_manifest(root, path)
                    if markdown is not None:
                        return {**_metadata(skill_id, relative, markdown), "markdown": markdown}
                    break
        raise HTTPException(404, "Skill not found")

    return router
