"""Small Telegram Bot API adapter: JSON POST, long polling, safe error messages."""
import logging
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import requests

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
        # requests.Session connection pools are not shared across threads:
        # polling/UI calls use one session; callback acknowledgments have
        # their own. This preserves HTTPS proxies and session keep-alive.
        self._thread = threading.local()
        self._sessions = set()
        self._sessions_lock = threading.Lock()
        self._closed = False
        self._trace_latency = os.getenv("LATENCY_DEBUG", "0").lower() in (
            "1", "true", "yes", "on")
        self._ack_slots = threading.BoundedSemaphore(12)
        self._ack_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="groupguard-callback-ack")

    def _session(self):
        if self._closed:
            raise TelegramAPIError("connection", "API 客户端已关闭")
        session = getattr(self._thread, "session", None)
        if session is not None:
            return session
        with self._sessions_lock:
            if self._closed:
                raise TelegramAPIError("connection", "API 客户端已关闭")
            session = requests.Session()
            self._sessions.add(session)
            self._thread.session = session
        return session

    def _drop_session(self):
        """Discard a broken connection pool without retrying mutations."""
        session = getattr(self._thread, "session", None)
        if session is not None:
            with self._sessions_lock:
                self._sessions.discard(session)
            session.close()
            del self._thread.session

    def call(self, method, *, _files=None, **params):
        # Long polling legitimately waits for updates. Interactive methods
        # should not stall the entire single-threaded bot for 35 seconds on
        # a degraded connection.
        read_timeout = (max(35, int(params.get("timeout", 0)) + 10)
                        if method == "getUpdates" else 15)
        started_at = time.monotonic()
        try:
            # Sessions reuse TCP/TLS connections between successive API calls.
            # Separate connect and read timeouts bound network outages.
            try:
                payload = ({"json": params} if _files is None else
                           {"data": {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()},
                            "files": _files})
                response = self._session().post(self._url + method, **payload,
                                                timeout=(5, read_timeout), allow_redirects=False)
            except requests.RequestException:
                self._drop_session()
                # Never log a URL with the Bot Token or exception repr.
                raise TelegramAPIError(method, "网络连接中断") from None
            try:
                result = response.json()
            except ValueError:
                raise TelegramAPIError(
                    method, "HTTP 错误" if response.status_code >= 400
                    else "服务端返回了无效 JSON",
                    error_code=response.status_code) from None
        except (TimeoutError, OSError):
            # Never log a URL with the bot token or exception repr containing that URL.
            self._drop_session()
            raise TelegramAPIError(method, "网络连接中断") from None
        finally:
            elapsed = time.monotonic() - started_at
            # Never print the URL, raw params, response, bot token or IDs.
            # Polling delays are expected, so only measure individual
            # interactive/API requests here.
            if method != "getUpdates" and elapsed >= 2.5:
                LOG.warning("Telegram API 响应较慢: %s 耗时 %.2f 秒",
                            method, elapsed)
            elif method != "getUpdates" and self._trace_latency:
                LOG.info("Telegram API 请求: %s 耗时 %.2f 秒",
                         method, elapsed)
        if not isinstance(result, dict) or result.get("ok") is not True:
            info = result if isinstance(result, dict) else {}
            description = str(info.get("description", "API 拒绝了请求"))
            # Telegram should not echo token, but don't expose raw endpoint in logs.
            if "/bot" in description:
                description = "API 拒绝了请求"
            retry = info.get("parameters", {}).get("retry_after") if isinstance(info.get("parameters"), dict) else None
            raise TelegramAPIError(
                method, description, retry_after=retry,
                error_code=info.get("error_code", response.status_code))
        return result.get("result")

    def answer_background(self, callback_query_id, text="", show_alert=False):
        """Overlap a non-alert acknowledgment with editing the same menu.

        No SQLite access or admin permission checks occur in the worker.
        Critical alerts remain synchronous. Bounded queue protects bot load.
        """
        if show_alert or not self._ack_slots.acquire(blocking=False):
            return False

        def deliver():
            try:
                self.safe("answerCallbackQuery",
                          callback_query_id=callback_query_id,
                          text=str(text)[:190], show_alert=False)
            except Exception:
                # The task must never block the polling thread, and callback
                # IDs/URLs must not leak into unexpected exception logs.
                LOG.error("Telegram 按钮后台确认遇到内部错误")
            finally:
                self._ack_slots.release()

        try:
            self._ack_pool.submit(deliver)
            return True
        except RuntimeError:
            self._ack_slots.release()
            return False

    def close(self):
        """Flush callback acknowledgments before closing connection pools."""
        self._ack_pool.shutdown(wait=True, cancel_futures=False)
        with self._sessions_lock:
            self._closed = True
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

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
        return self.fetch_file(file_id, max_bytes=max_bytes)

    def fetch_file(self, file_id, max_bytes=1_048_576):
        """Download only a Telegram file id through getFile; never log token URL."""
        if not isinstance(file_id, str) or not 1 <= len(file_id) <= 512:
            raise TelegramAPIError("getFile", "无效文件 ID")
        info = self.call("getFile", file_id=file_id)
        if not isinstance(info, dict):
            raise TelegramAPIError("getFile", "文件信息无效")
        if int(info.get("file_size") or 0) > max_bytes:
            raise TelegramAPIError("getFile", "文件太大")
        path = info.get("file_path", "")
        # No arbitrary URLs, path traversal or query parameters.
        if (not isinstance(path, str) or not re.fullmatch(
                r"[A-Za-z0-9_./-]{1,256}", path)
                or ".." in path or path.startswith("/")):
            raise TelegramAPIError("getFile", "文件路径不安全")
        request = urllib.request.Request(
            self._files_url + path,
            headers={"Accept": "application/octet-stream"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                content = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TelegramAPIError("getFile", "无法下载文件") from None
        if not content or len(content) > max_bytes:
            raise TelegramAPIError("getFile", "文件为空或超出大小限制")
        return content
