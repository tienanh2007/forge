import io
from contextlib import redirect_stderr

from helpers import ForgeTestCase
from forge_lib import inbox, issues, prs, state, tasks, tests_store
from forge_lib.errors import NotFound, UsageError


class TasksTest(ForgeTestCase):
    def test_init_project_and_inheritance(self):
        p = self.make_project(sonar_key="sk", base_branch="dev", clickup_parent="ENG-100")
        self.assertEqual(p["schema"], 1)
        self.assertEqual(p["state"], "scoping")
        self.assertEqual(p["repos"][0], {"path": str(self.repo.resolve()), "github": "o/r",
                                          "sonar_project_key": "sk", "base_branch": "dev"})
        self.assertEqual(p["clickup_parent"], {"id": None, "custom_id": "ENG-100", "url": None})
        for f in ("project.json", "project.md", "spec.md", "tests.md", "plan.md"):
            self.assertTrue((state.task_dir("proj") / f).exists(), f)
        with self.assertRaises(UsageError):
            self.make_project()

        t = tasks.new_task("proj", "ENG-101-api", "API")
        self.assertEqual(t["key"], "proj/ENG-101-api")
        self.assertIsNone(t["parent_key"])
        self.assertEqual((t["repo"], t["github"], t["sonar_project_key"], t["base_branch"]),
                         (str(self.repo.resolve()), "o/r", "sk", "dev"))
        self.assertEqual(t["state"], "scoped")
        self.assertEqual(t["state_history"][0]["state"], "scoped")
        d = state.task_dir(t["key"])
        for f in ("task.json", "spec.md", "tests.json", "tests.md", "prs.json", "PRs.md", "issues.json",
                  "issues.md", "inbox.json", "log.md"):
            self.assertTrue((d / f).exists(), f)

        tasks.merge("proj/ENG-101-api", {"github": "o/other"})
        sub = tasks.new_task("proj/ENG-101-api", "ENG-105-dao", "DAO", base_branch="feature")
        self.assertEqual(sub["key"], "proj/ENG-101-api/ENG-105-dao")
        self.assertEqual(sub["parent_key"], "proj/ENG-101-api")
        self.assertEqual((sub["github"], sub["base_branch"], sub["sonar_project_key"]), ("o/other", "feature", "sk"))
        self.assertEqual(state.task_dir(sub["key"]),
                         self.home / "proj" / "tasks" / "ENG-101-api" / "tasks" / "ENG-105-dao")
        self.assertEqual(state.descendant_keys("proj"), ["proj/ENG-101-api", "proj/ENG-101-api/ENG-105-dao"])

    def test_new_task_errors(self):
        with self.assertRaises(NotFound):
            tasks.new_task("nope", "T1-x", "X")
        self.make_project()
        tasks.new_task("proj", "T1-x", "X")
        with self.assertRaises(UsageError):
            tasks.new_task("proj", "T1-x", "X")
        with self.assertRaises(UsageError):
            tasks.new_task("proj", "tasks", "X")

    def test_set_state_merge(self):
        self.make_project()
        tasks.new_task("proj", "T1-a", "A")
        t = tasks.set_state("proj/T1-a", "in-progress", note="go")
        self.assertEqual(t["state"], "in-progress")
        self.assertEqual(t["state_history"][-1]["note"], "go")
        with self.assertRaises(UsageError):
            tasks.set_state("proj/T1-a", "handed-back")
        with self.assertRaises(UsageError):
            tasks.set_state("proj/T1-a", "bogus")
        t = tasks.merge("proj/T1-a", {"gate": {"consecutive_blocks": 2}, "clickup": {"id": "x"}})
        self.assertEqual(t["gate"], {"last_run": None, "passed": False, "reasons": [], "consecutive_blocks": 2})
        self.assertEqual(t["clickup"], {"id": "x"})
        p = tasks.merge("proj", {"state": "active"})
        self.assertEqual(p["state"], "active")

    def test_ready_and_task_for_session(self):
        self.make_project()
        tasks.new_task("proj", "T1-a", "A")
        tasks.new_task("proj", "T2-b", "B", depends_on=["proj/T1-a"])
        with redirect_stderr(io.StringIO()) as err:
            tasks.new_task("proj", "T3-c", "C", depends_on=["proj/T9-missing"])
        self.assertIn("dependency does not exist yet: proj/T9-missing", err.getvalue())
        self.assertEqual([t["key"] for t in tasks.ready("proj")], ["proj/T1-a"])
        tasks.set_state("proj/T1-a", "done")
        self.assertEqual([t["key"] for t in tasks.ready()], ["proj/T2-b"])
        tasks.merge("proj/T2-b", {"session": {"id": "sid-1", "name": "n", "worktree": "n"}})
        self.assertEqual(tasks.task_for_session("sid-1"), "proj/T2-b")
        self.assertIsNone(tasks.task_for_session("other"))
        self.assertIsNone(tasks.task_for_session(""))

    def test_tree_counts(self):
        self.make_project(sonar_key=None)
        tasks.new_task("proj", "T1-a", "A")
        tasks.new_task("proj/T1-a", "T2-b", "B")
        tests_store.add("proj/T1-a", "one", "unit")
        t2 = tests_store.add("proj/T1-a", "two", "unit")
        tests_store.set_status("proj/T1-a", t2["id"], "green")
        issues.add("proj/T1-a/T2-b", "Q", "question", "body")
        inbox.add("proj/T1-a/T2-b", "hi")
        inbox.add("proj/T1-a/T2-b", "worker note", author="worker")
        prs.add("proj/T1-a", "https://github.com/o/r/pull/1")
        prs.add("proj/T1-a", "https://github.com/o/r/pull/2")
        items = state.load_json_items("proj/T1-a", "prs")
        from helpers import green_status
        items[0]["status"] = green_status(sonar=None)
        items[1]["status"] = green_status(checks="FAILURE", sonar=None)
        state.save_json_items("proj/T1-a", "prs", items)

        node = state.tree("proj")
        self.assertEqual(node["kind"], "project")
        self.assertEqual(node["counts"], {"open_issues": 1, "awaiting_user": 1, "open_inbox": 1, "prs": 2, "prs_green": 1,
                                          "tests": 2, "tests_green": 1})
        t1 = node["children"][0]
        self.assertEqual((t1["key"], t1["kind"], t1["state"]), ("proj/T1-a", "task", "scoped"))
        self.assertEqual(t1["counts"], {"open_issues": 0, "awaiting_user": 0, "open_inbox": 0, "prs": 2, "prs_green": 1,
                                        "tests": 2, "tests_green": 1})
        self.assertEqual(t1["children"][0]["counts"]["open_inbox"], 1)
        self.assertEqual(state.tree("proj/T1-a/T2-b")["key"], "proj/T1-a/T2-b")
        self.assertEqual([n["key"] for n in state.tree()], ["proj"])
