"""Admin-configured auto-replies with multiple keywords, safe links and Telegram file_ids."""
import re
from urllib.parse import urlsplit

from .moderation import normalize


def parse_keywords(value):
    fragments = [item.strip() for item in re.split(r"[,，、;；]+", value) if item.strip()]
    if not 1 <= len(fragments) <= 8:
        raise ValueError("每条自动回复必须包含 1-8 个关键词，以逗号隔开")
    normalized = [normalize(item) for item in fragments]
    if any(not 2 <= len(item) <= 50 for item in normalized):
        raise ValueError("每个关键词需为 2-50 个字符")
    if len(set(normalized)) != len(normalized):
        raise ValueError("关键词不能重复")
    return ",".join(sorted(normalized)), "、".join(fragments)


def parse_buttons(value):
    """Format: 按钮名称 - https://example.com && 电报 - https://t.me/example."""
    if not value.strip():
        return []
    items = value.split("&&")
    if len(items) > 3:
        raise ValueError("每条自动回复最多 3 个链接按钮")
    buttons = []
    for item in items:
        label, separator, url = item.strip().rpartition(" - ")
        label, url = label.strip(), url.strip()
        if not separator or not 1 <= len(label) <= 40:
            raise ValueError("按钮格式：按钮名称 - https://example.com")
        if url.lower().startswith("t.me/"):
            url = "https://" + url
        if len(url) > 350 or any(c.isspace() for c in url):
            raise ValueError("链接太长或包含空格")
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and not parsed.username
                and not parsed.password
            )
            # Trigger port validation; malformed out-of-range ports raise ValueError.
            if parsed.port is not None and parsed.port != 443:
                valid = False
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("按钮链接必须是有效的 HTTPS 地址")
        buttons.append({"text": label, "url": url})
    return buttons


def media_from_reply(message):
    """Reuse Telegram file_id from an admin's replied media message."""
    referenced = message.get("reply_to_message") or {}
    if referenced.get("photo"):
        return "photo", referenced["photo"][-1].get("file_id")
    for kind in ("video", "document", "animation"):
        if referenced.get(kind):
            return kind, referenced[kind].get("file_id")
    return None, None
