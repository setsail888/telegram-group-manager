"""Conservative group-specific filters. False positives are always possible."""
from .i18n import msg as _ui, join_text
import re
import time
import unicodedata
from collections import OrderedDict, deque
from functools import lru_cache
from hashlib import sha256
from urllib.parse import urlsplit
from .keyword_matcher import KeywordMatcher

_LINK = re.compile(
    r"(?<![a-z0-9_@])(?:https?://|www\.)[^\s<>\u3000,;!\"'()，。！？；（）《》「」『』]+|"
    r"(?<![\w@])(?:[a-z0-9][a-z0-9-]*\.)+(?:com|net|org|cn|cc|io|xyz|top|vip|"
    r"me|info|app|co|dev|shop|site|online|link|ru|hk|tw|us|uk|de|jp|ai)"
    r"(?![a-z0-9_-]|\.[a-z0-9_-])"
    r"(?::\d{1,5})?(?:/[^\s<>]*)?",
    flags=re.IGNORECASE)
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u034f"}
_MENTION = re.compile(r"(?<![\w@])@[a-zA-Z][a-zA-Z0-9_]{4,31}\b")
_OBFUSCATED_DOT = re.compile(
    r"\s*(?:\[\s*(?:\.|dot)\s*\]|\(\s*(?:\.|dot)\s*\))\s*"
    r"|(?<=\w)\s+\.\s*|\.\s+(?=\w)", re.IGNORECASE)
WORD_MODES = {"contains": _ui('包含'), "word": _ui('整词'), "exact": _ui('整句')}


