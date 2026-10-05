import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from forge_lib.errors import IntegrationError

DEFAULT_ENV_FILE = "~/.config/forge/env"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path, default: str = "") -> str:
    path = Path(path)
    return path.read_text(encoding="utf-8") if path.exists() else default


def env_file() -> Path:
    return Path(os.environ.get("FORGE_ENV_FILE", DEFAULT_ENV_FILE)).expanduser()


def get_config(key: str, default=None):
    """Config value from the environment, falling back to KEY=VALUE lines in ~/.config/forge/env."""
    if os.environ.get(key):
        return os.environ[key]
    path = env_file()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip().removeprefix("export ").strip() == key:
                return v.strip().strip("'\"")
    return default


def run(cmd: list[str], cwd=None, input_text: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    """Run a command capturing text output. Raises IntegrationError if the binary is missing."""
    try:
        # Never inherit stdin: `claude` appends piped stdin to its prompt.
        stdin = None if input_text is not None else subprocess.DEVNULL
        return subprocess.run(cmd, cwd=cwd, input=input_text, stdin=stdin, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as e:
        raise IntegrationError(f"command not found: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise IntegrationError(f"command timed out: {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}") from e


def http_request(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                 timeout: float = 30) -> tuple[int, str]:
    """Return (status, body_text); HTTP error statuses are returned, not raised."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise IntegrationError(f"{method} {url.split('?')[0]} failed: {reason}") from e


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base (dicts merge, everything else replaces). Mutates base."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def next_id(items: list[dict], prefix: str) -> str:
    nums = [int(i["id"][len(prefix):]) for i in items
            if str(i.get("id", "")).startswith(prefix) and i["id"][len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"
