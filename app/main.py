"""Long-polling entry point. No incoming HTTP port, public IP, domain or SSL needed."""
import logging
import os
import signal
import time

from .api import TelegramAPI, TelegramAPIError
from .config import Config
from .command_menu import install_commands
from .scheduler import process_due_schedules
from .service import GroupBot
from .store import Store

LOG = logging.getLogger("groupbot")
_running = True


def _stop(_signal, _frame):
    global _running
    _running = False


def run(api, store, config):
    global _running
    identity = api.call("getMe")
    bot = GroupBot(api, store, identity["id"], identity["username"], config.owner_ids)
    # A new dedicated token can safely switch from webhook to polling.
    # No pending updates are deliberately discarded.
    api.call("deleteWebhook", drop_pending_updates=False)
    install_commands(api, "zh-CN")
    install_commands(api, "zh-CN", language_code="zh")
    install_commands(api, "en", language_code="en")
    LOG.info("机器人已启动，使用长轮询模式（不暴露 Token）。")
    backoff = 1
    last_cleanup = 0
    last_scheduler = 0
    last_prune = 0
    trace_latency = os.getenv("LATENCY_DEBUG", "0").lower() in (
        "1", "true", "yes", "on")
    while _running:
        try:
            bot.drain_ocr()
        except Exception:
            LOG.exception("OCR结果处理异常，本轮已跳过")
        try:
            bot.cleanup_word_notices()
        except Exception:
            LOG.exception("临时删除提醒清理失败")
        if time.monotonic() - last_cleanup >= 30:
            last_cleanup = time.monotonic()
            try:
                bot.cleanup_expired()
            except Exception:
                LOG.exception("验证过期清理失败")
        if time.monotonic() - last_scheduler >= 10:
            last_scheduler = time.monotonic()
            try:
                process_due_schedules(bot)
            except Exception:
                LOG.exception("定时消息调度失败")
        if time.monotonic() - last_prune >= 24 * 3600:
            last_prune = time.monotonic()
            try:
                store.prune_activity(retain_days=30)
            except Exception:
                LOG.exception("活跃记录清理失败")
        try:
            # When a notice is pending, use short polling windows so a
            # configured 10-second expiration is not delayed by a 25s poll.
            effective_timeout = (
                min(config.poll_timeout, 3)
                if store.has_pending_notice_deletions() else config.poll_timeout)
            updates = api.call(
                "getUpdates", offset=store.offset() + 1,
                timeout=effective_timeout, limit=50,
                allowed_updates=["message", "edited_message", "channel_post",
                                 "callback_query", "my_chat_member"])
            backoff = 1
            for update in updates:
                update_id = update.get("update_id")
                if not isinstance(update_id, int) or update_id <= store.offset():
                    continue
                start_handling = time.monotonic()
                try:
                    bot.handle_update(update)
                except Exception:
                    LOG.exception("处理单条更新失败 update_id=%s", update_id)
                finally:
                    elapsed = time.monotonic() - start_handling
                    if "callback_query" in update and elapsed >= 2.5:
                        # Do not log callback payload, Telegram ID or secrets.
                        LOG.warning(
                            "Telegram 管理按钮处理耗时 %.2f 秒；"
                            "请检查上方的 API 响应慢记录及服务器网络",
                            elapsed)
                    elif "callback_query" in update and trace_latency:
                        LOG.info("Telegram 管理按钮处理耗时 %.2f 秒", elapsed)
                # Consume a poison update rather than entering an infinite loop.
                store.save_offset(update_id)
        except TelegramAPIError as exc:
            LOG.warning("Telegram 连接出现问题：%s", exc)
            pause = max(backoff, min(60, int(exc.retry_after or 0)))
            for _ in range(pause):
                if not _running:
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 30)
    bot.ocr.shutdown()


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        LOG.error("%s", exc)
        raise SystemExit(2) from None
    store = Store(cfg.db_path, timezone=os.getenv("TZ", "Asia/Shanghai"))
    api = TelegramAPI(cfg.token, cfg.api_base)
    try:
        run(api, store, cfg)
    except TelegramAPIError as exc:
        LOG.error("启动失败：%s", exc)
        raise SystemExit(1) from None
    finally:
        api.close()
        store.close()


if __name__ == "__main__":
    main()
