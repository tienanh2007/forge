import json
import sys

from helpers import REPO_ROOT, ForgeTestCase
from forge_lib import gate, issues, render, state, tasks
from forge_lib.errors import UsageError

sys.path.insert(0, str(REPO_ROOT / "ui"))
import server  # noqa: E402

KEY = "proj/T1-a"
SID = "11111111-2222-3333-4444-555555555555"


class IssueStatesTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project()
        tasks.new_task("proj", "T1-a", "Alpha")

    def issue(self, issue_id="I-1", key=KEY):
        return state.find_item(state.load_json_items(key, "issues"), issue_id, "issue")

    def dispatch_live(self):
        tasks.merge(KEY, {"session": {"id": SID, "bg_id": "aaaa", "name": "forge-proj-T1-a"}})
        self.set_agents([{"id": "aaaa", "sessionId": SID, "name": "forge-proj-T1-a", "kind": "background",
                          "state": "working", "status": "busy", "cwd": "/w", "startedAt": 1}])

    def test_add_default_states_and_override(self):
        self.assertEqual(issues.add(KEY, "Q", "question", "b")["state"], "awaiting-user")
        fyi = issues.add(KEY, "F", "fyi", "b")
        self.assertEqual((fyi["state"], fyi["status"], fyi["decision"]), ("open", "open", None))
        self.assertEqual(fyi["history"][0]["by"], "worker")
        code, out, _ = self.forge("issues", "add", KEY, "--title", "F2", "--severity", "fyi", "--body", "b",
                                  "--state", "awaiting-user", "--by", "reviewer")
        self.assertEqual((code, out.strip()), (0, "I-3"))
        i3 = self.issue("I-3")
        self.assertEqual((i3["state"], i3["history"][0]["by"]), ("awaiting-user", "reviewer"))

    def test_set_state_validates_transitions_and_keeps_status_in_sync(self):
        issues.add(KEY, "Q", "question", "b")
        code, _, err = self.forge("issues", "set-state", KEY, "I-1", "open", "--note", "triage", "--by", "user")
        self.assertEqual(code, 0, err)
        code, _, err = self.forge("issues", "resolve", KEY, "I-1", "--resolution", "done")
        self.assertEqual(code, 0, err)
        item = self.issue()
        self.assertEqual((item["state"], item["status"], item["resolution"]), ("resolved", "resolved", "done"))
        code, _, err = self.forge("issues", "set-state", KEY, "I-1", "in-progress")
        self.assertEqual(code, 1)
        self.assertIn("cannot go resolved -> in-progress (allowed: open)", err)
        self.forge("issues", "set-state", KEY, "I-1", "open", "--note", "reopened")
        item = self.issue()
        self.assertEqual((item["state"], item["status"]), ("open", "open"))
        self.assertEqual([(h["state"], h["by"], h["note"]) for h in item["history"]],
                         [("awaiting-user", "worker", "raised"), ("open", "user", "triage"),
                          ("resolved", "worker", "done"), ("open", "worker", "reopened")])
        issues.resolve(KEY, "I-1", "dropped", wontfix=True)
        self.assertEqual((self.issue()["state"], self.issue()["status"]), ("wontfix", "wontfix"))

    def test_decide_without_session_queues_message(self):
        issues.add(KEY, "Pick", "decision", "b")
        code, out, err = self.forge("issues", "decide", KEY, "I-1", "--decision", "Option A")
        self.assertEqual(code, 0, err)
        self.assertIn("QUEUED in inbox (C-1)", out)
        msg = state.load_json_items(KEY, "inbox")[0]
        self.assertEqual((msg["author"], msg["issue_id"], msg["body"], msg["delivered"]),
                         ("user", "I-1", "Decision on I-1: Option A", False))
        item = self.issue()
        self.assertEqual(item["state"], "decided")
        self.assertEqual({k: item["decision"][k] for k in ("text", "by", "comment_id")},
                         {"text": "Option A", "by": "user", "comment_id": "C-1"})
        self.assertEqual(item["history"][-1] | {"at": None},
                         {"state": "decided", "at": None, "by": "user", "note": "Option A", "comment_id": "C-1"})

    def test_decide_with_live_session_sends_through_bridge(self):
        self.dispatch_live()
        issues.add(KEY, "Pick", "decision", "b")
        r = issues.decide(KEY, "I-1", "Option B", by="coordinator")
        self.assertEqual((r["delivery"]["mode"], r["delivery"]["delivered"]), ("sent", True))
        bridge = [c for c in self.claude_calls() if c["argv"][:1] == ["-p"]]
        self.assertIn("forge inbox proj/T1-a C-1: Decision on I-1: Option B", bridge[0]["stdin"])
        msg = state.load_json_items(KEY, "inbox")[0]
        self.assertEqual((msg["author"], msg["issue_id"], msg["delivered"]), ("coordinator", "I-1", True))

    def test_decide_from_comment_does_not_post_and_rejects_closed(self):
        issues.add(KEY, "Pick", "decision", "b")
        c1 = self.forge("inbox", "add", KEY, "--body", "go with A", "--issue", "I-1")[1].strip()
        code, out, _ = self.forge("issues", "decide", KEY, "I-1", "--decision", "A", "--comment-id", c1)
        self.assertEqual(code, 0)
        self.assertIn("worker not notified", out)
        self.assertEqual(len(state.load_json_items(KEY, "inbox")), 1)
        self.assertEqual(self.issue()["decision"]["comment_id"], c1)
        code, _, err = self.forge("issues", "decide", KEY, "I-1", "--decision", "A", "--comment-id", "C-9")
        self.assertEqual(code, 1)
        self.assertIn("message C-9 not found", err)
        issues.resolve(KEY, "I-1", "did A")
        with self.assertRaises(UsageError):
            issues.decide(KEY, "I-1", "B")

    def test_gate_fails_on_unacted_decision(self):
        issues.add(KEY, "Pick", "decision", "b")
        issues.decide(KEY, "I-1", "A", notify=False)
        self.assertIn("issue I-1 is decided: decision not acted on yet (resolve it with forge issues resolve)",
                      gate.evaluate(KEY, refresh=False))
        issues.set_state(KEY, "I-1", "in-progress")
        self.assertTrue(any("I-1 is in-progress" in r for r in gate.evaluate(KEY, refresh=False)))
        issues.resolve(KEY, "I-1", "did A")
        self.assertFalse(any("I-1" in r for r in gate.evaluate(KEY, refresh=False)))

    def test_list_filters_by_state_across_project(self):
        tasks.new_task(KEY, "T2-b", "Beta")
        issues.add(KEY, "Q", "question", "b")
        issues.add("proj/T1-a/T2-b", "F", "fyi", "b")
        code, out, _ = self.forge("issues", "list", "proj", "--state", "open", "--json")
        rows = json.loads(out)
        self.assertEqual([(r["task_key"], r["id"], r["state"]) for r in rows], [("proj/T1-a/T2-b", "I-1", "open")])
        self.assertEqual(len(issues.list_issues("proj")), 2)
        self.assertEqual(self.forge("issues", "list", "proj", "--state", "bogus")[0], 2)

    def test_migration_on_load_and_render_all_persists(self):
        path = state.items_file(KEY, "issues")
        old = [{"id": "I-1", "title": "Q", "severity": "question", "status": "open", "file": "issues/I-1.md",
                "artifact_url": None, "created": "2026-09-01T00:00:00Z", "updated": "2026-09-01T00:00:00Z",
                "resolution": ""},
               {"id": "I-2", "title": "F", "severity": "fyi", "status": "open", "file": "issues/I-2.md",
                "created": "2026-09-02T00:00:00Z", "updated": "2026-09-02T00:00:00Z", "resolution": ""},
               {"id": "I-3", "title": "B", "severity": "blocker", "status": "wontfix", "file": "issues/I-3.md",
                "created": "2026-09-03T00:00:00Z", "updated": "2026-09-03T00:00:00Z", "resolution": "no"}]
        path.write_text(json.dumps({"schema": 1, "items": old}))
        loaded = state.load_json_items(KEY, "issues")
        self.assertEqual([i["state"] for i in loaded], ["awaiting-user", "open", "wontfix"])
        self.assertEqual(loaded[0]["history"], [{"state": "awaiting-user", "at": "2026-09-01T00:00:00Z",
                                                 "by": "reviewer", "note": "migrated", "comment_id": None}])
        self.assertNotIn("state", json.loads(path.read_text())["items"][0])  # read-only until saved
        code, _, err = self.forge("render", "--all")
        self.assertEqual(code, 0, err)
        stored = json.loads(path.read_text())["items"]
        self.assertEqual([(i["state"], i["status"]) for i in stored],
                         [("awaiting-user", "open"), ("open", "open"), ("wontfix", "wontfix")])
        self.assertFalse(issues.migrate_file(KEY))

    def test_rendered_issues_status_and_decisions(self):
        issues.add(KEY, "Pick a DB", "decision", "b")
        issues.add(KEY, "Need creds", "question", "b")
        issues.decide(KEY, "I-1", "Use Postgres", notify=False)
        issues.set_state(KEY, "I-1", "in-progress")
        issues.resolve(KEY, "I-1", "migrated to Postgres")
        d = state.task_dir(KEY)
        issues_md = (d / "issues.md").read_text()
        self.assertIn("| ID | Severity | State | Status | Title | Decision | Artifact | Resolution |", issues_md)
        self.assertIn("| I-1 | decision | resolved | resolved | [Pick a DB](issues/I-1.md) | Use Postgres (user, ",
                      issues_md)
        self.assertIn("| Issues by state | **1 awaiting-user** · 1 resolved |", (d / "status.md").read_text())
        proj = state.project_dir("proj")
        self.assertIn("| **1** | **1 awaiting-user** · 1 resolved |", (proj / "status.md").read_text())
        dec = (proj / "decisions.md").read_text()
        wait, log = dec.split("## Decision log")
        self.assertIn("| T1-a | [I-2](tasks/T1-a/issues/I-2.md) | question | Need creds |", wait)
        self.assertIn("| T1-a | [I-1](tasks/T1-a/issues/I-1.md) | decision | Pick a DB: Use Postgres | user | "
                      "resolved | migrated to Postgres |", log)
        self.assertEqual(render.render_decisions("proj"), dec)

    def test_ui_api_decide_state_and_filters(self):
        issues.add(KEY, "Pick", "decision", "b")
        issues.add(KEY, "F", "fyi", "b")
        be = server.Backend(nudge_enabled=False)
        r = server.api_post_issue_decide(be, {}, {"key": KEY, "id": "I-1", "decision": " A "})
        self.assertEqual((r["issue"]["state"], r["delivery"]["mode"]), ("decided", "queued"))
        self.assertIn("delivery disabled", r["delivery"]["detail"])
        with self.assertRaises(server.ApiError) as ctx:
            server.api_post_issue_state(be, {}, {"key": KEY, "id": "I-1", "state": "open"})
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(server.ApiError) as ctx:
            server.api_post_issue_state(be, {}, {"key": KEY, "id": "I-9", "state": "open"})
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(server.ApiError) as ctx:
            server.api_post_issue_decide(be, {}, {"key": KEY, "id": "I-1", "decision": ""})
        self.assertEqual(ctx.exception.status, 400)
        done = server.api_post_issue_state(be, {}, {"key": KEY, "id": "I-2", "state": "wontfix", "note": "n/a"})
        self.assertEqual((done["state"], done["history"][-1]["by"]), ("wontfix", "user"))
        ids = lambda st: [i["id"] for i in server.api_issues(be, {"state": [st]}, {})]  # noqa: E731
        self.assertEqual((ids("active"), ids("all"), ids("decided")), (["I-1"], ["I-1", "I-2"], ["I-1"]))
        page = server.api_issue(be, {"key": [KEY], "id": ["I-1"]}, {})
        self.assertEqual(page["next_states"], ["awaiting-user", "in-progress", "resolved", "wontfix"])
        self.assertEqual(page["issue"]["decision"]["comment_id"], "C-1")
        self.assertEqual([m["id"] for m in page["thread"]], ["C-1"])
        self.assertEqual(server.fs_kind(state.project_dir("proj") / "decisions.md"), "rendered")
