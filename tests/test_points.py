import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.store import Store

TZ = ZoneInfo("Asia/Shanghai")


def moment(day):
    return int(datetime.strptime(day + " 10:00", "%Y-%m-%d %H:%M").replace(
        tzinfo=TZ).timestamp())


class PointsStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "points.sqlite")
        self.store = Store(self.path)
        self.store.ensure_chat(-100, "群1")
        self.store.ensure_chat(-200, "群2")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_disabled_by_default(self):
        self.assertFalse(self.store.settings(-100)["points_enabled"])
        with self.assertRaises(ValueError):
            self.store.checkin(-100, 3, now=moment("2026-10-10"))
        self.assertEqual(self.store.point_balance(-100, 3)["balance"], 0)

    def test_daily_idempotence_and_streak(self):
        self.store.set(-100, "points_enabled", True)
        first = self.store.checkin(-100, 3, now=moment("2026-10-10"))
        self.assertEqual((first["award"], first["streak"], first["balance"]), (5, 1, 5))
        again = self.store.checkin(-100, 3, now=moment("2026-10-10"))
        self.assertFalse(again["created"])
        self.assertEqual(again["balance"], 5)
        self.assertEqual(len(self.store.point_events(-100)), 1)
        nextday = self.store.checkin(-100, 3, now=moment("2026-10-11"))
        self.assertEqual((nextday["award"], nextday["streak"], nextday["balance"]),
                         (6, 2, 11))
        reset = self.store.checkin(-100, 3, now=moment("2026-10-15"))
        self.assertEqual((reset["award"], reset["streak"]), (5, 1))

    def test_award_cap(self):
        self.store.set(-100, "points_enabled", True)
        self.store.set(-100, "checkin_award", 20)
        from datetime import timedelta
        start = datetime(2026, 10, 1, 10, tzinfo=TZ)
        results = []
        for day in range(10):
            at = int((start + timedelta(days=day)).timestamp())
            results.append(self.store.checkin(-100, 3, now=at)["award"])
        self.assertEqual(results[:2], [20, 21])
        self.assertEqual(results[7:], [26, 26, 26])

    def test_group_separation(self):
        self.store.set(-100, "points_enabled", True)
        self.store.set(-200, "points_enabled", True)
        self.store.checkin(-100, 3, now=moment("2026-10-10"))
        self.assertEqual(self.store.point_balance(-200, 3)["balance"], 0)
        self.store.checkin(-200, 3, now=moment("2026-10-10"))
        self.assertEqual(self.store.point_balance(-100, 3)["balance"], 5)
        self.assertEqual(self.store.point_balance(-200, 3)["balance"], 5)

    def test_two_connections_no_double_award(self):
        self.store.set(-100, "points_enabled", True)
        duplicate = Store(self.path)
        try:
            self.assertTrue(self.store.checkin(
                -100, 3, now=moment("2026-10-10"))["created"])
            self.assertFalse(duplicate.checkin(
                -100, 3, now=moment("2026-10-10"))["created"])
            self.assertEqual(duplicate.point_balance(-100, 3)["balance"], 5)
            self.assertEqual(len(duplicate.point_events(-100)), 1)
        finally:
            duplicate.close()

    def test_admin_grant_and_deduct_audit(self):
        self.assertEqual(self.store.change_points(-100, 3, 20, 99), 20)
        self.assertEqual(self.store.change_points(-100, 3, -3, 99), 17)
        with self.assertRaises(ValueError):
            self.store.change_points(-100, 3, -18, 99)
        self.assertEqual(self.store.point_balance(-100, 3)["balance"], 17)
        self.assertEqual(self.store.point_balance(-200, 3)["balance"], 0)
        events = self.store.point_events(-100)
        self.assertEqual([e["delta"] for e in events], [-3, 20])
        self.assertEqual([e["actor_id"] for e in events], [99, 99])
        self.assertEqual(self.store.point_top(-100)[0]["user_id"], 3)

    def test_checkin_after_admin_adjust(self):
        self.store.set(-100, "points_enabled", True)
        self.store.change_points(-100, 3, 10, 99)
        item = self.store.checkin(-100, 3, now=moment("2026-10-10"))
        self.assertEqual((item["checkins"], item["balance"]), (1, 15))

    def test_invalid_input_does_not_mutate(self):
        for value in (0, 10001, -10001, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.change_points(-100, 3, value, 99)
        with self.assertRaises(ValueError):
            self.store.set(-100, "checkin_award", 101)
        with self.assertRaises(ValueError):
            self.store.set(-100, "repeat_photo_window", 2)
        with self.assertRaises(ValueError):
            self.store.set(-100, "repeat_photo_limit", 11)
        self.assertEqual(self.store.point_balance(-100, 3)["balance"], 0)


if __name__ == "__main__":
    unittest.main()
