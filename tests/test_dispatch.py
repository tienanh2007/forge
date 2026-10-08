from helpers import ForgeTestCase
from forge_lib import dispatch, state, tasks


class DispatchTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project(max_parallel=2)
        tasks.new_task("proj", "T1-a", "A")
        tasks.new_task("proj", "T2-b", "B", depends_on=["proj/T1-a"])
        self.key = "proj/T1-a"
        self.sid = "11111111-2222-3333-4444-555555555555"
        self.prompt = (f"You are the forge worker for task proj/T1-a. Task folder: {state.task_dir(self.key)}. "
                       "Invoke the forge:work skill and follow it.")

    def test_dry_run_first_dispatch(self):
        r = dispatch.dispatch(self.key, dry_run=True)
        self.assertEqual(r.code, 0)
        self.assertEqual(r.cmd, ["claude", "--bg", "--permission-mode", "auto", "--settings", '{"crossSessionInbound": "accept"}', "-n", "forge-proj-T1-a", "-w", "forge-proj-T1-a", self.prompt])
        self.assertEqual(r.cwd, str(self.repo.resolve()))
        self.assertEqual(self.claude_calls(), [])
        self.assertIsNone(state.load_task(self.key)["session"])

    def test_first_dispatch_runs_claude_and_resolves_real_session_id(self):
        # `claude --bg` assigns its own session id; forge picks the newest agent with the task's name.
        self.set_agents([{"id": "old", "sessionId": "old-sid", "name": "forge-proj-T1-a", "startedAt": 1},
                         {"id": "1111", "sessionId": self.sid, "name": "forge-proj-T1-a", "startedAt": 2},
                         {"id": "zzz", "sessionId": "other", "name": "unrelated", "startedAt": 3}])
        r = dispatch.dispatch(self.key, message="Focus on the DAO.")
        self.assertEqual(r.code, 0, r.output)
        calls = self.claude_calls()
        self.assertEqual(calls[0]["argv"], ["--bg", "--permission-mode", "auto", "--settings", '{"crossSessionInbound": "accept"}', "-n", "forge-proj-T1-a", "-w", "forge-proj-T1-a",
                                            self.prompt + "\n\nFocus on the DAO."])
        self.assertEqual(calls[0]["cwd"], str(self.repo.resolve()))
        self.assertEqual(calls[1]["argv"], ["agents", "--json", "--all"])
        t = state.load_task(self.key)
        self.assertEqual(t["session"], {"id": self.sid, "bg_id": "1111", "name": "forge-proj-T1-a",
                                        "worktree": "forge-proj-T1-a"})
        self.assertEqual(t["state"], "dispatched")

    def test_refuses_on_deps(self):
        r = dispatch.dispatch("proj/T2-b", dry_run=True)
        self.assertEqual(r.code, 1)
        self.assertIn("proj/T1-a (scoped)", r.output)
        self.assertEqual(dispatch.dispatch("proj/T2-b", dry_run=True, force=True).code, 0)
        tasks.set_state(self.key, "handed-back", allow_handback=True)
        self.assertEqual(dispatch.dispatch("proj/T2-b", dry_run=True).code, 0)

    def test_refuses_on_max_parallel(self):
        tasks.new_task("proj", "T3-c", "C")
        tasks.new_task("proj", "T4-d", "D")
        tasks.set_state("proj/T3-c", "in-progress")
        tasks.set_state("proj/T4-d", "dispatched")
        r = dispatch.dispatch(self.key, dry_run=True)
        self.assertEqual(r.code, 1)
        self.assertIn("max_parallel=2", r.output)
        self.assertEqual(dispatch.dispatch(self.key, dry_run=True, force=True).code, 0)
        tasks.set_state("proj/T4-d", "blocked")
        self.assertEqual(dispatch.dispatch(self.key, dry_run=True).code, 0)

    def _with_session(self):
        tasks.merge(self.key, {"session": {**dispatch.session_for(self.key), "id": self.sid}})
        tasks.set_state(self.key, "in-progress")

    def test_redispatch_active_writes_inbox(self):
        self._with_session()
        # "done" + idle = turn finished, process alive: still active (resuming would fork it).
        self.set_agents([{"sessionId": self.sid, "name": "forge-proj-T1-a", "state": "done", "status": "idle"}])
        r = dispatch.dispatch(self.key, message="new info")
        self.assertEqual((r.code, r.output), (3, "ACTIVE: use SendMessage to forge-proj-T1-a"))
        msgs = state.load_json_items(self.key, "inbox")
        self.assertEqual([(m["author"], m["body"], m["status"]) for m in msgs], [("coordinator", "new info", "open")])
        self.assertEqual([c["argv"] for c in self.claude_calls()], [["agents", "--json"]])
        self.assertTrue(dispatch.is_active({"id": self.sid}))

    def test_permission_mode_default_omits_flag_and_custom_is_passed(self):
        project = state.load_project("proj")
        project["permission_mode"] = "default"
        state.save("proj", project)
        self.assertEqual(dispatch.dispatch(self.key, dry_run=True).cmd[:5], ["claude", "--bg", "--settings", '{"crossSessionInbound": "accept"}', "-n"])
        project["permission_mode"] = "bypassPermissions"
        state.save("proj", project)
        self.assertEqual(dispatch.dispatch(self.key, dry_run=True).cmd[:4],
                         ["claude", "--bg", "--permission-mode", "bypassPermissions"])

    def test_redispatch_inactive_resumes(self):
        self._with_session()
        self.set_agents([{"sessionId": "someone-else", "state": "working"}])
        r = dispatch.dispatch(self.key)
        self.assertEqual(r.code, 0, r.output)
        self.assertEqual([c["argv"] for c in self.claude_calls() if c["argv"][0] == "--bg"][-1],
                         ["--bg", "--permission-mode", "auto", "--settings", '{"crossSessionInbound": "accept"}', "-n", "forge-proj-T1-a", "--resume", self.sid, "Resume task proj/T1-a; check inbox."])
        self.assertEqual(state.load_task(self.key)["state"], "in-progress")
        self.assertEqual(state.load_json_items(self.key, "inbox"), [])

    def test_message_requires_session_and_resumes(self):
        from forge_lib.errors import UsageError
        with self.assertRaises(UsageError):
            dispatch.dispatch(self.key, "hi", require_session=True)
        self._with_session()
        tasks.set_state(self.key, "blocked")
        r = dispatch.dispatch(self.key, "answer: use postgres", require_session=True)
        self.assertEqual(r.code, 0)
        self.assertEqual([c["argv"] for c in self.claude_calls()
                          if c["argv"][0] == "--bg"][-1], ["--bg", "--permission-mode", "auto", "--settings", '{"crossSessionInbound": "accept"}', "-n", "forge-proj-T1-a", "--resume", self.sid,
                                                           "answer: use postgres"])
        self.assertEqual(state.load_task(self.key)["state"], "dispatched")

    def test_sessions_join(self):
        self._with_session()
        self.set_agents([{"sessionId": self.sid, "name": "w", "state": "failed"},
                         {"sessionId": "x", "name": "other"}])
        rows = dispatch.sessions()
        self.assertEqual([(r["sessionId"], r["task_key"]) for r in rows], [(self.sid, "proj/T1-a"), ("x", None)])
        self.assertEqual(self.claude_calls()[-1]["argv"], ["agents", "--json", "--all"])
        self.assertFalse(dispatch.is_active({"id": self.sid}))
