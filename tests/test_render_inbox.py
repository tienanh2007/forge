from helpers import ForgeTestCase
from forge_lib import inbox, issues, render, state, tasks, tests_store
from forge_lib.errors import NotFound, UsageError


class RenderTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project()
        tasks.new_task("proj", "T1-a", "A")
        self.key = "proj/T1-a"
        self.dir = state.task_dir(self.key)

    def test_idempotent_and_notes_preserved(self):
        tests_store.add(self.key, "rejects | pipes", "unit", command="make test")
        md = (self.dir / "tests.md").read_text()
        self.assertIn("rejects \\| pipes", md)
        self.assertIn("`make test`", md)
        self.assertTrue(md.rstrip().endswith("## Notes"))
        render.render(self.key)
        self.assertEqual((self.dir / "tests.md").read_text(), md)

        notes = "## Notes\n\nFlaky on CI, see run 42.\n\n### sub\nmore\n"
        (self.dir / "tests.md").write_text(md.replace("## Notes\n", notes))
        tests_store.add(self.key, "second", "e2e")
        new_md = (self.dir / "tests.md").read_text()
        self.assertIn("second", new_md)
        self.assertIn("Flaky on CI, see run 42.\n\n### sub\nmore", new_md)
        self.assertEqual(new_md.count("## Notes"), 1)
        render.render("proj")
        self.assertEqual((self.dir / "tests.md").read_text(), new_md)

    def test_extract_notes_stops_at_next_h2(self):
        text = "# T\n## Notes\nkeep\n## Other\ndrop\n"
        self.assertEqual(render.extract_notes(text), "## Notes\nkeep\n")
        self.assertEqual(render.extract_notes("no notes"), "## Notes\n")

    def test_issues_and_prs_render(self):
        i = issues.add(self.key, "Pick a DB", "decision", "```mermaid\ngraph TD; A-->B\n```")
        self.assertEqual(i["id"], "I-1")
        self.assertIn("mermaid", state.read_issue_body(self.key, "I-1"))
        self.assertIn("[Pick a DB](issues/I-1.md)", (self.dir / "issues.md").read_text())
        issues.set_artifact(self.key, "I-1", "https://claude.ai/x")
        issues.resolve(self.key, "I-1", "postgres")
        item = state.load_json_items(self.key, "issues")[0]
        self.assertEqual((item["status"], item["resolution"], item["artifact_url"]),
                         ("resolved", "postgres", "https://claude.ai/x"))
        issues.add(self.key, "Meh", "fyi", "x")
        issues.resolve(self.key, "I-2", "no", wontfix=True)
        self.assertEqual(state.load_json_items(self.key, "issues")[1]["status"], "wontfix")
        with self.assertRaises(UsageError):
            issues.add(self.key, "bad", "urgent", "x")
        with self.assertRaises(NotFound):
            state.read_issue_body(self.key, "I-9")

    def test_tests_validation(self):
        t = tests_store.add(self.key, "n", "ci")
        with self.assertRaises(UsageError):
            tests_store.set_status(self.key, t["id"], "skipped")
        tests_store.set_status(self.key, t["id"], "skipped", skip_reason="n/a")
        with self.assertRaises(UsageError):
            tests_store.add(self.key, "n", "bogus")
        with self.assertRaises(NotFound):
            tests_store.set_status(self.key, "TST-9", "green")


class InboxTest(ForgeTestCase):
    def test_lifecycle(self):
        self.make_project()
        tasks.new_task("proj", "T1-a", "A")
        tasks.new_task("proj", "T2-b", "B")
        key = "proj/T1-a"
        issues.add(key, "Q", "question", "b")
        m1 = inbox.add(key, "please check", issue_id="I-1")
        m2 = inbox.add("proj/T2-b", "other")
        inbox.add(key, "coord says hi", author="coordinator")
        self.assertEqual((m1["id"], m1["status"], m1["delivered"], m1["author"]), ("C-1", "open", False, "user"))
        self.assertEqual([m["id"] for m in inbox.list_items(key, open_only=True)], ["C-1", "C-2"])
        self.assertEqual([(m["task_key"], m["id"]) for m in inbox.pending()],
                         [("proj/T1-a", "C-1"), ("proj/T2-b", m2["id"])])
        inbox.mark_delivered("proj/T2-b", m2["id"])
        self.assertEqual([m["id"] for m in inbox.pending()], ["C-1"])
        inbox.ack(key, "C-1")
        self.assertEqual(inbox.pending(), [])
        self.assertEqual([m["id"] for m in inbox.list_items(key, open_only=True)], ["C-2"])
        inbox.resolve(key, "C-2", reply="done")
        items = inbox.list_items(key)
        self.assertEqual([(m["id"], m["author"], m["status"]) for m in items],
                         [("C-1", "user", "acked"), ("C-2", "coordinator", "resolved"), ("C-3", "worker", "open")])
        self.assertEqual(items[2]["reply_to"], "C-2")
        self.assertEqual(state.open_inbox(key), [])
        with self.assertRaises(NotFound):
            inbox.add(key, "x", issue_id="I-9")
        with self.assertRaises(UsageError):
            inbox.add(key, "  ")
