import json
import os
import subprocess
from unittest import mock

from helpers import ForgeTestCase
from forge_lib.errors import IntegrationError, UsageError
from forge_lib.integrations import clickup, github, sonar

GQL = {"data": {"repository": {"pullRequest": {
    "url": "https://github.com/o/r/pull/5", "title": "Add DAO", "state": "OPEN", "isDraft": True,
    "baseRefName": "main", "headRefName": "feat", "reviewDecision": "REVIEW_REQUIRED",
    "updatedAt": "2026-09-25T10:00:00Z", "comments": {"totalCount": 3},
    "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
        {"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "b"},
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": "l"},
        {"__typename": "CheckRun", "name": "it", "status": "IN_PROGRESS", "conclusion": None, "detailsUrl": "i"},
        {"__typename": "StatusContext", "context": "sonar", "state": "PENDING", "targetUrl": "s"},
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR", "targetUrl": "e"},
    ]}}}}]},
    "reviewThreads": {"nodes": [
        {"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.java", "line": 3,
         "comments": {"nodes": [{"author": {"login": "rev"}, "body": "fix", "createdAt": "c", "url": "cu"}]}},
        {"id": "T2", "isResolved": True, "isOutdated": True, "path": "b.java", "line": None,
         "comments": {"nodes": [{"author": None, "body": "x", "createdAt": "c", "url": "u"}]}},
    ]},
}}}}


def proc(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class GithubTest(ForgeTestCase):
    def test_parse_pr_url(self):
        self.assertEqual(github.parse_pr_url("https://github.com/o/r/pull/12/files"), ("o/r", 12))
        with self.assertRaises(UsageError):
            github.parse_pr_url("https://gitlab.com/o/r/pull/1")

    @mock.patch("forge_lib.integrations.github.util.run")
    def test_pr_status_mapping(self, run):
        run.return_value = proc(json.dumps(GQL))
        s = github.pr_status("o/r", 5)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["gh", "api", "graphql", "-f"])
        self.assertTrue(cmd[4].startswith("query="))
        self.assertIn("statusCheckRollup", cmd[4])
        self.assertEqual(cmd[5:], ["-F", "owner=o", "-F", "name=r", "-F", "number=5"])
        self.assertEqual(s["checks"], {"state": "FAILURE", "total": 5,
                                       "failing": [{"name": "lint", "url": "l"}, {"name": "ci/legacy", "url": "e"}],
                                       "pending": [{"name": "it"}, {"name": "sonar"}],
                                       "names": ["build", "lint", "it", "sonar", "ci/legacy"]})
        self.assertEqual({k: s["threads"][k] for k in ("total", "resolved", "unresolved")},
                         {"total": 2, "resolved": 1, "unresolved": 1})
        self.assertEqual(s["threads"]["items"][0], {"id": "T1", "path": "a.java", "line": 3, "is_resolved": False,
                                                    "is_outdated": False, "comments": [
                                                        {"author": "rev", "body": "fix", "at": "c", "url": "cu"}]})
        self.assertIsNone(s["threads"]["items"][1]["comments"][0]["author"])
        self.assertEqual((s["state"], s["is_draft"], s["base"], s["head"], s["review_decision"], s["comments_count"]),
                         ("OPEN", True, "main", "feat", "REVIEW_REQUIRED", 3))

    @mock.patch("forge_lib.integrations.github.util.run")
    def test_checks_states(self, run):
        pr = json.loads(json.dumps(GQL))
        node = pr["data"]["repository"]["pullRequest"]
        node["commits"]["nodes"] = []
        run.return_value = proc(json.dumps(pr))
        self.assertEqual(github.pr_status("o/r", 5)["checks"]["state"], "NONE")
        node["commits"]["nodes"] = [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "b", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"__typename": "StatusContext", "context": "c", "state": "SUCCESS"}]}}}}]
        run.return_value = proc(json.dumps(pr))
        self.assertEqual(github.pr_status("o/r", 5)["checks"]["state"], "SUCCESS")

    @mock.patch("forge_lib.integrations.github.util.run")
    def test_errors(self, run):
        run.return_value = proc("", 1, "HTTP 401")
        with self.assertRaises(IntegrationError):
            github.pr_status("o/r", 5)
        run.return_value = proc(json.dumps({"data": {"repository": {"pullRequest": None}}}))
        with self.assertRaises(IntegrationError):
            github.pr_status("o/r", 5)


