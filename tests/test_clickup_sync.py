import json
import os
import sys
from unittest import mock

from helpers import REPO_ROOT, ForgeTestCase
from forge_lib import state, tasks
from forge_lib.errors import UsageError

sys.path.insert(0, str(REPO_ROOT / "ui"))
import server  # noqa: E402

KEY = "proj/ENG-2-b"
REF = {"id": "86b", "custom_id": "ENG-2", "url": "https://app.clickup.com/t/86b"}
LIST_STATUSES = {"statuses": [{"status": s} for s in
                              ("Planned", "in progress", "blocked", "review", "done", "cancelled", "QA")]}
PARENT = {"id": "86p", "custom_id": "ENG-1", "list": {"id": "L1"}, "subtasks": [
    {"id": "86b", "custom_id": "ENG-2", "url": "u2", "status": {"status": "planned"}},
    {"id": "86c", "custom_id": "ENG-3", "url": "u3", "status": {"status": "in progress"}},
    {"id": "86x", "custom_id": "ENG-22", "url": "u22"}]}
HTTP = "forge_lib.integrations.clickup.util.http_request"


def puts(http):
    return [(c[0][1], json.loads(c[1]["body"])) for c in http.call_args_list if c[0][0] == "PUT"]


class ClickupSyncTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project(clickup_parent="86p")
        tasks.new_task("proj", "ENG-2-b", "B")
        tasks.merge(KEY, {"clickup": REF})
        os.environ["CLICKUP_TOKEN"] = "pk_secret"

    @mock.patch(HTTP)
    def test_set_state_pushes_mapped_status_once(self, http):
        http.return_value = (200, "{}")
        t = tasks.set_state(KEY, "in-review")
        self.assertEqual(puts(http), [("https://api.clickup.com/api/v2/task/86b", {"status": "review"})])
        self.assertEqual((t["clickup_sync"]["status"], t["clickup_sync"]["error"]), ("review", None))
        self.assertEqual(state.load_task(KEY)["clickup_sync"], t["clickup_sync"])
        tasks.set_state(KEY, "handed-back", allow_handback=True)  # also "review": nothing to push
        self.assertEqual(len(puts(http)), 1)
        tasks.set_state(KEY, "done")
        self.assertEqual(puts(http)[-1][1], {"status": "done"})

    @mock.patch(HTTP)
    def test_no_push_when_unlinked_disabled_or_no_token(self, http):
        tasks.new_task("proj", "T9-x", "X")
        tasks.set_state("proj/T9-x", "in-progress")
        os.environ["FORGE_NO_CLICKUP"] = "1"
        tasks.set_state(KEY, "in-progress")
        del os.environ["FORGE_NO_CLICKUP"]
        del os.environ["CLICKUP_TOKEN"]
        tasks.set_state(KEY, "blocked")
        http.assert_not_called()
        self.assertNotIn("clickup_sync", state.load_task(KEY))

    @mock.patch(HTTP)
    def test_failure_is_recorded_not_raised_and_retried(self, http):
        http.return_value = (500, "boom pk_secret")
        t = tasks.set_state(KEY, "blocked", note="n")
        self.assertEqual(t["state"], "blocked")
        self.assertEqual(t["clickup_sync"]["status"], "blocked")
        self.assertIn("HTTP 500", t["clickup_sync"]["error"])
        log = (state.task_dir(KEY) / "log.md").read_text()
        self.assertIn("ClickUp status -> 'blocked' failed: ClickUp PUT /task/86b: HTTP 500", log)
        self.assertNotIn("pk_secret", log)
        http.side_effect = RuntimeError("network down")
        self.assertIn("network down", tasks.set_state(KEY, "blocked")["clickup_sync"]["error"])
        http.side_effect = None
        http.return_value = (200, "{}")
        self.assertIsNone(tasks.set_state(KEY, "blocked")["clickup_sync"]["error"])  # same status, but last failed
        self.assertEqual(len(puts(http)), 3)
        tasks.set_state(KEY, "blocked")
        self.assertEqual(len(puts(http)), 3)

    @mock.patch(HTTP)
    def test_project_map_overrides_default(self, http):
        tasks.merge("proj", {"clickup_status_map": {"in-review": "QA"}})
        http.return_value = (200, "{}")
        tasks.set_state(KEY, "in-review")
        tasks.set_state(KEY, "scoped")
        self.assertEqual([b["status"] for _, b in puts(http)], ["QA", "planned"])

    @mock.patch(HTTP)
    def test_link_cli(self, http):
        tasks.new_task("proj", "T5-e", "E")
        http.side_effect = [(200, json.dumps({"teams": [{"id": 7}]})),
                            (200, json.dumps({"id": "86e", "custom_id": "ENG-5", "url": "u5"}))]
        code, out, _ = self.forge("clickup", "link", "proj/T5-e", "https://app.clickup.com/t/7/ENG-5")
        self.assertEqual((code, out.strip()), (0, "proj/T5-e -> ENG-5 u5"))
        self.assertIn("/task/ENG-5?include_subtasks=false&custom_task_ids=true&team_id=7", http.call_args[0][1])
        self.assertEqual(state.load_task("proj/T5-e")["clickup"], {"id": "86e", "custom_id": "ENG-5", "url": "u5"})

    @mock.patch(HTTP)
    def test_link_subtasks_cli(self, http):
        tasks.new_task("proj", "ENG-3-cte-mask", "C")
        tasks.new_task("proj", "ENG-2", "exact name, no subtask match beyond ENG-2")
        tasks.new_task("proj", "misc", "M")
        http.return_value = (200, json.dumps(PARENT))
        code, out, _ = self.forge("clickup", "link-subtasks", "proj", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [
            {"key": "proj/ENG-2", "custom_id": "ENG-2", "action": "skipped (subtask linked to proj/ENG-2-b)"},
            {"key": "proj/ENG-2-b", "custom_id": "ENG-2", "action": "skipped (already linked)"},
            {"key": "proj/ENG-3-cte-mask", "custom_id": "ENG-3", "action": "linked"}])
        self.assertEqual(state.load_task("proj/ENG-3-cte-mask")["clickup"], {"id": "86c", "custom_id": "ENG-3",
                                                                            "url": "u3"})
        self.assertIsNone(state.load_task("proj/misc")["clickup"])
        self.assertIsNone(state.load_task("proj/ENG-2")["clickup"])
        self.assertEqual(http.call_args[0][1], "https://api.clickup.com/api/v2/task/86p?include_subtasks=true")

    @mock.patch(HTTP)
    def test_link_subtasks_skips_cancelled(self, http):
        tasks.new_task("proj", "ENG-3-old-long-name", "old")
        tasks.set_state("proj/ENG-3-old-long-name", "cancelled")
        tasks.new_task("proj", "ENG-3-new", "new")
        http.return_value = (200, json.dumps(PARENT))
        code, out, _ = self.forge("clickup", "link-subtasks", "proj", "--json")
        self.assertEqual(code, 0)
        rows = {r["key"]: r["action"] for r in json.loads(out)}
        self.assertEqual(rows["proj/ENG-3-old-long-name"], "skipped (cancelled)")
        self.assertEqual(rows["proj/ENG-3-new"], "linked")
        self.assertIsNone(state.load_task("proj/ENG-3-old-long-name")["clickup"])

    @mock.patch(HTTP)
    def test_sync_dry_run_does_not_write(self, http):
        tasks.set_state(KEY, "in-review")  # pushes "review"
        http.reset_mock()

        def respond(method, url, **kw):
            if "/list/L1" in url:
                return 200, json.dumps(LIST_STATUSES)
            if "/task/86p" in url:
                return 200, json.dumps(PARENT)
            return 200, json.dumps({"id": "86b", "status": {"status": "planned"}})
        http.side_effect = respond
        code, out, _ = self.forge("clickup", "sync", "proj", "--dry-run")
        self.assertEqual((code, out.strip()), (0, "proj/ENG-2-b\tENG-2\tplanned -> review"))
        self.assertTrue(all(c[0][0] == "GET" for c in http.call_args_list))
        code, out, _ = self.forge("clickup", "sync", KEY, "--dry-run", "--json")
        self.assertEqual(json.loads(out)[0]["current"], "planned")

        code, out, _ = self.forge("clickup", "sync", KEY)
        self.assertEqual((code, out.strip()), (0, "proj/ENG-2-b\tENG-2\treview\tok"))
        self.assertEqual(puts(http), [("https://api.clickup.com/api/v2/task/86b", {"status": "review"})])

    @mock.patch(HTTP)
    def test_sync_rejects_unknown_status(self, http):
        tasks.merge("proj", {"clickup_status_map": {"blocked": "stuck"}})
        http.side_effect = lambda m, url, **kw: (200, json.dumps(LIST_STATUSES if "/list/" in url else PARENT))
        code, _, err = self.forge("clickup", "sync", "proj", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("statuses not on list L1: stuck", err)
        self.assertEqual(puts(http), [])

    def test_link_subtasks_needs_parent(self):
        self.make_project("other")
        with self.assertRaises(UsageError):
            from forge_lib import clickup_sync
            clickup_sync.link_subtasks("other")


class UiTaskStateTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project()
        tasks.new_task("proj", "T1-a", "A")
        self.be = server.Backend(nudge_enabled=False)

    def post(self, **body):
        return server.api_post_task_state(self.be, {}, {"key": "proj/T1-a", **body})

    def test_sets_state_with_notes(self):
        r = self.post(state="blocked", note="")
        self.assertEqual((r["task"]["state"], r["task"]["state_history"][-1]["note"]), ("blocked", "set via UI"))
        self.assertFalse(r["clickup_pushed"])
        r = self.post(state="handed-back", note=" looks fine ")
        self.assertEqual(r["task"]["state"], "handed-back")
        self.assertEqual(r["task"]["state_history"][-1]["note"], "via UI (gate skipped): looks fine")
        self.assertEqual(self.post(state="handed-back")["task"]["state_history"][-1]["note"], "via UI (gate skipped)")
        self.assertIn("handed-back", server.api_task(self.be, {"key": ["proj/T1-a"]}, {})["task_states"])

    @mock.patch(HTTP)
    def test_returns_clickup_sync(self, http):
        tasks.merge("proj/T1-a", {"clickup": REF})
        os.environ["CLICKUP_TOKEN"] = "pk_secret"
        http.return_value = (200, "{}")
        r = self.post(state="in-review")
        self.assertTrue(r["clickup_pushed"])
        self.assertEqual((r["clickup_sync"]["status"], r["clickup_sync"]["error"]), ("review", None))

    def test_rejects_bad_input(self):
        for body, status in (({"state": "nope"}, 400), ({"state": 3}, 400), ({"key": "proj", "state": "done"}, 400),
                             ({"key": "proj/zz", "state": "done"}, 404)):
            with self.assertRaises(server.ApiError, msg=body) as ctx:
                server.api_post_task_state(self.be, {}, {"key": "proj/T1-a", **body})
            self.assertEqual(ctx.exception.status, status, body)
        self.assertIn("/api/task/state", server.POST_ROUTES)
