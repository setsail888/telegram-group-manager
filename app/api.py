"""Small Telegram Bot API adapter: JSON POST, long polling, safe error messages."""
import json
import logging
import re
import time
import urllib.error
import urllib.request

LOG = logging.getLogger(__name__)


class TelegramAPIError(Exception):
    def __init__(self, method, description="Telegram API 请求失败", retry_after=None, error_code=None):
        self.method = method
        self.retry_after = retry_after
        self.error_code = error_code
        super().__init__(f"{method}: {description[:200]}")


class TelegramAPI:
    def __init__(self, token, api_base="https://api.telegram.org"):
        self._url = f"{api_base}/bot{token}/"
        self._files_url = f"{api_base}/file/bot{token}/"

    def call(self, method, **params):
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url + method, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        # Long polling legitimately waits for updates. Interactive methods
        # should not stall the entire single-threaded bot for 35 seconds on
        # a degraded connection.
        timeout = (max(35, int(params.get("timeout", 0)) + 10)
                   if method == "getUpdates" else 15)
        started_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read(4096).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise TelegramAPIError(method, "HTTP 错误", error_code=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Never log a URL with the bot token or exception repr containing that URL.
            raise TelegramAPIError(method, "网络连接中断") from None
        except (ValueError, UnicodeDecodeError):
            raise TelegramAPIError(method, "服务端返回了无效 JSON") from None
        finally:
            elapsed = time.monotonic() - started_at
            # Never print the URL, raw params, response, bot token or IDs.
            # Polling delays are expected, so only measure individual
            # interactive/API requests here.
            if method != "getUpdates" and elapsed >= 2.5:
                LOG.warning("Telegram API 响应较慢: %s 耗时 %.2f 秒",
                            method, elapsed)
        if not isinstance(result, dict) or result.get("ok") is not True:
            info = result if isinstance(result, dict) else {}
            description = str(info.get("description", "API 拒绝了请求"))
            # Telegram should not echo token, but don't expose raw endpoint in logs.
            if "/bot" in description:
                description = "API 拒绝了请求"
            retry = info.get("parameters", {}).get("retry_after") if isinstance(info.get("parameters"), dict) else None
            raise TelegramAPIError(method, description, retry_after=retry, error_code=info.get("error_code"))
        return result.get("result")

    def safe(self, method, **params):
        try:
            return self.call(method, **params)
        except TelegramAPIError as exc:
            # Telegram returns 400 for editing an already identical page.
            # This is not a failure, and must not trigger sendMessage fallback
            # which would create duplicate menu messages and extra latency.
            if (method == "editMessageText"
                    and "message is not modified" in str(exc).lower()):
                return True
            LOG.warning("Telegram %s 未成功: %s", method, exc)
            return None

    def fetch_photo(self, file_id, max_bytes=3_000_000):
        """Download only a Telegram file id through getFile; never log token URL."""
        if not isinstance(file_id, str) or not 1 <= len(file_id) <= 512:
            raise TelegramAPIError("getFile", "无效文件 ID")
        info = self.call("getFile", file_id=file_id)
        if not isinstance(info, dict):
            raise TelegramAPIError("getFile", "文件信息无效")
        if int(info.get("file_size") or 0) > max_bytes:
            raise TelegramAPIError("getFile", "图片文件太大")
        path = info.get("file_path", "")
        # No arbitrary URLs, path traversal or query parameters.
        if (not isinstance(path, str) or not re.fullmatch(
                r"[A-Za-z0-9_./-]{1,256}", path)
                or ".." in path or path.startswith("/")):
            raise TelegramAPIError("getFile", "文件路径不安全")
        request = urllib.request.Request(
            self._files_url + path,
            headers={"Accept": "image/*"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                content = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TelegramAPIError("getFile", "无法下载图片") from None
        if not content or len(content) > max_bytes:
            raise TelegramAPIError("getFile", "图片为空或超出大小限制")
        return content
