"""Automatic flood penalties run only after a confirmed trigger deletion."""
from .i18n import msg as _ui, join_text
import time
import re

PENALTIES = {"none": _ui('仅撤回消息，不处罚'), "warn": _ui('警告'), "mute": _ui('禁言'),
             "kick": _ui('踢出（允许重新加入）'), "ban": _ui('踢出并封禁'), "delete": _ui('仅删除触发消息（沿用累计规则）')}
FLOOD_SETTINGS = ("flood_penalty", "flood_mute_minutes", "flood_ban_minutes", "warn_limit",
                  "anti_flood", "cross_flood_mode", "cross_flood_limit", "cross_flood_users",
                  "cross_flood_window", "cross_flood_min_chars", "observe_only", "auto_mute_enabled")


def result_text(value):
    """Restore trusted system outcomes after SQLite has stripped Text metadata."""
    from .i18n import catalog
    if value in catalog("en"):
        return _ui(value)
    warning = re.fullmatch(r"已警告 (\d+)/(\d+)", value)
    if warning:
        return _ui('已警告 {p0}/{p1}', p0=warning[1], p1=warning[2])
    mute = re.fullmatch(r"禁言 (\d+) 分钟", value)
    if mute:
        return _ui('禁言 {p0} 分钟', p0=mute[1])
    ban = re.fullmatch(r"踢出并封禁 (\d+) 分钟", value)
    if ban:
        return _ui('踢出并封禁 ')+_ui('{p0} 分钟', p0=ban[1])
    for prefix in (_ui('已'), _ui('处罚失败：'), _ui('踢出并封禁 ')):
        if value.startswith(prefix):
            return prefix + result_text(value[len(prefix):])
    return str(value)


def penalty_label(settings):
    mode = settings["flood_penalty"]
    if mode == "delete":
        return _ui('单次仅删除触发消息')
    if mode == "mute":
        return _ui('删除并禁言 {p0} 分钟', p0=settings['flood_mute_minutes'])
    if mode == "warn":
        return _ui('删除并警告，达到{p0}次禁言{p1}分钟', p0=settings['warn_limit'], p1=settings['flood_mute_minutes'])
    if mode == "ban":
        duration = settings["flood_ban_minutes"]
        return _ui('踢出并封禁 ')+(_ui('{p0} 分钟', p0=duration) if duration else _ui('永久'))
    return PENALTIES[mode]


def apply_penalty(bot, cid, uid, mid, settings, *, is_edit=False):
    mode = settings["flood_penalty"]
    if mode in ("none", "delete"):
        return _ui('不追加处罚') if mode == "none" else _ui('沿用全局累计规则')
    if is_edit:
        return _ui('编辑消息不追加成员处罚')
    if not bot.store.claim_flood_penalty(cid, uid, mid, mode):
        return None  # Preserve the first audit outcome on replay.
    if (not bot.is_supergroup(cid) or not bot.bot_can(cid, "can_restrict_members")
            or not bot.can_target(cid, uid)):
        return _ui('处罚失败：群类型、权限或目标身份不满足条件')
    # Refresh identity after claiming, including left/restricted membership state.
    from .api import TelegramAPIError
    try:
        person = bot.api.call("getChatMember", chat_id=cid, user_id=uid)
    except TelegramAPIError:
        return _ui('处罚失败：无法核验目标身份')
    if (not isinstance(person, dict) or person.get("status") not in ("member", "restricted")
            or person.get("is_member") is False or (person.get("user") or {}).get("is_bot")):
        return _ui('跳过处罚：目标不是可管理的在群普通成员')
    if mode == "warn":
        count = bot.store.warning(cid, uid)
        bot.store.stat(cid, "actions")
        bot.log(cid, _ui('防刷屏警告 {p0}/{p1}', p0=count, p1=settings['warn_limit']), target=uid)
        if count < settings["warn_limit"]:
            return _ui('已警告 {p0}/{p1}', p0=count, p1=settings['warn_limit'])
    if mode in ("warn", "mute"):
        from .service import RESTRICTED
        ok = bot.api.safe("restrictChatMember", chat_id=cid, user_id=uid, permissions=RESTRICTED,
                          until_date=int(time.time())+settings["flood_mute_minutes"]*60)
        label = _ui('禁言 {p0} 分钟', p0=settings['flood_mute_minutes'])
        if ok is True and mode == "warn":
            bot.store.clear_warning(cid, uid)
    elif mode == "kick":
        # Telegram's default unban call removes a current member and permits
        # rejoining, without first applying a permanent ban.
        ok = bot.api.safe("unbanChatMember", chat_id=cid, user_id=uid, only_if_banned=False)
        label = _ui('踢出，允许重新加入')
    else:
        duration = settings["flood_ban_minutes"]
        options = {"until_date": int(time.time())+duration*60} if duration else {}
        ok = bot.api.safe("banChatMember", chat_id=cid, user_id=uid, **options)
        label = _ui('踢出并封禁 ')+(_ui('{p0} 分钟', p0=duration) if duration else _ui('永久'))
    if ok is True:
        bot.store.stat(cid, "actions")
        bot.log(cid, _ui('防刷屏：')+label, target=uid)
        return _ui('已')+label
    return (_ui('已警告；达到上限但禁言失败，警告保留') if mode == "warn" else _ui('处罚失败：')+label)
