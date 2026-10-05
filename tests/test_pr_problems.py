import unittest

from helpers import ForgeTestCase  # noqa: F401  (puts the repo on sys.path)
from forge_lib import prs


def pr(number, base, head, base_oid="b0", head_oid="h0", checks=("build",), mergeable="MERGEABLE",
       draft=False, stack_index=0):
    return {"repo": "o/r", "number": number, "stack_index": stack_index, "status": {
        "base": base, "head": head, "base_oid": base_oid, "head_oid": head_oid, "mergeable": mergeable,
        "is_draft": draft, "threads": {"unresolved": 0},
        "checks": {"state": "SUCCESS", "names": list(checks), "failing": [], "pending": []}}}


class PrProblemsTest(unittest.TestCase):
    def test_green(self):
        self.assertEqual(prs.problems(pr(1, "main", "a"), False, ["build"]), [])

    def test_conflict_is_a_problem_even_when_checks_green(self):
        out = prs.problems(pr(1, "main", "a", mergeable="CONFLICTING"), False)
        self.assertEqual(out, ["PR o/r#1: merge conflict with main (CI may not run until resolved)"])

    def test_required_check_that_never_ran(self):
        # Draft PR where only the title check ran: rollup SUCCESS but the build job was skipped.
        item = pr(1, "main", "a", checks=("clickup_check / Validate PR title",), draft=True)
        self.assertEqual(prs.problems(item, False, ["build"]),
                         ["PR o/r#1: required check 'build' never ran (draft PRs may skip it)"])

    def test_stale_stack_detected_via_with_below(self):
        bottom = pr(1, "main", "a", head_oid="new", stack_index=0)
        top = pr(2, "a", "b", base_oid="old", stack_index=1)
        pairs = prs.with_below([top, bottom])
        self.assertEqual([(i["number"], b and b["number"]) for i, b in pairs], [(1, None), (2, 1)])
        self.assertEqual(prs.problems(top, False, None, bottom),
                         ["PR o/r#2: stale stack - based on an old a; restack onto o/r#1"])
        self.assertEqual(prs.problems(pr(2, "a", "b", base_oid="new", stack_index=1), False, None, bottom), [])


if __name__ == "__main__":
    unittest.main()
