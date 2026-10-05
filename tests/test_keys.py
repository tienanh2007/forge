from pathlib import Path

from helpers import ForgeTestCase
from forge_lib import keys
from forge_lib.errors import UsageError


class KeysTest(ForgeTestCase):
    def test_roundtrip_nested(self):
        home = Path("/h")
        key = "billing-v2/ENG-101-api/ENG-105-dao"
        path = keys.key_to_path(home, key)
        self.assertEqual(path, Path("/h/billing-v2/tasks/ENG-101-api/tasks/ENG-105-dao"))
        path.parent.mkdir  # noqa: B018 (path need not exist for key_to_path)
        self.home.mkdir()
        real = keys.key_to_path(self.home, key)
        real.mkdir(parents=True)
        self.assertEqual(keys.path_to_key(self.home, real), key)

    def test_project_key(self):
        self.assertTrue(keys.is_project_key("billing-v2"))
        self.assertEqual(keys.key_to_path(Path("/h"), "billing-v2"), Path("/h/billing-v2"))
        self.assertEqual(keys.parent_key("a/b/c"), "a/b")
        self.assertIsNone(keys.parent_key("a"))
        self.assertEqual(keys.project_of("a/b/c"), "a")

    def test_invalid(self):
        for bad in ("a/tasks/b", "a/../b", "", "a//b", ".cache"):
            with self.assertRaises(UsageError, msg=bad):
                keys.split_key(bad)
        self.home.mkdir()
        with self.assertRaises(UsageError):
            keys.path_to_key(self.home, self.home / "p" / "x" / "t")
