import os
import tempfile
import unittest

from app.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "groupbot.sqlite")
        self.store = Store(self.path)
        self.store.ensure_chat(-1001, "群 A")
        self.store.ensure_chat(-1002, "群 B")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_group_isolation_and_persistence(self):
        self.store.set(-1001, "block_links", False)
        self.store.add_filter(-1001, "word", "test", "TEST")
        self.assertTrue(self.store.settings(-1002)["block_links"])
        self.assertEqual(self.store.filters(-1002, "word"), [])
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.settings(-1001)["block_links"])
        self.assertEqual(len(self.store.filters(-1001, "word")), 1)

    def test_pending_and_stats(self):
        self.store.put_pending(-1001, 23, "abc", 2, 100, 15)
        self.assertEqual(len(self.store.expired(99)), 0)
        self.assertEqual(self.store.expired(100)[0]["user_id"], 23)
        self.store.del_pending(-1001, 23)
        self.assertIsNone(self.store.pending(-1001, 23))
        self.store.stat(-1001, "deleted", 2, when=1750000000)
        self.assertEqual(sum(x["count"] for x in self.store.db.execute(
            "SELECT count FROM stats WHERE chat_id=-1001")), 2)

    def test_offset(self):
        self.assertEqual(self.store.offset(), -1)
        self.store.save_offset(456)
        self.assertEqual(self.store.offset(), 456)

    def test_reject_invalid_settings(self):
        with self.assertRaises(ValueError):
            self.store.set(-1001, "log_chat_id", 123)
        with self.assertRaises(ValueError):
            self.store.set(-1001, "block_links", "off")
        with self.assertRaises(ValueError):
            self.store.add_filter(-1001, "unexpected", "x", "x")


if __name__ == "__main__":
    unittest.main()
