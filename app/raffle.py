"""Transparent, free group raffle with one entry per real user.

No purchases, entry fee, deposits, betting, points stakes or monetary payouts.
Random winner uses secrets.randbelow, stored permanently; redraw not provided.
"""
from contextlib import contextmanager
import secrets
import time


class Raffles:
    def __init__(self, store):
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

    def active(self, chat_id):
        row = self.db.execute(
            "SELECT * FROM raffles WHERE chat_id=? AND status='open' "
            "ORDER BY id DESC LIMIT 1", (int(chat_id),)).fetchone()
        return dict(row) if row else None

    def create(self, chat_id, admin_id, title, now=None):
        if not 2 <= len(title.strip()) <= 100:
            raise ValueError("免费抽奖标题必须是 2–100 字")
        moment = int(time.time() if now is None else now)
        with self.tx():
            if self.active(chat_id):
                raise ValueError("本群已有进行中的免费抽奖，先开奖或取消")
            cur = self.db.execute(
                "INSERT INTO raffles(chat_id,title,status,created_by,created_at)"
                " VALUES(?,?,'open',?,?)",
                (int(chat_id), title.strip(), int(admin_id), moment))
            return cur.lastrowid

    def join(self, chat_id, user_id, now=None):
        moment = int(time.time() if now is None else now)
        with self.tx():
            raffle = self.active(chat_id)
            if raffle is None:
                raise ValueError("当前没有开放的免费抽奖")
            already = self.db.execute(
                "SELECT 1 FROM raffle_entries WHERE raffle_id=? AND user_id=?",
                (raffle["id"], int(user_id))).fetchone()
            count = self.db.execute(
                "SELECT count(*) FROM raffle_entries WHERE raffle_id=?",
                (raffle["id"],)).fetchone()[0]
            if count >= 20000 and not already:
                raise ValueError("本次活动已满 20000 人")
            cur = self.db.execute(
                "INSERT OR IGNORE INTO raffle_entries(raffle_id,chat_id,user_id,created_at)"
                " VALUES(?,?,?,?)",
                (raffle["id"], int(chat_id), int(user_id), moment))
            return {"joined": bool(cur.rowcount), "raffle_id": raffle["id"],
                    "count": count + int(bool(cur.rowcount))}

    def draw(self, chat_id, admin_id, now=None, *, selected_user=None, expected_id=None):
        moment = int(time.time() if now is None else now)
        with self.tx():
            raffle = self.active(chat_id)
            if raffle is None:
                raise ValueError("没有可开奖的免费抽奖")
            if expected_id is not None and raffle["id"] != expected_id:
                raise ValueError("活动已变化，请重新开奖。")
            entrants = [r[0] for r in self.db.execute(
                "SELECT user_id FROM raffle_entries WHERE raffle_id=? ORDER BY user_id",
                (raffle["id"],))]
            if not entrants:
                raise ValueError("没有参与者，不能开奖")
            selected = entrants[secrets.randbelow(len(entrants))] if selected_user is None else selected_user
            if selected not in entrants:
                raise ValueError("候选报名已失效，请重试。")
            cursor = self.db.execute(
                "UPDATE raffles SET status='drawn',winner_id=?,finished_at=? "
                "WHERE chat_id=? AND id=? AND status='open'",
                (selected, moment, int(chat_id), raffle["id"]))
            if cursor.rowcount != 1:
                raise ValueError("活动状态已经变化")
            return {"id": raffle["id"], "title": raffle["title"],
                    "winner_id": selected, "count": len(entrants)}

    def cancel(self, chat_id, admin_id, now=None):
        moment = int(time.time() if now is None else now)
        with self.tx():
            cur = self.db.execute(
                "UPDATE raffles SET status='cancelled',finished_at=? "
                "WHERE chat_id=? AND status='open'",
                (moment, int(chat_id)))
            return cur.rowcount == 1

    def drop_member(self, chat_id, user_id, *, raffle_id=None):
        """Remove departed users from still-open raffles only."""
        return self.db.execute(
            "DELETE FROM raffle_entries WHERE chat_id=? AND user_id=? "
            "AND raffle_id IN (SELECT id FROM raffles "
            "WHERE chat_id=? AND status='open') AND (? IS NULL OR raffle_id=?)",
            (int(chat_id), int(user_id), int(chat_id), raffle_id, raffle_id)).rowcount

    def history(self, chat_id, limit=8):
        return [dict(r) for r in self.db.execute(
            "SELECT r.id,r.title,r.status,r.winner_id,r.created_at,"
            "(SELECT count(*) FROM raffle_entries e WHERE e.raffle_id=r.id) AS count "
            "FROM raffles r WHERE r.chat_id=? ORDER BY r.id DESC LIMIT ?",
            (int(chat_id), min(25, max(1, limit))))]
