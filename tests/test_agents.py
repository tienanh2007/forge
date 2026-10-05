import json
import os

from helpers import ForgeTestCase
from forge_lib import agents, state, tasks
from forge_lib.errors import UsageError

SID = "aaaaaaaa-1111-2222-3333-444444444444"


def rows_jsonl(rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


class TranscriptTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.projects = self.tmp / "claude-projects"
        self.cwd = "/Users/x/src/my-service/.claude/worktrees/forge-a"

    def write(self, dirname, rows, sid=SID):
        d = self.projects / dirname
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text(rows_jsonl(rows))
        return p

    def test_mangle(self):
        self.assertEqual(agents.mangle(self.cwd), "-Users-x-src-my-service--claude-worktrees-forge-a")

    def test_path_prefers_cwd_then_newest_glob(self):
        self.assertIsNone(agents.transcript_path(SID, self.cwd))
        other = self.write("-somewhere-else", [])
        self.assertEqual(agents.transcript_path(SID, self.cwd), other)
        exact = self.write(agents.mangle(self.cwd), [])
        os.utime(exact, (1, 1))  # older than the glob match, but cwd wins
        self.assertEqual(agents.transcript_path(SID, self.cwd), exact)
        self.assertIsNone(agents.transcript_path("../etc", self.cwd))

    def test_tail_parses_roles_kinds_and_skips_noise(self):
        long = "x" * 5000
        self.write(agents.mangle(self.cwd), [
            {"type": "file-history-snapshot"},
            {"type": "user", "timestamp": "t1", "message": {"content": "hello worker"}},
            {"type": "user", "isMeta": True, "timestamp": "t1", "message": {"content": "caveat"}},
            {"type": "assistant", "timestamp": "t2", "message": {"content": [
                {"type": "thinking", "thinking": "secret"},
                {"type": "text", "text": long},
                {"type": "tool_use", "id": "tu1", "name": "Bash",
                 "input": {"command": "ls -la\n  /tmp", "description": "list"}}]}},
            {"type": "user", "timestamp": "t3", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": [{"type": "text", "text": "y" * 900}],
                 "is_error": True}]}},
            {"type": "user", "timestamp": "t4", "message": {"content":
                "Another Claude session sent a message:\nplease rebase"}},
            {"type": "system", "timestamp": "t5", "content": "compacted"},
            {"type": "user", "isMeta": True, "timestamp": "t6", "message": {"content":
                'Another Claude session sent a message:\n<cross-session-message from="uds:/x" from-name="coord-1" '
                'from-mode="prompting">\nforge inbox k C-1: hi\n</cross-session-message>'}},
            {"type": "user", "isMeta": True, "timestamp": "t7", "message": {"content": "Stop hook feedback:\nack C-1"}},
        ])
        e = agents.transcript_tail(SID, self.cwd)
        self.assertEqual([(x["role"], x["kind"]) for x in e], [
            ("user", "text"), ("assistant", "text"), ("assistant", "tool_use"), ("assistant", "tool_result"),
            ("cross", "text"), ("system", "text"), ("cross", "text"), ("system", "text")])
        self.assertEqual((e[6]["text"], e[6]["sender"]), ("forge inbox k C-1: hi", "coord-1"))
        self.assertEqual(e[7]["text"], "Stop hook feedback:\nack C-1")
        self.assertEqual(e[0], {"at": "t1", "role": "user", "kind": "text", "text": "hello worker",
                                "tool": None, "is_error": False})
        self.assertTrue(e[1]["text"].startswith("x" * 2000) and "[+3000 chars]" in e[1]["text"])
        self.assertEqual((e[2]["tool"], e[2]["text"]), ("Bash", "ls -la /tmp"))
        self.assertEqual((e[3]["tool"], e[3]["is_error"]), ("Bash", True))
        self.assertTrue(e[3]["text"].startswith("y" * 400) and "[+500 chars]" in e[3]["text"])
        self.assertEqual(e[4]["text"], "please rebase")
        self.assertNotIn("secret", json.dumps(e))

    def test_tail_reads_only_the_end_of_large_files(self):
        rows = [{"type": "user", "timestamp": f"t{i}", "message": {"content": f"msg {i} " + "p" * 200}}
                for i in range(3000)]
        self.write(agents.mangle(self.cwd), rows)
        e = agents.transcript_tail(SID, self.cwd, limit=5)
        self.assertEqual([x["at"] for x in e], [f"t{i}" for i in range(2995, 3000)])


class SendAndOverviewTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        self.make_project(max_parallel=2)
        tasks.new_task("proj", "T1-a", "A")
        tasks.new_task("proj", "T2-b", "B")
        self.key = "proj/T1-a"
        self.session = {"id": SID, "bg_id": "aaaa", "name": "forge-proj-T1-a", "worktree": "forge-proj-T1-a"}
        tasks.merge(self.key, {"session": self.session, "state": "in-progress"})
        tasks.merge("proj", {"coordinator_session": {"id": "coord-sid", "name": "past-coord"}})

    def live(self, **extra):
        self.set_agents([{"id": "aaaa", "sessionId": SID, "name": "forge-proj-T1-a", "kind": "background",
                          "state": "working", "status": "busy", "cwd": "/w", "startedAt": 1, **extra}])

    def bridge_calls(self):
        return [c for c in self.claude_calls() if c["argv"][:1] == ["-p"]]

    def test_send_live_uses_bridge_and_marks_delivered(self):
        self.live()
        r = agents.send(self.key, "Please rebase on main")
        self.assertEqual((r["mode"], r["delivered"]), ("sent", True))
        call = self.bridge_calls()[0]
        self.assertEqual(call["argv"], agents.BRIDGE_CMD[1:])
        self.assertEqual(call["cwd"], str(self.repo.resolve()))
        self.assertIn("session named 'forge-proj-T1-a'", call["stdin"])
        self.assertIn("forge inbox proj/T1-a C-1: Please rebase on main", call["stdin"])
        msg = state.load_json_items(self.key, "inbox")[0]
        self.assertEqual((msg["author"], msg["body"], msg["delivered"]), ("user", "Please rebase on main", True))

    def test_send_live_bridge_failure_leaves_message_for_relay(self):
        self.live()
        os.environ["FAKE_CLAUDE_BRIDGE_OUT"] = "FAILED: no such session"
        r = agents.send(self.key, "hi")
        self.assertEqual((r["mode"], r["delivered"], r["detail"]), ("queued", False, "FAILED: no such session"))
        self.assertFalse(state.load_json_items(self.key, "inbox")[0]["delivered"])

    def test_send_not_live_resumes(self):
        self.set_agents([])
        r = agents.send(self.key, "hi")
        self.assertEqual((r["mode"], r["delivered"]), ("resumed", True))
        resume = [c for c in self.claude_calls() if "--resume" in c["argv"]][0]
        self.assertEqual(resume["argv"][:7], ["--bg", "--permission-mode", "auto", "-n", "forge-proj-T1-a", "--resume", SID])
        self.assertIn("forge inbox proj/T1-a C-1: hi", resume["argv"][7])
        self.assertEqual(self.bridge_calls(), [])

    def test_send_without_session_refuses(self):
        with self.assertRaises(UsageError):
            agents.send("proj/T2-b", "hi")
        self.assertEqual(state.load_json_items("proj/T2-b", "inbox"), [])

    def test_send_to_coordinator_matches_by_name(self):
        self.set_agents([{"id": None, "sessionId": "other-id", "name": "past-coord", "kind": "interactive",
                          "status": "idle"}])
        r = agents.send("proj", "status?")
        self.assertEqual((r["mode"], r["delivered"]), ("sent", True))
        self.assertIn("'past-coord'", self.bridge_calls()[0]["stdin"])
        self.set_agents([])
        with self.assertRaises(UsageError):
            agents.send("proj", "status?")

    def test_overview_joins_live_info_and_transcript(self):
        self.live()
        d = self.tmp / "claude-projects" / agents.mangle("/w")
        d.mkdir(parents=True)
        (d / f"{SID}.jsonl").write_text(rows_jsonl([{"type": "user", "timestamp": "t", "message": {"content": "x"}}]))
        tasks.merge("proj/T2-b", {})
        rows = {r["key"]: r for r in agents.overview()}
        self.assertEqual(list(rows), ["proj", "proj/T1-a", "proj/T2-b"])
        self.assertEqual(rows["proj"]["kind"], "coordinator")
        self.assertFalse(rows["proj"]["live"]["live"])
        t1 = rows["proj/T1-a"]
        self.assertEqual((t1["live"]["live"], t1["live"]["state"], t1["attach"]), (True, "working", "claude attach aaaa"))
        self.assertTrue(t1["last_activity"])
        self.assertEqual(agents.detail(self.key)["entries"][0]["text"], "x")
        self.assertIsNone(rows["proj/T2-b"]["session"])

    def test_overview_tolerates_claude_agents_failure(self):
        (self.bin / "claude").write_text("#!/bin/sh\nexit 2\n")
        rows = agents.overview("proj")
        self.assertEqual([r["live"] for r in rows], [{"live": None}] * 3)

    def test_cli_send_and_agents(self):
        self.live()
        code, out, _ = self.forge("send", self.key, "hello")
        self.assertEqual(code, 0)
        self.assertIn("SENT live (C-1)", out)
        code, out, _ = self.forge("agents")
        self.assertEqual(code, 0)
        self.assertIn("proj/T1-a\tin-progress\tforge-proj-T1-a\tworking/busy", out)
        code, out, _ = self.forge("send", "proj/T2-b", "hello")
        self.assertEqual(code, 1)


class UtilRunStdinTest(ForgeTestCase):
    def test_child_does_not_inherit_stdin(self):
        # `claude` appends piped stdin to its prompt, so util.run must not pass ours through.
        import subprocess
        import sys
        from helpers import REPO_ROOT
        code = "import sys; sys.path.insert(0, %r); from forge_lib import util; print(repr(util.run(['cat']).stdout))" % str(REPO_ROOT)
        out = subprocess.run([sys.executable, "-c", code], input="leaked", capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "''")
