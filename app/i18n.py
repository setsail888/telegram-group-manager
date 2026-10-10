"""Deferred local UI translations. Interpolated user content is never translated."""
from contextvars import ContextVar
from functools import lru_cache
import json
from pathlib import Path

LOCALES = {
    "zh-CN": "🇨🇳 简体中文", "en": "🇺🇸 English",
    "fr": "🇫🇷 Français", "de": "🇩🇪 Deutsch",
    "es": "🇪🇸 Español", "ru": "🇷🇺 Русский",
    "pt": "🇵🇹🇧🇷 Português", "it": "🇮🇹 Italiano",
    "el": "🇬🇷 Ελληνικά", "nl": "🇳🇱 Nederlands",
    "pl": "🇵🇱 Polski", "id": "🇮🇩 Bahasa Indonesia",
    "cs": "🇨🇿 Čeština", "da": "🇩🇰 Dansk",
    "fi": "🇫🇮 Suomi", "hu": "🇭🇺 Magyar",
    "ro": "🇷🇴 Română", "sv": "🇸🇪 Svenska",
    "tr": "🇹🇷 Türkçe", "uk": "🇺🇦 Українська",
    "ar": "🇸🇦 العربية", "vi": "🇻🇳 Tiếng Việt",
    "hi": "🇮🇳 हिन्दी", "ms": "🇲🇾 Bahasa Melayu",
    "fa": "🇮🇷 فارسی", "uz": "🇺🇿 Oʻzbekcha",
}
CURRENT_LANGUAGE = ContextVar("groupguard_language", default="zh-CN")


def normalize_locale(value):
    code = str(value or "").replace("_", "-").lower()
    base = code.split("-", 1)[0]
    base = {"in": "id"}.get(base, base)
    return base if base in LOCALES else "zh-CN"


@lru_cache(maxsize=32)
def catalog(locale):
    if locale == "zh-CN" or locale not in LOCALES:
        return {}
    return json.loads((Path(__file__).with_name("locales") / f"{locale}.json").read_text(encoding="utf-8"))


class Text(str):
    """A canonical Chinese string with a separate rendering recipe.

    Comparisons, JSON, SQLite and policy keys keep their existing semantics.
    Translation happens only at Telegram's output boundary, for its recipient.
    """
    def __new__(cls, source, fields=None, *, recipe=None):
        original = source.format(**fields) if fields is not None else source
        obj = super().__new__(cls, original)
        obj.source, obj.fields, obj.recipe = source, fields, recipe
        return obj

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return Text(str(self) + str(other), recipe=("concat", self, other))

    def __radd__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return Text(str(other) + str(self), recipe=("concat", other, self))

    def __getitem__(self, key):
        value = super().__getitem__(key)
        return Text(value, recipe=("slice", self, key)) if isinstance(key, slice) else value

    def replace(self, old, new, count=-1):
        return Text(str(self).replace(old, new, count), recipe=("replace", self, old, new, count))

    def format(self, *args, **kwargs):
        return Text(str(self).format(*args, **kwargs), recipe=("format", self, args, kwargs))


def msg(source, **fields):
    return Text(source, fields if fields else None)


def error_text(error):
    return error.args[0] if error.args and isinstance(error.args[0], Text) else str(error)


def join_text(separator, values):
    values = tuple(values)
    original = separator.join(values)
    if isinstance(separator, Text) or any(isinstance(value, Text) for value in values):
        return Text(original, recipe=("join", separator, values))
    return original


def render(value, locale):
    if isinstance(value, Exception) and value.args and isinstance(value.args[0], Text):
        return render(value.args[0], locale)
    if not isinstance(value, Text) or locale == "zh-CN":
        return value
    if value.recipe:
        op, *parts = value.recipe
        if op == "concat":
            return render(parts[0], locale) + render(parts[1], locale)
        if op == "join":
            return render(parts[0], locale).join(render(v, locale) for v in parts[1])
        if op == "slice":
            return render(parts[0], locale)[parts[1]]
        if op == "replace":
            return render(parts[0], locale).replace(render(parts[1], locale), render(parts[2], locale), parts[3])
        if op == "format":
            return render(parts[0], locale).format(*(render(v, locale) for v in parts[1]),
                **{k: render(v, locale) for k, v in parts[2].items()})
    template = catalog(locale).get(value.source, value.source)
    if value.fields is None:
        return template
    return template.format(**{key: render(field, locale) for key, field in value.fields.items()})


class LocalizedAPI:
    """Translate only text/caption/button labels, never URLs, IDs or payloads."""
    OUTPUTS = {"sendMessage", "editMessageText", "answerCallbackQuery", "sendPhoto",
               "sendVideo", "sendDocument", "sendAnimation", "sendAudio", "sendVoice"}

    def __init__(self, api, store):
        self.raw, self.store = api, store

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def params(self, method, params):
        if method not in self.OUTPUTS:
            return params
        locale = self.store.ui_language(params["chat_id"]) if "chat_id" in params else CURRENT_LANGUAGE.get()
        result = dict(params)
        for key in ("text", "caption"):
            if key in result:
                limit = 190 if method == "answerCallbackQuery" else 1024 if key == "caption" else 4000
                result[key] = str(render(result[key], locale))[:limit]
        markup = result.get("reply_markup")
        if isinstance(markup, dict) and "inline_keyboard" in markup:
            result["reply_markup"] = {**markup, "inline_keyboard": [
                [{**b, "text": str(render(b["text"], locale))} for b in row] for row in markup["inline_keyboard"]]}
        return result

    def safe(self, method, **params):
        return self.raw.safe(method, **self.params(method, params))

    def call(self, method, **params):
        return self.raw.call(method, **self.params(method, params))

    def answer_background(self, **params):
        background = getattr(self.raw, "answer_background", None)
        return bool(background and background(**self.params("answerCallbackQuery", params)))
