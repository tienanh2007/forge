import os
import sys

from helpers import REPO_ROOT, ForgeTestCase
from forge_lib import issues, state, tasks

sys.path.insert(0, str(REPO_ROOT / "ui"))
import server  # noqa: E402


class FakeBackend:
    def __init__(self, home):
        self._home = home

    def home(self):
        return self._home


class FsApiTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project()
        tasks.new_task("proj", "T1-a", "A")
        issues.add("proj/T1-a", "Q", "question", "body")
        self.be = FakeBackend(self.home)
        (self.home / ".cache").mkdir(exist_ok=True)
        (self.home / ".cache" / "x.json").write_text("{}")

    def call(self, fn, path, **extra):
        return fn(self.be, {"path": [path], **{k: [v] for k, v in extra.items()}}, {})

    def assert_status(self, status, fn, path):
        with self.assertRaises(server.ApiError) as ctx:
            self.call(fn, path)
        self.assertEqual(ctx.exception.status, status, path)

    def test_tree_classifies_and_excludes(self):
        tree = server.api_fs_tree(self.be, {}, {})
        self.assertEqual(tree["rel"], "")
        self.assertEqual([c["name"] for c in tree["children"]], ["proj"])
        proj = tree["children"][0]
        self.assertEqual(proj["role"], "project")
        kinds = {c["name"]: c["kind"] for c in proj["children"] if c["type"] == "file"}
        self.assertEqual((kinds["project.json"], kinds["status.md"], kinds["tests.md"], kinds["spec.md"]),
                         ("state", "rendered", "authored", "authored"))
        task = self.call(server.api_fs_tree, "proj/tasks/T1-a")
        self.assertEqual(task["role"], "task")
        kinds = {c["name"]: c["kind"] for c in task["children"] if c["type"] == "file"}
        for name in ("PRs.md", "tests.md", "issues.md", "status.md", "HANDBACK.md"):
            self.assertEqual(kinds[name], "rendered", name)
        self.assertEqual((kinds["task.json"], kinds["log.md"], kinds["spec.md"]), ("state", "authored", "authored"))
        issue_dir = next(c for c in task["children"] if c["name"] == "issues")
        self.assertEqual(issue_dir["children"][0]["rel"], "proj/tasks/T1-a/issues/I-1.md")
        self.assertEqual(issue_dir["children"][0]["kind"], "authored")

    def test_file_and_meta(self):
        f = self.call(server.api_fs_file, "proj/tasks/T1-a/status.md")
        self.assertIn("# Status — proj/T1-a", f["content"])
        self.assertEqual((f["kind"], f["source"]), ("rendered", "task.json + tests/prs/issues/inbox json"))
        meta = self.call(server.api_fs_file, "proj/tasks/T1-a/issues/I-1.md", meta="1")
        self.assertNotIn("content", meta)
        self.assertEqual(meta["kind"], "authored")
        big = state.task_dir("proj/T1-a") / "big.txt"
        big.write_bytes(b"x" * (server.FS_MAX_FILE_BYTES + 1))
        self.assert_status(413, server.api_fs_file, "proj/tasks/T1-a/big.txt")

    def test_confinement(self):
        outside = self.tmp / "secret.txt"
        outside.write_text("nope")
        os.symlink(outside, state.task_dir("proj/T1-a") / "link.txt")
        os.symlink(self.tmp, self.home / "proj" / "escape")
        for bad in ("../secret.txt", "proj/../../secret.txt", str(outside), "/etc/passwd", ".cache/x.json",
                    "proj/tasks/T1-a/link.txt", "proj/escape/secret.txt", "proj\\..\\x", "proj/.hidden"):
            fn = server.api_fs_file
            with self.assertRaises(server.ApiError, msg=bad) as ctx:
                self.call(fn, bad)
            self.assertIn(ctx.exception.status, (400, 404), bad)
        self.assert_status(404, server.api_fs_file, "proj/nope.md")
        self.assert_status(400, server.api_fs_file, "proj")
        self.assert_status(400, server.api_fs_tree, "proj/project.json")
        names = [c["name"] for c in self.call(server.api_fs_tree, "proj")["children"]]
        self.assertNotIn("escape", names)
