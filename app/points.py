"""Chinese group check-in and points commands, no external reward purchases."""
from .i18n import msg as _ui, join_text
import time

MEMBER_COMMANDS = {"checkin", "points", "pointtop"}
ADMIN_COMMANDS = {"pointmode", "pointrate", "pointadd", "pointtake", "pointlog"}


def _positive_int(value, maximum, example):
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError(example) from None
    if not 1 <= count <= maximum:
        raise ValueError(example)
    return count


def handle_member_command(bot, message, command, argument):
    """Only the member themselves can check in; never act as anonymous admins."""
    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}
    uid = sender.get("id")
    if argument.strip() or not uid or sender.get("is_bot") or message.get("sender_chat"):
        return False  # allow regular moderation to inspect suspicious command text
    if not bot.store.settings(chat_id)["points_enabled"]:
        # Do not flood groups with repeated disabled-command notices.
        return True
    now = time.monotonic()
    key = (chat_id, int(uid), command)
    previous = bot._public_command_time.get(key)
    if previous is not None and now - previous < 20:
        return True
    bot._public_command_time[key] = now
    if len(bot._public_command_time) > 15000:
        for old, tick in list(bot._public_command_time.items()):
            if now - tick > 60:
                del bot._public_command_time[old]
    if command == "checkin":
        result = bot.store.checkin(chat_id, uid)
        if result["created"]:
            bot.send(
                chat_id,
                _ui('✅ 今日签到成功！获得 {p0} 积分\n连续签到 {p1} 天 · 当前余额 {p2}', p0=result['award'], p1=result['streak'], p2=result['balance']),
                disable_notification=True)
        else:
            bot.send(chat_id, _ui('📌 今天已经签到，当前积分 {p0}。', p0=result['balance']))
        return True
    if command == "points":
        data = bot.store.point_balance(chat_id, uid)
        bot.send(chat_id,
                 _ui('🪙 你的积分：{p0}\n累计签到：{p1} 天 · 连续签到：{p2} 天', p0=data['balance'], p1=data['checkins'], p2=data['streak']))
        return True
    ranking = bot.store.point_top(chat_id, 10)
    text = _ui('🏆 本群积分榜\n') + (
        join_text('\n', (_ui('{p0}. {p1}：{p2} 分', p0=i, p1=r['user_id'], p2=r['balance']) for i, r in enumerate(ranking, 1))) or _ui('暂无积分记录'))
    bot.send(chat_id, text)
    return True


def handle_admin_command(bot, message, command, argument):
    """Called only AFTER fresh getChatMember administrator authorization."""
    chat_id = message["chat"]["id"]
    actor = message["from"]["id"]
    if command == "pointmode":
        if argument not in ("on", "off"):
            raise ValueError(_ui('用法：/pointmode on 或 /pointmode off'))
        enabled = argument == "on"
        bot.store.set(chat_id, "points_enabled", enabled)
        bot.send(chat_id, _ui('🪙 签到积分已{p0}。', p0=_ui('开启') if enabled else _ui('关闭')))
        bot.log(chat_id, _ui('签到积分开关 {p0}', p0=enabled), actor=actor)
        return
    if command == "pointrate":
        award = _positive_int(argument, 100, _ui('用法：/pointrate 5（1-100）'))
        bot.store.set(chat_id, "checkin_award", award)
        bot.send(chat_id, _ui('✅ 每次签到基础奖励设为 {p0} 积分（连续签到还有少量奖励）。', p0=award))
        bot.log(chat_id, _ui('基础签到奖励 {p0}', p0=award), actor=actor)
        return
    if command == "pointlog":
        rows = bot.store.point_events(chat_id, limit=12)
        message_text = join_text('\n', (_ui('{p0} {p1}{p2} ({p3}) → {p4}', p0=r['user_id'], p1='+' if r['delta'] > 0 else '', p2=r['delta'], p3=r['kind'], p4=r['balance']) for r in rows))
        bot.send(chat_id, _ui('🧾 最近积分流水\n') + (message_text or _ui('暂无')))
        return
    if command not in ("pointadd", "pointtake"):
        return
    target_message = message.get("reply_to_message") or {}
    target = target_message.get("from") or {}
    user_id = target.get("id")
    if not user_id or target.get("is_bot") or target_message.get("sender_chat"):
        raise ValueError(_ui('请回复真实群成员的消息，再发送 /{p0} 数量', p0=command))
    amount = _positive_int(argument, 10000, _ui('用法：/{p0} 20（1-10000）', p0=command))
    delta = amount if command == "pointadd" else -amount
    total = bot.store.change_points(chat_id, user_id, delta, actor)
    bot.log(chat_id, _ui('积分调整 {p0}{p1}，余额 {p2}', p0='+' if delta > 0 else '', p1=delta, p2=total),
            actor=actor, target=user_id)
    # Admin success is not echoed in public chat. Audit in dedicated log group.