class SonarTest(ForgeTestCase):
    def test_requires_token(self):
        with self.assertRaises(IntegrationError):
            sonar.quality_gate("k", 1)

    @mock.patch("forge_lib.integrations.sonar.util.http_request")
    def test_ok_and_not_found(self, http):
        os.environ["SONAR_TOKEN"] = "sekrit-token"
        http.return_value = (200, json.dumps({"projectStatus": {"status": "ERROR", "conditions": [
            {"status": "ERROR", "metricKey": "new_coverage", "actualValue": "10", "errorThreshold": "80"}]}}))
        r = sonar.quality_gate("my_key", 9)
        method, url = http.call_args[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://sonarcloud.io/api/qualitygates/project_status?projectKey=my_key&pullRequest=9")
        self.assertEqual(http.call_args[1]["headers"]["Authorization"], "Basic c2Vrcml0LXRva2VuOg==")
        self.assertEqual(r, {"status": "ERROR", "url": "https://sonarcloud.io/summary/new_code?id=my_key&pullRequest=9",
                             "conditions": [{"metric": "new_coverage", "status": "ERROR", "actual": "10",
                                             "threshold": "80"}]})
        http.return_value = (404, '{"errors":[{"msg":"Pull request not found"}]}')
        self.assertEqual(sonar.quality_gate("my_key", 9)["status"], "NONE")
        http.return_value = (500, "boom sekrit-token")
        with self.assertRaises(IntegrationError) as ctx:
            sonar.quality_gate("my_key", 9)
        self.assertNotIn("sekrit-token", str(ctx.exception))

    @mock.patch("forge_lib.integrations.sonar.util.http_request")
    def test_token_from_env_file(self, http):
        (self.tmp / "no-env").write_text("# c\nexport SONAR_TOKEN='filetok'\n")
        http.return_value = (200, json.dumps({"projectStatus": {"status": "OK"}}))
        self.assertEqual(sonar.quality_gate("k", 1)["status"], "OK")
        self.assertEqual(http.call_args[1]["headers"]["Authorization"], "Basic ZmlsZXRvazo=")


class ClickupTest(ForgeTestCase):
    def setUp(self):
        super().setUp()
        os.environ["CLICKUP_TOKEN"] = "pk_secret"

    @mock.patch("forge_lib.integrations.clickup.util.http_request")
    def test_get_task_raw_and_custom(self, http):
        http.return_value = (200, json.dumps({"id": "86abc"}))
        clickup.get_task("86abc")
        self.assertEqual(http.call_args[0], ("GET", "https://api.clickup.com/api/v2/task/86abc?include_subtasks=true"))
        self.assertEqual(http.call_args[1]["headers"], {"Authorization": "pk_secret"})

        http.side_effect = [(200, json.dumps({"teams": [{"id": 111}, {"id": 222}]})), (200, "{}")]
        clickup.get_task("https://app.clickup.com/t/999/ENG-123")
        urls = [c[0][1] for c in http.call_args_list[-2:]]
        self.assertEqual(urls, ["https://api.clickup.com/api/v2/team",
                                "https://api.clickup.com/api/v2/task/ENG-123?include_subtasks=true"
                                "&custom_task_ids=true&team_id=111"])
        http.side_effect = None
        os.environ["CLICKUP_TEAM_ID"] = "555"
        http.return_value = (200, "{}")
        clickup.get_task("ENG-9", include_subtasks=False)
        self.assertEqual(http.call_args[0][1], "https://api.clickup.com/api/v2/task/ENG-9?include_subtasks=false"
                                               "&custom_task_ids=true&team_id=555")

    @mock.patch("forge_lib.integrations.clickup.util.http_request")
    def test_create_and_errors(self, http):
        http.return_value = (200, json.dumps({"id": "new"}))
        clickup.create_task("L1", "Name", "**md**", parent="86abc")
        self.assertEqual(http.call_args[0], ("POST", "https://api.clickup.com/api/v2/list/L1/task"))
        self.assertEqual(json.loads(http.call_args[1]["body"]),
                         {"name": "Name", "markdown_description": "**md**", "parent": "86abc"})
        http.return_value = (401, "bad token pk_secret")
        with self.assertRaises(IntegrationError) as ctx:
            clickup.get_task("86abc")
        self.assertNotIn("pk_secret", str(ctx.exception))

    def test_task_summary(self):
        s = clickup.task_summary({"id": "1", "custom_id": "ENG-1", "name": "P", "status": {"status": "open"},
                                  "url": "u", "assignees": [{"username": "tien"}, {"email": "a@b"}],
                                  "subtasks": [{"id": "2", "name": "c", "status": {"status": "done"}}]})
        self.assertEqual(s["assignees"], ["tien", "a@b"])
        self.assertEqual(s["subtasks"][0], {"id": "2", "custom_id": None, "name": "c", "status": "done",
                                            "url": None, "assignees": [], "subtasks": []})
