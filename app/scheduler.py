"""Persistent scheduled posts; Telegram Bot API does not schedule messages itself.

All times are local to Store.tz. Only group admins may create, pause or remove tasks.
Delivery is retried after transient failures. Rare duplicate sends are possible
if the process crashes after Telegram accepts a message but before SQLite commits.
"""
from .i18n import msg as _ui, join_text
import logging
import re
import time
from datetime import datetime, timedelta

from .api import TelegramAPIError

LOG = logging.getLogger(__name__)
WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    _ui('一'): 0, _ui('二'): 1, _ui('三'): 2, _ui('四'): 3, _ui('五'): 4, _ui('六'): 5, _ui('日'): 6, _ui('天'): 6,
}
MAX_LATENESS = 6 * 3600


def _clock(value):
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError(_ui('时间请使用 24 小时 HH:MM 格式，例如 09:00'))
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError(_ui('时间超出范围'))
    return hour, minute


def parse_rule(mode, rule):
    """Canonicalize /schedule input without running arbitrary cron expressions."""
    mode = mode.lower()
    rule = join_text(' ', rule.strip().split())
    if mode == "daily":
        _clock(rule)
        return mode, rule
    if mode == "weekly":
        parts = rule.split(" ")
        if len(parts) != 2 or parts[0].lower() not in WEEKDAYS:
            raise ValueError(_ui('周任务格式：weekly mon 09:00（mon 至 sun，或 一 至 日）'))
        _clock(parts[1])
        day = next(name for name, index in WEEKDAYS.items()
                   if name in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                   and index == WEEKDAYS[parts[0].lower()])
        return mode, _ui('{p0} {p1}', p0=day, p1=parts[1])
    if mode == "once":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", rule):
            raise ValueError(_ui('单次任务格式：once 2026-10-15 09:00'))
        try:
            datetime.strptime(rule, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError(_ui('日期或时间不存在')) from exc
        return mode, rule
    raise ValueError(_ui('只支持 once / daily / weekly 三种定时类型'))


def next_occurrence(mode, rule, tz, after=None):
    """Strictly future epoch; rejects one-off dates in the past."""
    moment = time.time() if after is None else float(after)
    local = datetime.fromtimestamp(moment, tz)
    mode, rule = parse_rule(mode, rule)
    if mode == "once":
        candidate = datetime.strptime(rule, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if candidate.timestamp() <= moment:
            raise ValueError(_ui('单次任务时间必须在未来'))
        if candidate.timestamp() > moment + 366 * 86400:
            raise ValueError(_ui('单次任务最长可以设置为未来一年'))
        return int(candidate.timestamp())
    if mode == "daily":
        hour, minute = _clock(rule)
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() <= moment:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=hour, minute=minute)
    else:
        weekday, clock = rule.split()
        hour, minute = _clock(clock)
        delta = (WEEKDAYS[weekday] - local.weekday()) % 7
        candidate = (local + timedelta(days=delta)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() <= moment:
            candidate += timedelta(days=7)
    # Some time zones have DST gaps/folds; normalize through timestamp.
    # Asia/Shanghai (the documented default) does not have current DST gaps.
    return int(candidate.timestamp())


def parse_new_schedule(command):
    """Syntax: daily 09:00 | 标题 | 内容, weekly mon 09:00 | ... etc."""
    head, sep, remaining = command.partition("|")
    if not sep:
        raise ValueError(_ui('用法：/schedule daily 09:00 | 标题 | 消息内容'))
    title, sep, body = remaining.partition("|")
    if not sep:
        raise ValueError(_ui('请使用两个竖线 | 分隔时间、标题和消息内容'))
    head = head.strip().split(maxsplit=1)
    if len(head) != 2:
        raise ValueError(_ui('请提供定时类型和时间'))
    mode, rule = parse_rule(head[0], head[1])
    title, body = title.strip(), body.strip()
    if not 1 <= len(title) <= 60 or not 1 <= len(body) <= 3500:
        raise ValueError(_ui('标题为 1-60 字，内容为 1-3500 字'))
    return mode, rule, title, body


def format_run(timestamp, tz):
    return datetime.fromtimestamp(int(timestamp), tz).strftime("%Y-%m-%d %H:%M")


def process_due_schedules(bot, now=None, limit=10):
    """Send at most 10 overdue posts per pass; no calls without an approved job."""
    moment = int(time.time() if now is None else now)
    done = 0
    for item in bot.store.due_schedules(now=moment, limit=limit):
        if not bot.store.claim_schedule(item["id"], now=moment):
            continue
        if moment - item["next_run"] > MAX_LATENESS:
            # Never dump a multi-day queue after a NAS restart.
            next_run = (next_occurrence(item["mode"], item["rule"], bot.store.tz, moment)
                        if item["mode"] != "once" else None)
            bot.store.finish_schedule(item["id"], sent=False, next_run=next_run, now=moment)
            bot.log(item["chat_id"], _ui('定时任务 #{p0} 超过 6 小时，已跳过过期发送', p0=item['id']))
            continue
        # Revocation matters: a former group admin must not retain posting rights.
        try:
            member = bot.api.call("getChatMember", chat_id=item["chat_id"],
                                  user_id=item["owner_id"])
        except TelegramAPIError:
            bot.store.schedule_failed(item["id"], now=moment)
            continue
        if not isinstance(member, dict) or member.get("status") not in (
                "creator", "administrator", "member", "restricted", "left", "kicked"):
            bot.store.schedule_failed(item["id"], now=moment)
            continue
        if member.get("status") not in ("creator", "administrator"):
            bot.store.toggle_schedule(item["chat_id"], item["id"], False)
            bot.log(item["chat_id"], _ui('定时任务 #{p0} 创建者已不是管理员，任务已暂停', p0=item['id']))
            continue
        message = bot.send(item["chat_id"],
                           _ui('📢 {p0}\n\n{p1}', p0=item['title'], p1=item['body']))
        if not isinstance(message, dict) or not message.get("message_id"):
            bot.store.schedule_failed(item["id"], now=moment)
            continue
        next_run = (next_occurrence(item["mode"], item["rule"], bot.store.tz, moment)
                    if item["mode"] != "once" else None)
        bot.store.finish_schedule(item["id"], sent=True, next_run=next_run, now=moment)
        done += 1
    return done
