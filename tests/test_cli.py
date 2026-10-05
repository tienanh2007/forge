import json
from unittest import mock

from helpers import ForgeTestCase
from forge_lib import state


class CliTest(ForgeTestCase):
    def test_end_to_end(self):
        code, out, err = self.forge("init-project", "proj", "--title", "P", "--repo", str(self.repo), "--github", "o/r")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.forge("new-task", "proj", "T1-a", "--title", "A")[1].strip(), "proj/T1-a")
        self.assertEqual(self.forge("new-task", "proj/T1-a", "T2-b", "--title", "B", "--depends-on",
                                    "proj/T1-a")[0], 0)
        self.assertEqual(self.forge("tests", "add", "proj/T1-a", "--name", "n", "--type", "unit")[1].strip(), "TST-1")
        self.assertEqual(self.forge("tests", "set", "proj/T1-a", "TST-1", "--status", "green")[0], 0)
        self.assertEqual(self.forge("issues", "add", "proj/T1-a", "--title", "Q", "--severity", "question",
                                    "--body", "why?")[1].strip(), "I-1")
        self.assertEqual(self.forge("inbox", "add", "proj/T1-a", "--body", "hi", "--issue", "I-1")[1].strip(), "C-1")
        code, out, _ = self.forge("inbox", "pending", "--json")
        self.assertEqual(json.loads(out)[0]["task_key"], "proj/T1-a")
        code, out, _ = self.forge("tree", "proj", "--json")
        self.assertEqual(json.loads(out)["counts"]["open_inbox"], 1)
        self.assertIn("proj/T1-a/T2-b [scoped]", self.forge("tree")[1])
        self.assertEqual(self.forge("path", "proj/T1-a/T2-b")[1].strip(),
                         str(self.home / "proj" / "tasks" / "T1-a" / "tasks" / "T2-b"))
        code, out, _ = self.forge("gate", "proj/T1-a", "--json")
        self.assertEqual(code, 1)
        self.assertIn("no PRs recorded in prs.json", json.loads(out)["reasons"])
        code, out, _ = self.forge("dispatch", "proj/T1-a/T2-b", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", out)
        code, out, _ = self.forge("dispatch", "proj/T1-a", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("claude --bg -n forge-proj-T1-a -w forge-proj-T1-a 'You are the forge worker"))
        self.assertEqual(self.forge("set", "proj/T1-a", "--merge", '{"session":{"id":"S1","name":"n"}}')[0], 0)
        self.assertEqual(self.forge("task-for-session", "S1")[1].strip(), "proj/T1-a")
        self.assertEqual(self.forge("task-for-session", "S2")[0], 1)
        self.assertEqual(self.forge("show", "proj/T1-a", "--json")[0], 0)
        self.assertEqual(self.forge("set-state", "proj/T1-a", "handed-back")[0], 1)
        self.assertEqual(self.forge("home")[1].strip(), str(self.home))
        code, _, err = self.forge("show", "proj/nope")
        self.assertEqual(code, 1)
        self.assertIn("task not found", err)

    def test_hook_never_crashes(self):
        code, out, err = self.forge("hook", "stop", stdin="not json{")
        self.assertEqual((code, out, err), (0, "", ""))
        log = state.cache_dir() / "hook-errors.log"
        self.assertIn("JSONDecodeError", log.read_text())
        with mock.patch("forge_lib.hooks.session_start", side_effect=RuntimeError("kaboom")):
            self.assertEqual(self.forge("hook", "session-start", stdin="{}"), (0, "", ""))
        self.assertIn("kaboom", log.read_text())
        self.assertEqual(self.forge("hook", "stop", stdin='{"session_id":"x"}'), (0, "", ""))

    def test_hook_stop_blocks_via_cli(self):
        self.make_project()
        self.forge("new-task", "proj", "T1-a", "--title", "A")
        self.forge("set", "proj/T1-a", "--merge", '{"session":{"id":"S1","name":"n"}}')
        self.forge("inbox", "add", "proj/T1-a", "--body", "hello")
        code, out, _ = self.forge("hook", "stop", stdin='{"session_id":"S1","stop_hook_active":false}')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_ui_missing(self):
        with mock.patch("forge_lib.cli.REPO_ROOT", self.tmp):
            code, _, err = self.forge("ui")
        self.assertEqual(code, 1)
        self.assertIn("UI not installed", err)


class AdoptTest(ForgeTestCase):
    def test_scan(self):
        parent = {"id": "p1", "custom_id": "ENG-100", "name": "Billing V2", "url": "pu", "status": {"status": "open"},
                  "subtasks": [{"id": "s1", "custom_id": "ENG-101", "name": "API layer!", "url": "su",
                                "status": {"status": "in progress"}},
                               {"id": "s2", "name": "No custom id"}]}
        self.set_agents([{"sessionId": "a1", "name": "eng-101 work", "cwd": "/x"}, {"sessionId": "a2", "name": "z"}])
        gh_prs = {"ENG-100 in:title": [], "ENG-101 in:title": [{"number": 4, "url": "u4"}]}
        with mock.patch("forge_lib.adopt.clickup.get_task", return_value=parent) as gt, \
                mock.patch("forge_lib.adopt.github.search_prs", side_effect=lambda r, q: gh_prs[q]) as sp:
            code, out, err = self.forge("adopt-scan", "--clickup-parent", "ENG-100", "--github", "o/r",
                                        "--repo", str(self.repo))
        self.assertEqual(code, 0, err)
        gt.assert_called_once_with("ENG-100", include_subtasks=True)
        self.assertEqual(sp.call_count, 2)
        doc = json.loads(out)
        self.assertEqual(doc["project"]["slug"], "eng-100-billing-v2")
        self.assertEqual(doc["project"]["clickup_parent"], {"id": "p1", "custom_id": "ENG-100", "url": "pu"})
        self.assertEqual([t["task_dir"] for t in doc["tasks"]], ["ENG-101-api-layer", "T2-no-custom-id"])
        self.assertEqual(doc["tasks"][0]["prs"], [{"number": 4, "url": "u4"}])
        self.assertEqual([s["sessionId"] for s in doc["tasks"][0]["candidate_sessions"]], ["a1"])
        self.assertEqual(len(doc["sessions"]), 2)
        self.assertFalse(self.home.exists())
