import os
import sqlite3
import tempfile
import unittest
from app.economy import Economy
from app.store import Store


class EconomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "economy.db")
        self.store = Store(self.path)
        self.store.ensure_chat(-100, "A")
        self.store.ensure_chat(-200, "B")
        self.store.set(-100, "points_enabled", True)
        self.e = Economy(self.store)
        self.store.change_points(-100, 7, 100, 1, now=100)
        self.store.change_points(-100, 8, 10, 1, now=100)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_transfer_requires_confirmation_and_is_atomic(self):
        row = self.e.new_transfer(-100, 7, 8, 25, 51, now=200)
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 100)
        self.assertEqual(self.store.point_balance(-100, 8)["balance"], 10)
        self.e.bind_prompt(-100, row["id"], 321)
        receipt = self.e.confirm_transfer(
            -100, row["id"], row["nonce"], 7, 321, accept=True, now=201)
        self.assertEqual(receipt["amount"], 25)
        self.assertTrue(receipt["created"])
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 75)
        self.assertEqual(self.store.point_balance(-100, 8)["balance"], 35)
        self.assertEqual(
            self.e.confirm_transfer(-100, row["id"], row["nonce"],
                                    7, 321, accept=True, now=202)["created"], False)
        events = self.store.point_events(-100)
        self.assertEqual([r["kind"] for r in events[:2]],
                         ["transfer_in", "transfer_out"])
        other = self.e.new_transfer(-100, 7, 8, 25, 51, now=203)
        self.assertEqual(other["id"], row["id"])

    def test_wrong_person_nonce_group_message_cannot_transfer(self):
        row = self.e.new_transfer(-100, 7, 8, 30, 52, now=200)
        self.e.bind_prompt(-100, row["id"], 500)
        for cid, nonce, user, message in (
            (-200, row["nonce"], 7, 500),
            (-100, "wrong-nonce", 7, 500),
            (-100, row["nonce"], 8, 500),
            (-100, row["nonce"], 7, 501),
        ):
            with self.subTest(cid=cid, user=user, nonce=nonce):
                with self.assertRaises(ValueError):
                    self.e.confirm_transfer(
                        cid, row["id"], nonce, user, message, accept=True, now=201)
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 100)

    def test_cancel_expire_and_insufficient_funds(self):
        row = self.e.new_transfer(-100, 7, 8, 90, 53, now=200)
        self.e.bind_prompt(-100, row["id"], 550)
        self.assertEqual(
            self.e.confirm_transfer(-100, row["id"], row["nonce"], 7, 550,
                                    accept=False, now=201)["status"], "cancelled")
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 100)
        expired = self.e.new_transfer(-100, 7, 8, 90, 54, now=200)
        self.e.bind_prompt(-100, expired["id"], 551)
        self.assertEqual(
            self.e.confirm_transfer(-100, expired["id"], expired["nonce"], 7, 551,
                                    accept=True, now=501)["status"], "expired")
        self.store.change_points(-100, 7, -99, 1, now=510)
        other = self.e.new_transfer(-100, 7, 8, 1, 55, now=520)
        self.e.bind_prompt(-100, other["id"], 552)
        self.store.change_points(-100, 7, -1, 1, now=521)
        with self.assertRaises(ValueError):
            self.e.confirm_transfer(
                -100, other["id"], other["nonce"], 7, 552,
                accept=True, now=522)
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 0)
        self.assertEqual(self.store.point_balance(-100, 8)["balance"], 10)

    def test_concurrent_connections_cannot_confirm_twice(self):
        row = self.e.new_transfer(-100, 7, 8, 30, 56, now=200)
        self.e.bind_prompt(-100, row["id"], 553)
        extra = Store(self.path)
        try:
            other = Economy(extra)
            self.assertTrue(self.e.confirm_transfer(
                -100, row["id"], row["nonce"], 7, 553,
                accept=True, now=201)["created"])
            self.assertFalse(other.confirm_transfer(
                -100, row["id"], row["nonce"], 7, 553,
                accept=True, now=202)["created"])
            self.assertEqual(extra.point_balance(-100, 8)["balance"], 40)
        finally:
            extra.close()

    def test_shop_purchase_refund_inventory(self):
        item = self.e.add_item(-100, 1, "专属群头衔", "管理员人工处理",
                               20, 1, now=100)
        order = self.e.purchase(-100, 7, item, 61, now=200)
        self.assertEqual(order["status"], "pending")
        self.assertEqual(self.e.item(-100, item)["stock"], 0)
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 80)
        dup = self.e.purchase(-100, 7, item, 61, now=201)
        self.assertFalse(dup["created"])
        with self.assertRaises(ValueError):
            self.e.purchase(-100, 8, item, 62, now=201)
        refund = self.e.process_order(-100, order["id"], 1, "refund", now=202)
        self.assertTrue(refund["changed"])
        self.assertEqual(self.e.item(-100, item)["stock"], 1)
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 100)
        self.assertFalse(self.e.process_order(
            -100, order["id"], 1, "refund", now=203)["changed"])
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 100)

    def test_shop_fulfilled_order_cannot_refund(self):
        item = self.e.add_item(-100, 1, "角色", "", 20, 2, now=100)
        order = self.e.purchase(-100, 7, item, 63, now=200)
        self.assertTrue(self.e.process_order(
            -100, order["id"], 1, "fulfill", now=201)["changed"])
        self.assertFalse(self.e.process_order(
            -100, order["id"], 1, "refund", now=202)["changed"])
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 80)
        self.assertEqual(self.e.item(-100, item)["stock"], 1)

    def test_shop_rejects_cross_group_and_overdraft(self):
        item = self.e.add_item(-100, 1, "大礼包", "虚拟奖励", 50, 1)
        with self.assertRaises(ValueError):
            self.e.purchase(-200, 7, item, 64)
        with self.assertRaises(ValueError):
            self.e.purchase(-100, 8, item, 64)
        self.assertEqual(self.e.item(-100, item)["stock"], 1)
        order = self.e.purchase(-100, 7, item, 65)
        with self.assertRaises(ValueError):
            self.e.process_order(-200, order["id"], 2, "refund")
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 50)
        self.assertEqual(self.e.orders(-200), [])

    def test_invalid_parameters_no_side_effects(self):
        with self.assertRaises(ValueError):
            self.e.new_transfer(-100, 7, 7, 1, 66)
        with self.assertRaises(ValueError):
            self.e.new_transfer(-100, 7, 8, -1, 66)
        with self.assertRaises(ValueError):
            self.e.add_item(-100, 1, "x", "", 0, 1)
        with self.assertRaises(ValueError):
            self.e.add_item(-100, 1, "x", "", 1, -1)
        self.assertEqual(self.e.items(-100), [])

    def test_existing_legacy_points_not_modified(self):
        # The new schema must not remove balances, streaks or earlier audit.
        self.store.set(-100, "points_enabled", False)
        with self.assertRaises(ValueError):
            self.e.new_transfer(-100, 7, 8, 10, 67)
        self.assertEqual(self.store.point_balance(-100, 7)["balance"], 100)
        self.assertEqual(self.store.point_balance(-100, 7)["checkins"], 0)


if __name__ == "__main__":
    unittest.main()
