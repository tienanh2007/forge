from unittest import mock

from helpers import ForgeTestCase, green_status
from forge_lib import gate, inbox, issues, prs, render, state, tasks, tests_store


class StatusRenderTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project()
        tasks.new_task("proj", "T1-a", "Alpha")
        self.key = "proj/T1-a"
        self.dir = state.task_dir(self.key)
        self.gh = {"status": green_status(sonar=None)}
        p = mock.patch("forge_lib.prs.github.pr_status", side_effect=lambda r, n: dict(self.gh["status"]))
        p.start()
        self.addCleanup(p.stop)

    def status(self, key=None) -> str:
        return (state.task_dir(key or self.key) / "status.md").read_text()

    def project_status(self) -> str:
        return (state.project_dir("proj") / "status.md").read_text()

    def test_created_with_task(self):
        md = self.status()
        self.assertIn("# Status — proj/T1-a", md)
        self.assertIn("| State | scoped |", md)
        self.assertIn("| Owner session | — |", md)
        self.assertIn("| Gate | not run |", md)
        self.assertIn("| PRs | no PRs |", md)
        self.assertIn("_Not handed back yet._ Current state: scoped.", (self.dir / "HANDBACK.md").read_text())
        self.assertIn("| [T1-a](tasks/T1-a/status.md) | scoped | — | not run | 0 | 0 | none | no PRs | 0/0 | 0 |",
                      self.project_status())

    def test_rerendered_on_mutations(self):
        tasks.set_state(self.key, "in-progress", note="picked up")
        self.assertIn("| State | in-progress |", self.status())
        self.assertRegex(self.status(), r"\| .*Z \| in-progress \| picked up \|")
        self.assertIn("| in-progress |", self.project_status())

        tests_store.add(self.key, "t", "unit")
        self.assertIn("| Tests | 0/1 green |", self.status())
        issues.add(self.key, "Stuck", "blocker", "body")
        self.assertIn("| Open issues | 1 (1 blocker) |", self.status())
        self.assertIn("- [I-1](issues/I-1.md) (blocker, **awaiting-user**) Stuck", self.status())
        self.assertIn("| in-progress | — | not run | 1 |", self.project_status())
        inbox.add(self.key, "hello")
        self.assertIn("| Open inbox | 1 |", self.status())
        tasks.merge(self.key, {"session": {"id": "abc", "name": "forge-proj-T1-a"}})
        self.assertIn("| Owner session | forge-proj-T1-a (`abc`) |", self.status())

    def test_gate_writes_reasons_and_pr_problems(self):
        prs.add(self.key, "https://github.com/o/r/pull/7")
        tasks.merge(self.key, {"required_checks": ["lint"]})
        self.gh["status"] = green_status(checks="FAILURE", unresolved=1, sonar=None)
        r = gate.run(self.key)
        self.assertFalse(r["passed"])
        md = self.status()
        self.assertIn("| Gate | FAIL (last run ", md)
        self.assertIn("- no tests recorded in tests.json", md)
        self.assertIn("- [o/r#7](https://github.com/o/r/pull/7) t\n  - CI failing (build)\n"
                      "  - required check 'lint' never ran (draft PRs may skip it)\n"
                      "  - 1 unresolved review thread(s)", md)
        self.assertIn("| PRs | 0/1 green |", md)
        prs_md = (self.dir / "PRs.md").read_text()
        self.assertIn("| Problems |", prs_md)
        self.assertIn("CI failing (build); required check 'lint' never ran", prs_md)
        tasks.merge(self.key, {"required_checks": []})  # task.json change re-renders PRs.md problems
        self.assertNotIn("'lint'", (self.dir / "PRs.md").read_text())
        self.assertIn("0/1 green", self.project_status())

    def test_children_listed_and_refreshed(self):
        tasks.new_task(self.key, "T2-kid", "Kid")
        self.assertIn("| [T2-kid](tasks/T2-kid/status.md) | scoped | not run |", self.status())
        tasks.set_state(f"{self.key}/T2-kid", "blocked")
        self.assertIn("| [T2-kid](tasks/T2-kid/status.md) | blocked | not run |", self.status())
        self.assertIn("| ↳ [T1-a/T2-kid](tasks/T1-a/tasks/T2-kid/status.md) | blocked |", self.project_status())

    def test_handback_snapshot(self):
        t = tests_store.add(self.key, "works", "unit")
        tests_store.set_status(self.key, t["id"], "green", evidence="ci#1")
        prs.add(self.key, "https://github.com/o/r/pull/7")
        r = gate.handback(self.key, "Did the thing", "docs/x.md")
        self.assertTrue(r["passed"])
        hb = state.load_task(self.key)["handback"]
        self.assertEqual(hb["gate"], {"passed": True, "reasons": [], "last_run": r["last_run"]})
        md = (self.dir / "HANDBACK.md").read_text()
        self.assertIn("current state: handed-back", md)
        self.assertIn("## Summary\n\nDid the thing\n\n## Docs\n\ndocs/x.md", md)
        self.assertIn(f"## Gate at handback\n\nPASS (last run {r['last_run']})\n", md)
        self.assertIn("| [o/r#7](https://github.com/o/r/pull/7) | t | OPEN | SUCCESS | - | 0 open / 0 |", md)
        self.assertIn("1/1 green", md)
        self.assertIn("| TST-1 | works | green | ci#1 |", md)
        # A later failing gate changes status.md but not the handback snapshot.
        self.gh["status"] = green_status(checks="FAILURE", sonar=None)
        gate.run(self.key)
        render.render(self.key)
        self.assertIn("PASS (last run", (self.dir / "HANDBACK.md").read_text())
        self.assertIn("| Gate | FAIL", self.status())

    def test_render_all_cli(self):
        self.make_project("other")
        tasks.new_task("other", "T9-z", "Zed")
        for path in (self.dir / "status.md", self.dir / "HANDBACK.md", self.dir / "PRs.md",
                     state.project_dir("other") / "status.md"):
            path.unlink()
        code, out, _ = self.forge("render", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["other", "other/T9-z", "proj", "proj/T1-a"])
        self.assertTrue((self.dir / "HANDBACK.md").exists())
        self.assertTrue((state.project_dir("other") / "status.md").exists())
        self.assertIn("_No PRs yet._", (self.dir / "PRs.md").read_text())
        self.assertEqual(self.forge("render")[0], 1)
        self.assertEqual(self.forge("render", "proj", "--all")[0], 1)
