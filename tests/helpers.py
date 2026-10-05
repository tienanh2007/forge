import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forge_lib import cli, dispatch, tasks  # noqa: E402

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
entry = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if sys.argv[1:2] == ["-p"]:
    entry["stdin"] = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
if sys.argv[1:2] == ["-p"]:
    print(os.environ.get("FAKE_CLAUDE_BRIDGE_OUT", "SENT"))
if sys.argv[1:2] == ["agents"]:
    path = os.environ.get("FAKE_CLAUDE_AGENTS")
    print(open(path).read() if path and os.path.exists(path) else "[]")
"""


def green_status(checks="SUCCESS", unresolved=0, sonar="OK"):
    return {"url": "u", "title": "t", "state": "OPEN", "is_draft": True, "base": "main", "head": "b",
            "review_decision": None,
            "checks": {"state": checks, "total": 1, "failing": [{"name": "build", "url": "x"}]
                       if checks == "FAILURE" else [], "pending": []},
            "threads": {"total": unresolved, "resolved": 0, "unresolved": unresolved, "items": []},
            "comments_count": 0, "updated_at": "2026-09-25T00:00:00Z",
            **({"sonar": {"status": sonar, "conditions": [], "url": "s"}} if sonar else {})}


class ForgeTestCase(unittest.TestCase):
    def setUp(self):
        self._resolve_attempts = dispatch.RESOLVE_ATTEMPTS
        dispatch.RESOLVE_ATTEMPTS = 1  # no sleeping while polling for the launched session
        self.addCleanup(setattr, dispatch, "RESOLVE_ATTEMPTS", self._resolve_attempts)
        self.tmp = Path(tempfile.mkdtemp(prefix="forge-test-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.claude_log = self.tmp / "claude.log"
        self.agents_file = self.tmp / "agents.json"
        self._env = dict(os.environ)
        os.environ.update({
            "FORGE_HOME": str(self.home), "FORGE_ENV_FILE": str(self.tmp / "no-env"),
            "FAKE_CLAUDE_LOG": str(self.claude_log), "FAKE_CLAUDE_AGENTS": str(self.agents_file),
            "FORGE_CLAUDE_PROJECTS_DIR": str(self.tmp / "claude-projects"),
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
        })
        for k in ("CLICKUP_TOKEN", "SONAR_TOKEN", "CLICKUP_TEAM_ID", "FAKE_CLAUDE_BRIDGE_OUT"):
            os.environ.pop(k, None)
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_project(self, slug="proj", **kw):
        kw.setdefault("github", "o/r")
        return tasks.init_project(slug, "Project", str(self.repo), **kw)

    def claude_calls(self) -> list[dict]:
        if not self.claude_log.exists():
            return []
        return [json.loads(line) for line in self.claude_log.read_text().splitlines()]

    def set_agents(self, agents: list[dict]):
        self.agents_file.write_text(json.dumps(agents))

    def forge(self, *argv, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as e:
                    code = e.code
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue(), err.getvalue()
