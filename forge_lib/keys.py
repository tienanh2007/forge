"""Task key <-> filesystem path. Key = slug/dir/dir..., path = home/slug/tasks/dir/tasks/dir."""
import re
from pathlib import Path

from forge_lib.errors import UsageError

SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TASKS = "tasks"


def validate_segment(seg: str) -> str:
    if not SEGMENT_RE.match(seg or "") or seg == TASKS or ".." in seg:
        raise UsageError(f"invalid key segment: {seg!r}")
    return seg


def split_key(key: str) -> list[str]:
    parts = key.strip("/").split("/")
    for p in parts:
        validate_segment(p)
    return parts


def is_project_key(key: str) -> bool:
    return len(split_key(key)) == 1


def project_of(key: str) -> str:
    return split_key(key)[0]


def parent_key(key: str) -> str | None:
    parts = split_key(key)
    return "/".join(parts[:-1]) if len(parts) > 1 else None


def key_to_path(home: Path, key: str) -> Path:
    parts = split_key(key)
    path = Path(home) / parts[0]
    for seg in parts[1:]:
        path = path / TASKS / seg
    return path


def path_to_key(home: Path, path) -> str:
    rel = Path(path).resolve().relative_to(Path(home).resolve()).parts
    if not rel or len(rel) % 2 == 0 or any(rel[i] != TASKS for i in range(1, len(rel), 2)):
        raise UsageError(f"not a forge project/task path: {path}")
    return "/".join(split_key("/".join(rel[0::2])))
