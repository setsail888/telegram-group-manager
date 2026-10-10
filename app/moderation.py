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


def normalize(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(c for c in value if c not in _ZERO_WIDTH
                   and unicodedata.category(c) not in ("Cf",)).strip()


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

    def check(self, chat_id, user_id, text, now=None):
        now = time.monotonic() if now is None else now
        key = (chat_id, user_id)
        recent = self.records[key]
        while recent and now - recent[0][0] > max(self.window, self.repeat_window):
            recent.popleft()
        normalized = normalize(text)[:256]
        recent.append((now, normalized))
        too_many = sum(now - tick <= self.window for tick, _ in recent) > self.limit
        repeated = bool(normalized) and sum(
            now - tick <= self.repeat_window and value == normalized
            for tick, value in recent) >= self.repeat_limit
        # Bound memory for large communities; purge inactive users.
        if len(self.records) > 15000 and now - self.last_housekeeping > 60:
            self.last_housekeeping = now
            for old_key in list(self.records):
                if not self.records[old_key] or now - self.records[old_key][-1][0] > 60:
                    del self.records[old_key]
        return "短时刷屏" if too_many else ("重复刷屏" if repeated else None)


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


def evaluate(message, settings, banned_words, allowed_domains, flood=None):
    """Return one Chinese reason or None. Call only after bot/admin exemption checks."""
    content = message.get("text") or message.get("caption") or ""
    clean = normalize(content)
    for word in banned_words:
        if word and normalize(word) in clean:
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
    if settings.get("anti_flood") and flood is not None:
        user = message.get("from", {}).get("id")
        chat = message.get("chat", {}).get("id")
        if user is not None and chat is not None:
            return flood.check(chat, user, content)
    return None
