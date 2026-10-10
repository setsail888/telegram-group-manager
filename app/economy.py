"""Group-scoped virtual points: confirmed transfers and manually fulfilled rewards.

Never handles money or external payment. All changes are transactional and audited.
"""
from .i18n import msg as _ui, join_text
from contextlib import contextmanager
import secrets
import time


def _id(value):
    if type(value) is not int or value == 0:
        raise ValueError(_ui('无效的成员或群组编号'))
    return value


def _amount(value, maximum=1000):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(_ui('数量必须在 1 到 {p0} 之间', p0=maximum))
    return value


class Economy:
    def __init__(self, store):
        self.store = store
        self.db = store.db

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def _active(self, chat_id):
        if not self.store.settings(chat_id)["points_enabled"]:
            raise ValueError(_ui('本群尚未开启积分系统'))

    def _wallet(self, chat_id, user_id):
        row = self.db.execute(
            "SELECT balance FROM point_balances WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)).fetchone()
        return row[0] if row else 0

    def _change(self, chat_id, user_id, delta, kind, actor, when):
        amount = self._wallet(chat_id, user_id) + delta
        if not 0 <= amount <= 1_000_000_000:
            raise ValueError(_ui('积分不足或超过余额上限'))
        self.db.execute(
            "INSERT INTO point_balances(chat_id,user_id,balance) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET balance=excluded.balance",
            (chat_id, user_id, amount))
        self.db.execute(
            "INSERT INTO point_events(chat_id,user_id,delta,balance,kind,actor_id,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (chat_id, user_id, delta, amount, kind, actor, when))
        return amount

    def new_transfer(self, chat_id, source, target, amount, message_id, now=None):
        chat_id, source, target = _id(chat_id), _id(source), _id(target)
        _amount(amount, 1000)
        _amount(message_id, 2_000_000_000)
        if source == target:
            raise ValueError(_ui('不能向自己转账'))
        self._active(chat_id)
        moment = int(time.time() if now is None else now)
        with self.tx():
            duplicate = self.db.execute(
                "SELECT * FROM transfer_intents WHERE chat_id=? AND request_message_id=?",
                (chat_id, message_id)).fetchone()
            if duplicate:
                if (duplicate["source_id"] != source or duplicate["target_id"] != target
                        or duplicate["amount"] != amount):
                    raise ValueError(_ui('已存在相同编号的转账'))
                return dict(duplicate)
            pending = self.db.execute(
                "SELECT count(*) FROM transfer_intents WHERE chat_id=? AND source_id=? "
                "AND status='pending' AND expires_at>?", (chat_id, source, moment)
            ).fetchone()[0]
            if pending >= 3:
                raise ValueError(_ui('请先确认或取消此前的转账'))
            if self._wallet(chat_id, source) < amount:
                raise ValueError(_ui('积分不足，不能发起转账'))
            nonce = secrets.token_hex(8)
            cursor = self.db.execute(
                "INSERT INTO transfer_intents(chat_id,source_id,target_id,amount,nonce,"
                "request_message_id,expires_at,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,'pending',?)",
                (chat_id, source, target, amount, nonce, message_id,
                 moment + 300, moment))
            return dict(self.db.execute(
                "SELECT * FROM transfer_intents WHERE id=?", (cursor.lastrowid,)
            ).fetchone())

    def bind_prompt(self, chat_id, intent_id, message_id):
        self.db.execute(
            "UPDATE transfer_intents SET confirmation_message_id=? "
            "WHERE chat_id=? AND id=? AND status='pending'",
            (int(message_id), int(chat_id), int(intent_id)))

    def get_transfer(self, chat_id, intent_id):
        row = self.db.execute(
            "SELECT * FROM transfer_intents WHERE chat_id=? AND id=?",
            (int(chat_id), int(intent_id))).fetchone()
        return dict(row) if row else None

    def confirm_transfer(self, chat_id, intent_id, nonce, requester, prompt_id,
                         *, accept, now=None):
        when = int(time.time() if now is None else now)
        cid, uid = _id(chat_id), _id(requester)
        with self.tx():
            row = self.db.execute(
                "SELECT * FROM transfer_intents WHERE id=? AND chat_id=?",
                (int(intent_id), cid)).fetchone()
            if (not row or row["source_id"] != uid or row["nonce"] != nonce
                    or not row["confirmation_message_id"]
                    or row["confirmation_message_id"] != prompt_id):
                raise ValueError(_ui('只有发起人才能操作此转账按钮'))
            if row["status"] != "pending":
                return {"created": False, "status": row["status"]}
            if row["expires_at"] <= when:
                self.db.execute(
                    "UPDATE transfer_intents SET status='cancelled' WHERE id=?",
                    (row["id"],))
                return {"created": False, "status": "expired"}
            if not accept:
                self.db.execute(
                    "UPDATE transfer_intents SET status='cancelled' WHERE id=?",
                    (row["id"],))
                return {"created": True, "status": "cancelled"}
            self._active(cid)
            self._change(cid, uid, -row["amount"], "transfer_out", uid, when)
            new_receiver_balance = self._change(
                cid, row["target_id"], row["amount"], "transfer_in", uid, when)
            self.db.execute(
                "INSERT INTO point_transfers(chat_id,source_id,target_id,amount,"
                "request_message_id,created_at) VALUES(?,?,?,?,?,?)",
                (cid, uid, row["target_id"], row["amount"],
                 row["request_message_id"], when))
            self.db.execute(
                "UPDATE transfer_intents SET status='executed' WHERE id=?",
                (row["id"],))
            return {"created": True, "status": "executed",
                    "amount": row["amount"], "target_id": row["target_id"],
                    "receiver_balance": new_receiver_balance}

    def add_item(self, chat_id, actor, title, description, price, stock, now=None):
        cid, actor = _id(chat_id), _id(actor)
        _amount(price, 10000)
        _amount(stock, 1000)
        if not 1 <= len(title.strip()) <= 60 or len(description) > 300:
            raise ValueError(_ui('奖励名称需 1–60 字，说明不能超过 300 字'))
        when = int(time.time() if now is None else now)
        with self.tx():
            n = self.db.execute(
                "SELECT count(*) FROM shop_items WHERE chat_id=? AND active=1",
                (cid,)).fetchone()[0]
            if n >= 30:
                raise ValueError(_ui('每群最多同时上架 30 项奖励'))
            cursor = self.db.execute(
                "INSERT INTO shop_items(chat_id,title,description,price,stock,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (cid, title.strip(), description.strip(), price, stock, actor, when))
            return cursor.lastrowid

    def items(self, chat_id):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM shop_items WHERE chat_id=? AND active=1 "
            "ORDER BY id DESC LIMIT 40", (int(chat_id),))]

    def item(self, chat_id, item_id):
        row = self.db.execute(
            "SELECT * FROM shop_items WHERE chat_id=? AND id=?",
            (int(chat_id), int(item_id))).fetchone()
        return dict(row) if row else None

    def disable_item(self, chat_id, item_id):
        return bool(self.db.execute(
            "UPDATE shop_items SET active=0 WHERE chat_id=? AND id=? AND active=1",
            (int(chat_id), int(item_id))).rowcount)

    def purchase(self, chat_id, user_id, item_id, message_id, now=None, *, request_key=None):
        cid, uid = _id(chat_id), _id(user_id)
        if request_key is None:
            _amount(message_id, 2_000_000_000)
        elif (not isinstance(request_key, str) or not 16 <= len(request_key) <= 64
              or any(c not in "0123456789abcdef" for c in request_key)):
            raise ValueError(_ui('按钮请求标识无效'))
        when = int(time.time() if now is None else now)
        with self.tx():
            if request_key is not None:
                self.db.execute("INSERT OR IGNORE INTO ui_purchase_requests(chat_id,user_id,token) "
                                "VALUES(?,?,?)", (cid,uid,request_key))
                request = self.db.execute("SELECT id FROM ui_purchase_requests "
                                          "WHERE chat_id=? AND user_id=? AND token=?",
                                          (cid,uid,request_key)).fetchone()
                # Disjoint from positive group-message command ids; private
                # chats can share message numbers without colliding orders.
                message_id = -request[0]
            duplicate = self.db.execute(
                "SELECT id,member_id,status FROM shop_orders "
                "WHERE chat_id=? AND request_message_id=?",
                (cid, message_id)).fetchone()
            if duplicate:
                if duplicate["member_id"] != uid:
                    raise ValueError(_ui('订单指令编号冲突'))
                return {"created": False, "id": duplicate["id"],
                        "status": duplicate["status"]}
            self._active(cid)
            item = self.db.execute(
                "SELECT * FROM shop_items WHERE chat_id=? AND id=? AND active=1",
                (cid, int(item_id))).fetchone()
            if not item or item["stock"] < 1:
                raise ValueError(_ui('奖励已下架或库存不足'))
            balance = self._change(cid, uid, -item["price"], "shop_purchase",
                                   uid, when)
            changed = self.db.execute(
                "UPDATE shop_items SET stock=stock-1 WHERE chat_id=? "
                "AND id=? AND stock>0", (cid, item_id)).rowcount
            if changed != 1:
                raise ValueError(_ui('库存已售完'))
            cursor = self.db.execute(
                "INSERT INTO shop_orders(chat_id,member_id,item_id,item_title,cost,"
                "status,request_message_id,created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                (cid, uid, item["id"], item["title"], item["price"], message_id, when))
            return {"created": True, "id": cursor.lastrowid,
                    "status": "pending", "balance": balance,
                    "title": item["title"]}

    def orders(self, chat_id, *, user_id=None, status=None, limit=20):
        sql = "SELECT * FROM shop_orders WHERE chat_id=?"
        args = [int(chat_id)]
        if user_id is not None:
            sql += " AND member_id=?"
            args.append(int(user_id))
        if status is not None:
            if status not in ("pending", "fulfilled", "refunded"):
                raise ValueError(_ui('未知订单状态'))
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(40, limit)))
        return [dict(r) for r in self.db.execute(sql, args)]

    def process_order(self, chat_id, order_id, actor, action, now=None):
        if action not in ("fulfill", "refund"):
            raise ValueError(_ui('未知订单操作'))
        cid, actor = _id(chat_id), _id(actor)
        when = int(time.time() if now is None else now)
        with self.tx():
            row = self.db.execute(
                "SELECT * FROM shop_orders WHERE chat_id=? AND id=?",
                (cid, int(order_id))).fetchone()
            if not row:
                raise ValueError(_ui('该订单不存在于本群'))
            if row["status"] != "pending":
                return {"changed": False, "status": row["status"],
                        "member_id": row["member_id"]}
            if action == "refund":
                self._change(cid, row["member_id"], row["cost"], "shop_refund",
                             actor, when)
                self.db.execute(
                    "UPDATE shop_items SET stock=stock+1 WHERE chat_id=? AND id=?",
                    (cid, row["item_id"]))
            status = "fulfilled" if action == "fulfill" else "refunded"
            self.db.execute(
                "UPDATE shop_orders SET status=?,processed_by=?,processed_at=? "
                "WHERE chat_id=? AND id=?", (status, actor, when, cid, order_id))
            return {"changed": True, "status": status,
                    "member_id": row["member_id"]}
