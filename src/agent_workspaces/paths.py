"""Private workspace paths and atomic JSON state."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def check_session_id(session_id: str) -> str:
    """Validate an opaque session id; reject path traversal and odd names."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    return session_id


def state_root() -> Path:
    return Path.home() / ".local" / "state" / "agent-workspaces"


def share_root() -> Path:
    return Path.home() / ".local" / "share" / "agent-workspaces"


def runtime_root() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(base) / "agent-workspaces"


def session_dir(session_id: str) -> Path:
    return state_root() / check_session_id(session_id)


def work_dir(session_id: str) -> Path:
    return session_dir(session_id) / "work"


def artifacts_dir(session_id: str) -> Path:
    return share_root() / check_session_id(session_id) / "artifacts"


def session_runtime_dir(session_id: str) -> Path:
    return runtime_root() / check_session_id(session_id)


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path


def atomic_write_json(path: Path, obj: object, mode: int = 0o600) -> None:
    """Write JSON atomically (tmp file + rename) so readers never see partials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
