"""Bounded, metadata-only cross-account duplicate detection, per chat/topic."""
import json
import time
from collections import OrderedDict
from hashlib import sha256

from .moderation import normalize
from .points import MEMBER_COMMANDS as POINT_COMMANDS
from .economy_commands import MEMBER_COMMANDS as ECONOMY_COMMANDS

from .i18n import msg as _ui

REASON = _ui("跨账号重复刷屏")
MEMBER_COMMANDS = POINT_COMMANDS | ECONOMY_COMMANDS | {"help", "rules", "menu", "start"}
CROSS_KEYS = ("anti_flood", "cross_flood_mode", "cross_flood_limit", "cross_flood_users",
              "cross_flood_window", "cross_flood_min_chars", "observe_only")


def fingerprint(message, minimum):
    text = normalize(message.get("text") or message.get("caption") or "")
    media = []
    for kind in ("photo", "video", "animation", "document", "sticker", "voice", "audio", "video_note"):
        value = message.get(kind)
        if isinstance(value, list):
            value = value[-1] if value else None
        if isinstance(value, dict) and value.get("file_unique_id"):
            media.append((kind, value["file_unique_id"]))
    if not media and len("".join(text.split())) < minimum:
        return None
    buttons = [[(normalize(b.get("text") or ""), b.get("url") or "", b.get("callback_data") or "")
                for b in row if isinstance(b, dict)]
               for row in (message.get("reply_markup") or {}).get("inline_keyboard", [])]
    targets = [(e.get("type"), e.get("offset"), e.get("length"), e.get("url"), (e.get("user") or {}).get("id"))
               for e in message.get("entities", message.get("caption_entities", []))
               if e.get("type") in ("text_link", "text_mention")]
    # Simultaneous check-ins/raffle joins are legitimate identical messages.
    # Arguments, media, buttons and hidden targets still receive normal checks.
    if (not media and not buttons and not targets and text.startswith("/") and len(text.split()) == 1
            and text.split("@", 1)[0][1:] in MEMBER_COMMANDS):
        return None
    value = json.dumps([text, media, buttons, targets], ensure_ascii=False, separators=(",", ":"))
    return sha256(value.encode("utf-8")).digest()


class CrossAccountGuard:
    def __init__(self, max_records=20000, max_buckets=5000):
        self.records, self.buckets, self.seen = OrderedDict(), {}, OrderedDict()
        self.signatures, self.active = OrderedDict(), {}
        self.max_records, self.max_buckets = max_records, max_buckets

    def invalidate(self, cid, mid):
        row = self.records.pop((cid, mid), None)
        if row:
            bucket = self.buckets[row["key"]]
            bucket.pop(mid, None)
            if not bucket:
                del self.buckets[row["key"]]
                self.active.pop(row["key"], None)

    def clear(self, cid):
        for chat, mid in list(self.records):
            if chat == cid:
                self.invalidate(chat, mid)
        # Seen IDs remain to reject replayed messages after changing settings.

    def mark_processed(self, cid, mid):
        if (cid, mid) in self.records:
            self.records[cid, mid]["processed"] = True

    def activate(self, event, window, now=None):
        self.active[event["key"]] = (time.monotonic() if now is None else now)+window

    def check(self, message, settings, *, is_edit=False, now=None):
        now = time.monotonic() if now is None else now
        cid, mid = message["chat"]["id"], message.get("message_id")
        signature = tuple(settings[k] for k in CROSS_KEYS)
        if self.signatures.get(cid) != signature:
            self.clear(cid)
            self.signatures[cid] = signature
        self.signatures.move_to_end(cid)
        while len(self.signatures) > self.max_buckets:
            old_cid, _ = self.signatures.popitem(last=False)
            self.clear(old_cid)
        if is_edit:
            self.invalidate(cid, mid)
            return None
        while self.records and now-next(iter(self.records.values()))["time"] > 300:
            old_cid, old_mid = next(iter(self.records))
            self.invalidate(old_cid, old_mid)
        while self.seen and (len(self.seen) > self.max_records or now-next(iter(self.seen.values())) > 300):
            self.seen.popitem(last=False)
        if not settings["anti_flood"] or settings["cross_flood_mode"] == "off" or not mid or (cid, mid) in self.seen:
            return None
        self.seen[cid, mid] = now
        digest = fingerprint(message, settings["cross_flood_min_chars"])
        if digest is None:
            return None
        key = (cid, message.get("message_thread_id", 0), digest)
        bucket = self.buckets.get(key, {})
        for old in list(bucket.values()):
            if now-old["time"] > settings["cross_flood_window"]:
                self.invalidate(cid, old["mid"])
        bucket = self.buckets.setdefault(key, OrderedDict())
        row = {"key": key, "cid": cid, "mid": mid, "uid": message["from"]["id"], "time": now, "processed": False}
        bucket[mid] = self.records[cid, mid] = row
        while len(bucket) > 100:
            self.invalidate(cid, next(iter(bucket)))
        while len(self.records) > self.max_records or len(self.buckets) > self.max_buckets:
            old_cid, old_mid = next(iter(self.records))
            self.invalidate(old_cid, old_mid)
        # A previously confirmed wave remains blocked until one quiet window.
        active = self.active.get(key, 0) > now
        if active or (len(bucket) >= settings["cross_flood_limit"]
                      and len({r["uid"] for r in bucket.values()}) >= settings["cross_flood_users"]):
            return {"key": key, "rows": list(bucket.values()), "active": active}
        return None
