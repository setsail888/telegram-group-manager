"""Environment-only configuration: no secrets committed or printed."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    token: str
    db_path: str = "/app/data/bot.sqlite3"
    api_base: str = "https://api.telegram.org"
    poll_timeout: int = 25
    owner_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls):
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or token == "PASTE_YOUR_BOTFATHER_TOKEN_HERE":
            raise ValueError("请先在 .env 设置 BotFather 提供的 BOT_TOKEN")
        api_base = os.getenv("API_BASE", "https://api.telegram.org").rstrip("/")
        parsed = urlsplit(api_base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("API_BASE 必须是有效的 HTTPS 地址")
        try:
            timeout = int(os.getenv("POLL_TIMEOUT", "25"))
        except ValueError as exc:
            raise ValueError("POLL_TIMEOUT 必须是整数") from exc
        if not 1 <= timeout <= 50:
            raise ValueError("POLL_TIMEOUT 应在 1 到 50 秒之间")
        owners = frozenset(int(x.strip()) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip())
        return cls(token=token, db_path=os.getenv("DB_PATH", "/app/data/bot.sqlite3"),
                   api_base=api_base, poll_timeout=timeout, owner_ids=owners)