def normalize(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return join_text('', (c for c in value if c not in _ZERO_WIDTH and unicodedata.category(c) not in ('Cf',))).strip()


def compact(value):
    """Opt-in detection of deliberately split keyword characters (not default)."""
    value = normalize(value)
    return join_text('', (c for c in value if not unicodedata.category(c).startswith(('P', 'Z', 'S', 'M')) and (not c.isspace())))


@lru_cache(maxsize=8)
def _word_rules(words, exceptions, modes=()):
    """Reuse normalized rules; immutable keys make edits take effect immediately."""
    rules = []
    modes = dict(modes)
    for word in words:
        phrase = normalize(word)
        dense = compact(phrase)
        minimum = 3 if any('\u3400' <= c <= '\u9fff' for c in dense) else 4
        if phrase:
            mode = modes.get(phrase, "contains")
            if mode not in WORD_MODES:
                raise ValueError(_ui('未知关键词匹配方式'))
            rules.append((word, phrase, dense, mode, len(dense) >= minimum))
    waived = set()
    for rule in exceptions:
        prefix, sep, context = rule.partition("|")
        if sep and normalize(context):
            waived.add((normalize(prefix), normalize(context)))
    return KeywordMatcher(rules), frozenset(waived)


class WordWaivers(frozenset):
    """Marks normalized pairs so hot scans need no per-exception type checks."""


def word_waivers(exceptions):
    if isinstance(exceptions, WordWaivers):
        return exceptions
    if isinstance(exceptions, frozenset) and all(isinstance(e, tuple) for e in exceptions):
        return WordWaivers(exceptions)
    waived = set()
    for rule in exceptions:
        prefix, sep, context = str(rule).partition("|")
        if sep and normalize(context):
            waived.add((normalize(prefix), normalize(context)))
    return WordWaivers(waived)


def compile_words(words, modes=None):
    return _word_rules(tuple(str(w) for w in words), (),
                       tuple(sorted((normalize(k), v) for k, v in (modes or {}).items())))[0]


def match_words(text, banned_words, *, loose=False, exceptions=(), modes=None):
    """Return the matching banned word; exceptions only waive their own rule."""
    clean = normalize(text)
    dense = compact(clean) if loose else ""
    rules = banned_words if isinstance(banned_words, KeywordMatcher) else compile_words(banned_words, modes)
    return rules.find(clean, dense=dense if loose else None, waived=word_waivers(exceptions))


def message_word_hit(message, banned_words, *, loose=False, exceptions=(), modes=None, excluded=frozenset()):
    parts = [message.get("text") or message.get("caption") or ""]
    parts += [str(b.get("text") or "") for row in (message.get("reply_markup") or {}).get("inline_keyboard", [])
              for b in row if isinstance(b, dict)]
    clean = normalize(join_text('\n', parts))
    rules = banned_words if isinstance(banned_words, KeywordMatcher) else compile_words(banned_words, modes)
    waived = word_waivers(exceptions)
    hit = rules.find(clean, dense=compact(clean) if loose else None, waived=waived, excluded=excluded)
    if hit is not None:
        return hit
    # Exact text is checked independently of buttons, with whole-message waivers.
    if len(parts) > 1:
        for part in parts:
            hit = rules.find(normalize(part), context=clean, waived=waived, excluded=excluded, exact_only=True)
            if hit is not None:
                return hit
    return None


def media_kind(message):
    for kind in ("photo", "video", "animation", "document", "sticker",
                 "voice", "video_note", "audio"):
        if message.get(kind):
            return kind
    return None


def normalize_domain(value):
    value = normalize(value).strip().strip(".")
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return ""
        value = (parsed.hostname or "").rstrip(".")
        value = value.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", value) or len(value) > 253:
        return ""
    return value


def _entity_text(content, entity):
    """Telegram offsets address the original text in UTF-16 code units."""
    offset, length = entity.get("offset"), entity.get("length")
    if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
        return ""
    data = content.encode("utf-16-le")
    if (offset + length) * 2 > len(data):
        return ""
    try:
        return data[offset * 2:(offset + length) * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return ""


def _entities(message):
    for field, key in (("text", "entities"), ("caption", "caption_entities")):
        content = message.get(field) or ""
        for entity in message.get(key) or []:
            yield content, entity


def extract_domains(message):
    """Find links in text, captions and hidden text_link entities."""
    content = (message.get("text") or message.get("caption") or "")
    urls = [m.group(0) for m in _LINK.finditer(normalize(content))]
    for original, entity in _entities(message):
        if entity.get("type") == "text_link" and entity.get("url"):
            urls.append(entity["url"])
        elif entity.get("type") == "url":
            url = _entity_text(original, entity)
            if url:
                urls.append(url)
    result = []
    for url in urls:
        url = normalize(url).rstrip(".,!?;:，。！？；：、）)]}〉》」』\"'")
        domain = normalize_domain(url)
        # Explicit URLs with malformed/unsupported hostnames are still links:
        # never silently classify an invalid host as whitelisted content.
        result.append(domain or "")
    return list(dict.fromkeys(result))


def obfuscated_domains(content):
    clean = normalize(content)
    decoded = re.sub(r"\bhxxp(s?)://", r"http\1://", clean)
    decoded = _OBFUSCATED_DOT.sub(".", decoded)
    return extract_domains({"text": decoded}) if decoded != clean else []


def mention_count(message):
    """Count visible @ tokens and hidden mentions once per actual occurrence."""
    count = len(_MENTION.findall(normalize(
        message.get("text") or message.get("caption") or "")))
    for content, entity in _entities(message):
        if entity.get("type") == "text_mention":
            # A hidden entity may itself display an @username already counted.
            if not _MENTION.fullmatch(normalize(_entity_text(content, entity))):
                count += 1
    return count


def allowed(domain, whitelist):
    return any(domain == allow or domain.endswith("." + allow) for allow in whitelist)


class FloodGuard:
    def __init__(self, limit=7, window=10, repeat_limit=3, repeat_window=45):
        self.limit = limit
        self.window = window
        self.repeat_limit = repeat_limit
        self.repeat_window = repeat_window
        self.records = OrderedDict()
        self.last_housekeeping = 0
        self.max_records = 15000

    def check(self, chat_id, user_id, text, now=None, *,
              limit=None, window=None, repeat_limit=None, repeat_window=None,
              media=None, media_limit=5, media_window=20, loose=False,
              message_id=None, album_id=None):
        now = time.monotonic() if now is None else now
        key = (chat_id, user_id)
        # Retain the longest configurable window, not just 60 seconds.
        if now - self.last_housekeeping >= 60:
            self.last_housekeeping = now
            for old_key in list(self.records):
                if now - self.records[old_key][-1][0] > 300:
                    del self.records[old_key]
        recent = self.records.setdefault(key, deque())
        self.records.move_to_end(key)
        while len(self.records) > self.max_records:
            self.records.popitem(last=False)
        limit = self.limit if limit is None else limit
        window = self.window if window is None else window
        repeat_limit = self.repeat_limit if repeat_limit is None else repeat_limit
        repeat_window = self.repeat_window if repeat_window is None else repeat_window
        while recent and now - recent[0][0] > max(window, repeat_window, media_window):
            recent.popleft()
        if message_id is not None and any(row[3] == message_id for row in recent):
            return None
        normalized = compact(text) if loose else normalize(text)
        # Compare complete content without keeping thousands of long messages.
        fingerprint = sha256(normalized.encode("utf-8")).digest() if normalized else None
        recent.append((now, fingerprint, media, message_id, album_id))
        # Bound a single very active member's history as well as the number
        # of tracked members; configured thresholds never exceed 30/300 sec.
        while len(recent) > 512:
            recent.popleft()
        def count_events(seconds, only_media=False):
            return len({("album", row[4]) if row[4] else ("message", index)
                        for index, row in enumerate(recent)
                        if now-row[0] <= seconds and (not only_media or row[2] is not None)})
        too_many = count_events(window) > limit
        repeated = fingerprint is not None and sum(
            now - tick <= repeat_window and value == fingerprint
            for tick, value, _, _, _ in recent) >= repeat_limit
        media_burst = bool(media) and count_events(media_window, True) > media_limit
        return (_ui('短时刷屏') if too_many else _ui('媒体刷屏') if media_burst
                else _ui('重复刷屏') if repeated else None)


class PhotoRepeatGuard:
    """Optional conservative photo flood guard based on Telegram file_unique_id.

    Detects only exact Telegram photo identity repeated by one sender in one
    group. It does not inspect image contents, QR codes or re-encoded copies.
    """

    def __init__(self):
        self.records = OrderedDict()
        self.last_cleanup = 0
        self.max_records = 10000

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
        if moment - self.last_cleanup >= 60:
            self.last_cleanup = moment
            for old_key in list(self.records):
                if moment - self.records[old_key][-1][0] > 600:
                    del self.records[old_key]
        key = (int(chat), int(sender), file_key)
        recent = self.records.setdefault(key, deque())
        self.records.move_to_end(key)
        while len(self.records) > self.max_records:
            self.records.popitem(last=False)
        while recent and moment - recent[0][0] > window:
            recent.popleft()
        message_id = message.get("message_id")
        if message_id is not None and any(mid == message_id for _, mid in recent):
            return None
        recent.append((moment, message_id))
        while len(recent) > 512:
            recent.popleft()
        if len(recent) >= limit:
            return _ui('重复图片刷屏')
        return None


def evaluate(message, settings, banned_words, allowed_domains, flood=None,
             *, exceptions=(), is_edit=False, modes=None):
    """Return one Chinese reason or None. Call only after bot/admin exemption checks."""
    content = message.get("text") or message.get("caption") or ""
    buttons = [button for row in (message.get("reply_markup") or {}).get("inline_keyboard", [])
               for button in row if isinstance(button, dict)]
    clean = normalize(join_text('\n', [content] + [str(b.get('text') or '') for b in buttons]))
    if settings.get("word_filter_enabled", True) and message_word_hit(
            message, banned_words, loose=settings.get("word_loose_match", False),
            exceptions=exceptions, modes=modes) is not None:
        return _ui('违禁词')
    if settings.get("block_forward") and (
        message.get("forward_origin") or message.get("forward_from")
        or message.get("forward_from_chat")):
        return _ui('禁止转发')
    if settings.get("block_links"):
        allowlist = [normalize_domain(d) for d in allowed_domains]
        allowlist = [d for d in allowlist if d]
        if any(not allowed(domain, allowlist) for domain in extract_domains(message)):
            return _ui('外部链接')
        if any(not allowed(domain, allowlist) for b in buttons
               if (domain := normalize_domain(b.get("url") or ""))):
            return _ui('外部链接')
    if settings.get("spam_guard_enabled"):
        if mention_count(message) > settings.get("mention_limit", 5):
            return _ui('批量提及刷屏')
        if settings.get("block_links") and any(
                not allowed(domain, allowlist) for domain in obfuscated_domains(clean)):
            return _ui('伪装链接')
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
                loose=settings.get("word_loose_match", False),
                message_id=message.get("message_id"),
                album_id=message.get("media_group_id") if settings.get("album_single_enabled") else None)
    return None
