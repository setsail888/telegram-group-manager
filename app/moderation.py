"""Conservative group-specific filters. False positives are always possible."""
import re
import time
import unicodedata
from collections import defaultdict, deque
from urllib.parse import urlsplit

_LINK = re.compile(
    r"(?<![\w@])(?:https?://|www\.)[^\s<>\u3000]+|"
    r"(?<![\w@])(?:[a-z0-9][a-z0-9-]*\.)+(?:com|net|org|cn|cc|io|xyz|top|vip|"
    r"me|info|app|co|dev|shop|site|online|link|ru|hk|tw|us|uk|de|jp|ai)"
    r"(?::\d{1,5})?(?:/[^\s<>]*)?",
    flags=re.IGNORECASE)
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u034f"}
_MENTION = re.compile(r"(?<![\w@])@[a-zA-Z][a-zA-Z0-9_]{4,31}\b")
_OBFUSCATED_URL = re.compile(
    r"(?i)(?<![\w])(?:t|telegram|www)\s*(?:\[\s*\.\s*\]|\(\s*\.\s*\)|．|\s+\.\s*|\s*\.\s+)\s*(?:me|com|net|org)\b"
    r"|(?i:(?<![\w])hxxps?://[^\s]+)")


def normalize(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(c for c in value if c not in _ZERO_WIDTH
                   and unicodedata.category(c) not in ("Cf",)).strip()


def compact(value):
    """Opt-in detection of deliberately split keyword characters (not default)."""
    value = normalize(value)
    return "".join(c for c in value if not unicodedata.category(c).startswith(
        ("P", "Z")) and not c.isspace())


def match_words(text, banned_words, *, loose=False, exceptions=()):
    """Return the matching banned word; exceptions only waive their own rule."""
    clean = normalize(text)
    dense = compact(clean) if loose else ""
    for word in banned_words:
        phrase = normalize(word)
        if not phrase:
            continue
        basic = phrase in clean
        dense_keyword = compact(phrase)
        # Loose mode is opt-in and requires enough characters to reduce
        # false positives on short, common Chinese/English substrings.
        allowed_loose = len(dense_keyword) >= (3 if any(
            "\u3400" <= c <= "\u9fff" for c in dense_keyword) else 4)
        found = basic or (loose and allowed_loose and dense_keyword in dense)
        if not found:
            continue
        exception_match = False
        for rule in exceptions:
            prefix, sep, context = str(rule).partition("|")
            if not sep or normalize(prefix) != phrase:
                continue
            # Waive only this exact preapproved phrase, never a longer spam
            # message with the allowlisted phrase appended to evade filters.
            if normalize(context) and normalize(context) == clean:
                exception_match = True
                break
        if not exception_match:
            return word
    return None


def media_kind(message):
    for kind in ("photo", "video", "animation", "document", "sticker",
                 "voice", "video_note", "audio"):
        if message.get(kind):
            return kind
    return None


def normalize_domain(value):
    value = normalize(value).strip().strip(".")
    if value.startswith(("https://", "http://")):
        value = urlsplit(value).hostname or ""
    value = value.split("/")[0].split(":")[0]
    try:
        value = value.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", value) or len(value) > 253:
        return ""
    return value


def extract_domains(message):
    """Find links in text, captions and hidden text_link entities."""
    content = (message.get("text") or message.get("caption") or "")
    urls = [m.group(0) for m in _LINK.finditer(normalize(content))]
    for key in ("entities", "caption_entities"):
        for entity in message.get(key, []):
            if entity.get("type") == "text_link" and entity.get("url"):
                urls.append(entity["url"])
    result = []
    for url in urls:
        if url.startswith(("http://", "https://")):
            domain = normalize_domain(url)
        else:
            domain = normalize_domain(url.removeprefix("www."))
        # Explicit URLs with malformed/unsupported hostnames are still links:
        # never silently classify an invalid host as whitelisted content.
        result.append(domain or "")
    return result


def allowed(domain, whitelist):
    return any(domain == allow or domain.endswith("." + allow) for allow in whitelist)


class FloodGuard:
    def __init__(self, limit=7, window=10, repeat_limit=3, repeat_window=45):
        self.limit = limit
        self.window = window
        self.repeat_limit = repeat_limit
        self.repeat_window = repeat_window
        self.records = defaultdict(deque)
        self.last_housekeeping = 0

    def check(self, chat_id, user_id, text, now=None, *,
              limit=None, window=None, repeat_limit=None, repeat_window=None,
              media=None, media_limit=5, media_window=20, loose=False):
        now = time.monotonic() if now is None else now
        key = (chat_id, user_id)
        recent = self.records[key]
        limit = self.limit if limit is None else limit
        window = self.window if window is None else window
        repeat_limit = self.repeat_limit if repeat_limit is None else repeat_limit
        repeat_window = self.repeat_window if repeat_window is None else repeat_window
        while recent and now - recent[0][0] > max(window, repeat_window, media_window):
            recent.popleft()
        normalized = (compact(text) if loose else normalize(text))[:256]
        recent.append((now, normalized, media))
        # Bound a single very active member's history as well as the number
        # of tracked members; configured thresholds never exceed 30/300 sec.
        while len(recent) > 512:
            recent.popleft()
        too_many = sum(now - tick <= window for tick, _, _ in recent) > limit
        repeated = bool(normalized) and sum(
            now - tick <= repeat_window and value == normalized
            for tick, value, _ in recent) >= repeat_limit
        media_burst = bool(media) and sum(
            now - tick <= media_window and kind is not None
            for tick, _, kind in recent) > media_limit
        # Bound memory for large communities; purge inactive users.
        if len(self.records) > 15000 and now - self.last_housekeeping > 60:
            self.last_housekeeping = now
            for old_key in list(self.records):
                if not self.records[old_key] or now - self.records[old_key][-1][0] > 60:
                    del self.records[old_key]
        return ("短时刷屏" if too_many else "媒体刷屏" if media_burst
                else "重复刷屏" if repeated else None)


class PhotoRepeatGuard:
    """Optional conservative photo flood guard based on Telegram file_unique_id.

    Detects only exact Telegram photo identity repeated by one sender in one
    group. It does not inspect image contents, QR codes or re-encoded copies.
    """

    def __init__(self):
        self.records = defaultdict(deque)
        self.last_cleanup = 0

    def check(self, message, *, limit=3, window=60, now=None):
        photos = message.get("photo") or []
        sender = (message.get("from") or {}).get("id")
        chat = (message.get("chat") or {}).get("id")
        if not photos or sender is None or chat is None:
            return None
        file_key = photos[-1].get("file_unique_id")
        if not isinstance(file_key, str) or not file_key:
            return None
        moment = time.monotonic() if now is None else now
        key = (int(chat), int(sender), file_key)
        recent = self.records[key]
        while recent and moment - recent[0] > window:
            recent.popleft()
        recent.append(moment)
        if len(self.records) > 10000 and moment - self.last_cleanup > 60:
            self.last_cleanup = moment
            for old_key in list(self.records):
                if not self.records[old_key] or moment - self.records[old_key][-1] > 600:
                    del self.records[old_key]
        if len(recent) >= limit:
            return "重复图片刷屏"
        return None


def evaluate(message, settings, banned_words, allowed_domains, flood=None,
             *, exceptions=(), is_edit=False):
    """Return one Chinese reason or None. Call only after bot/admin exemption checks."""
    content = message.get("text") or message.get("caption") or ""
    clean = normalize(content)
    if match_words(clean, banned_words, loose=settings.get("word_loose_match", False),
                   exceptions=exceptions) is not None:
        return "违禁词"
    if settings.get("block_forward") and (
        message.get("forward_origin") or message.get("forward_from")
        or message.get("forward_from_chat")):
        return "禁止转发"
    if settings.get("block_links"):
        allowlist = [normalize_domain(d) for d in allowed_domains]
        allowlist = [d for d in allowlist if d]
        if any(not allowed(domain, allowlist) for domain in extract_domains(message)):
            return "外部链接"
    if settings.get("spam_guard_enabled"):
        if len(_MENTION.findall(clean)) > settings.get("mention_limit", 5):
            return "批量提及刷屏"
        if _OBFUSCATED_URL.search(clean) and settings.get("block_links"):
            return "伪装链接"
    if settings.get("anti_flood") and flood is not None and not is_edit:
        user = message.get("from", {}).get("id")
        chat = message.get("chat", {}).get("id")
        if user is not None and chat is not None:
            return flood.check(
                chat, user, content,
                limit=settings.get("flood_limit", 7),
                window=settings.get("flood_window", 10),
                repeat_limit=settings.get("repeat_limit", 3),
                repeat_window=settings.get("repeat_window", 45),
                media=media_kind(message) if settings.get("media_flood_enabled") else None,
                media_limit=settings.get("media_flood_limit", 5),
                media_window=settings.get("media_flood_window", 20),
                loose=settings.get("word_loose_match", False))
    return None
