import json
from unittest import mock

from helpers import ForgeTestCase, green_status
from forge_lib import gate, hooks, inbox, issues, prs, state, tasks, tests_store


class GateBase(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project(sonar_key="sk")
        tasks.new_task("proj", "T1-a", "A")
        self.key = "proj/T1-a"
        self.gh = {"status": green_status(sonar=None)}
        self.sq = {"status": {"status": "OK", "conditions": [], "url": "s"}}
        p1 = mock.patch("forge_lib.prs.github.pr_status", side_effect=lambda r, n: dict(self.gh["status"]))
        p2 = mock.patch("forge_lib.prs.sonar.quality_gate", side_effect=lambda k, n: dict(self.sq["status"]))
        self.pr_status = p1.start()
        self.quality_gate = p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def make_green(self):
        t = tests_store.add(self.key, "t", "unit")
        tests_store.set_status(self.key, t["id"], "green", evidence="ok")
        prs.add(self.key, "https://github.com/o/r/pull/7")


class GateTest(GateBase):
    def test_empty_fails(self):
        r = gate.run(self.key)
        self.assertFalse(r["passed"])
        self.assertEqual(r["reasons"], ["no tests recorded in tests.json", "no PRs recorded in prs.json"])
        g = state.load_task(self.key)["gate"]
        self.assertEqual((g["passed"], g["reasons"], g["consecutive_blocks"]), (False, r["reasons"], 0))

    def test_pass(self):
        self.make_green()
        r = gate.run(self.key)
        self.assertEqual(r["reasons"], [])
        self.assertTrue(r["passed"])
        self.pr_status.assert_called_once_with("o/r", 7)
        self.quality_gate.assert_called_once_with("sk", 7)
        item = state.load_json_items(self.key, "prs")[0]
        self.assertEqual(item["status"]["sonar"]["status"], "OK")
        self.assertEqual((item["title"], item["branch"], item["base"]), ("t", "b", "main"))

    def test_failure_reasons(self):
        self.make_green()
        tests_store.add(self.key, "pending one", "unit")
        self.gh["status"] = green_status(checks="FAILURE", unresolved=2, sonar=None)
        self.sq["status"] = {"status": "NONE", "conditions": [], "url": "s"}
        inbox.add(self.key, "hey")
        tasks.new_task(self.key, "T2-child", "child")
        r = gate.run(self.key)
        self.assertEqual(r["reasons"], [
            "test TST-2 (pending one) is pending",
            "PR o/r#7: CI failing (build)",
            "PR o/r#7: 2 unresolved review thread(s)",
            "PR o/r#7: sonar pending",
            "open inbox message C-1 from user",
            "child task proj/T1-a/T2-child is scoped",
        ])

    def test_sonar_error_and_fetch_error(self):
        self.make_green()
        self.sq["status"] = {"status": "ERROR", "conditions": [
            {"metric": "new_coverage", "status": "ERROR", "actual": "10", "threshold": "80"}], "url": "s"}
        self.assertEqual(gate.run(self.key)["reasons"], ["PR o/r#7: sonar quality gate ERROR (new_coverage)"])
        from forge_lib.errors import IntegrationError
        self.pr_status.side_effect = IntegrationError("gh down")
        r = gate.run(self.key)
        self.assertEqual(r["reasons"], ["PR o/r#7: status fetch failed: gh down"])
        self.assertEqual(state.load_json_items(self.key, "prs")[0]["status"]["checks"]["state"], "SUCCESS")

    def test_no_sonar_key_skips_sonar(self):
        tasks.merge(self.key, {"sonar_project_key": None})
        self.make_green()
        self.assertTrue(gate.run(self.key)["passed"])
        self.quality_gate.assert_not_called()

    def test_handback(self):
        from forge_lib.errors import UsageError
        with self.assertRaises(UsageError):
            gate.handback(self.key, "", "docs")
        self.assertFalse(gate.handback(self.key, "s", "d")["passed"])
        self.assertEqual(state.load_task(self.key)["state"], "scoped")
        self.make_green()
        self.assertTrue(gate.handback(self.key, "did it", "docs/x.md")["passed"])
        t = state.load_task(self.key)
        self.assertEqual(t["state"], "handed-back")
        self.assertEqual(t["handback"]["docs"], "docs/x.md")


class HooksTest(GateBase):
    def setUp(self):
        super().setUp()
        tasks.merge(self.key, {"session": {"id": "sid", "name": "forge-proj-T1-a", "worktree": "forge-proj-T1-a"}})

    def stop(self):
        out = hooks.stop({"session_id": "sid", "stop_hook_active": False, "cwd": "/x"})
        return json.loads(out) if out else None

    def test_unknown_session_allows(self):
        self.assertEqual(hooks.stop({"session_id": "nope"}), "")
        self.assertEqual(hooks.session_start({"session_id": "nope"}), "")

    def test_blocks_on_open_inbox_then_resets(self):
        inbox.add(self.key, "look at the DAO")
        d = self.stop()
        self.assertEqual(d["decision"], "block")
        self.assertIn("C-1 (user): look at the DAO", d["reason"])
        self.assertEqual(state.load_task(self.key)["gate"]["consecutive_blocks"], 1)
        inbox.ack(self.key, "C-1")
        self.assertIsNone(self.stop())
        self.assertEqual(state.load_task(self.key)["gate"]["consecutive_blocks"], 0)

    def test_in_review_gate_and_cap(self):
        tasks.set_state(self.key, "in-review")
        for i in range(1, 6):
            d = self.stop()
            self.assertEqual(d["decision"], "block", i)
            self.assertIn("no tests recorded", d["reason"])
            self.assertEqual(state.load_task(self.key)["gate"]["consecutive_blocks"], i)
        self.assertIsNone(self.stop())
        t = state.load_task(self.key)
        self.assertEqual(t["state"], "blocked")
        self.assertIn("blocked 5 times", t["state_history"][-1]["note"])
        self.assertEqual(t["gate"]["consecutive_blocks"], 0)

    def test_in_review_gate_passing_allows(self):
        tasks.set_state(self.key, "in-review")
        self.make_green()
        self.assertIsNone(self.stop())

    def test_in_progress_does_not_run_gate(self):
        tasks.set_state(self.key, "in-progress")
        self.assertIsNone(self.stop())
        self.pr_status.assert_not_called()

    def test_session_start_briefing(self):
        d = state.task_dir(self.key)
        (d / "spec.md").write_text("\n".join(f"spec line {i}" for i in range(100)))
        (d / "log.md").write_text("\n".join(f"log line {i}" for i in range(100)))
        inbox.add(self.key, "question for you")
        issues.add(self.key, "Need creds", "blocker", "b")
        self.make_green()
        gate.run(self.key)
        out = hooks.session_start({"session_id": "sid", "source": "startup"})
        self.assertIn("# forge task proj/T1-a", out)
        self.assertIn(f"Task folder: {d}", out)
        self.assertIn("spec line 59", out)
        self.assertNotIn("spec line 60", out)
        self.assertIn("log line 99", out)
        self.assertIn("log line 70", out)
        self.assertNotIn("log line 69", out)
        self.assertIn("C-1 (user): question for you", out)
        self.assertIn("I-1 [blocker] Need creds", out)
        self.assertIn("o/r#7", out)
        self.assertIn("open inbox message C-1", out)

    def test_coordinator_briefing(self):
        tasks.merge("proj", {"coordinator_session": {"id": "coord", "name": "c"}})
        out = hooks.session_start({"session_id": "coord"})
        self.assertIn("forge project proj", out)
        self.assertIn("proj/T1-a [scoped]", out)
