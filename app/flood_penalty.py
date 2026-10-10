"""Automatic flood penalties run only after a confirmed trigger deletion."""
import time

PENALTIES = {"none": "仅撤回消息，不处罚", "warn": "警告", "mute": "禁言",
             "kick": "踢出（允许重新加入）", "ban": "踢出并封禁", "delete": "仅删除触发消息（沿用累计规则）"}
FLOOD_SETTINGS = ("flood_penalty", "flood_mute_minutes", "flood_ban_minutes", "warn_limit",
                  "anti_flood", "cross_flood_mode", "cross_flood_limit", "cross_flood_users",
                  "cross_flood_window", "cross_flood_min_chars", "observe_only", "auto_mute_enabled")


def penalty_label(settings):
    mode = settings["flood_penalty"]
    if mode == "delete":
        return "单次仅删除触发消息"
    if mode == "mute":
        return f"删除并禁言 {settings['flood_mute_minutes']} 分钟"
    if mode == "warn":
        return f"删除并警告，达到{settings['warn_limit']}次禁言{settings['flood_mute_minutes']}分钟"
    if mode == "ban":
        duration = settings["flood_ban_minutes"]
        return "踢出并封禁 "+(f"{duration} 分钟" if duration else "永久")
    return PENALTIES[mode]


def apply_penalty(bot, cid, uid, mid, settings, *, is_edit=False):
    mode = settings["flood_penalty"]
    if mode in ("none", "delete"):
        return "不追加处罚" if mode == "none" else "沿用全局累计规则"
    if is_edit:
        return "编辑消息不追加成员处罚"
    if not bot.store.claim_flood_penalty(cid, uid, mid, mode):
        return None  # Preserve the first audit outcome on replay.
    if (not bot.is_supergroup(cid) or not bot.bot_can(cid, "can_restrict_members")
            or not bot.can_target(cid, uid)):
        return "处罚失败：群类型、权限或目标身份不满足条件"
    # Refresh identity after claiming, including left/restricted membership state.
    from .api import TelegramAPIError
    try:
        person = bot.api.call("getChatMember", chat_id=cid, user_id=uid)
    except TelegramAPIError:
        return "处罚失败：无法核验目标身份"
    if (not isinstance(person, dict) or person.get("status") not in ("member", "restricted")
            or person.get("is_member") is False or (person.get("user") or {}).get("is_bot")):
        return "跳过处罚：目标不是可管理的在群普通成员"
    if mode == "warn":
        count = bot.store.warning(cid, uid)
        bot.store.stat(cid, "actions")
        bot.log(cid, f"防刷屏警告 {count}/{settings['warn_limit']}", target=uid)
        if count < settings["warn_limit"]:
            return f"已警告 {count}/{settings['warn_limit']}"
    if mode in ("warn", "mute"):
        from .service import RESTRICTED
        ok = bot.api.safe("restrictChatMember", chat_id=cid, user_id=uid, permissions=RESTRICTED,
                          until_date=int(time.time())+settings["flood_mute_minutes"]*60)
        label = f"禁言 {settings['flood_mute_minutes']} 分钟"
        if ok is True and mode == "warn":
            bot.store.clear_warning(cid, uid)
    elif mode == "kick":
        # Telegram's default unban call removes a current member and permits
        # rejoining, without first applying a permanent ban.
        ok = bot.api.safe("unbanChatMember", chat_id=cid, user_id=uid, only_if_banned=False)
        label = "踢出，允许重新加入"
    else:
        duration = settings["flood_ban_minutes"]
        options = {"until_date": int(time.time())+duration*60} if duration else {}
        ok = bot.api.safe("banChatMember", chat_id=cid, user_id=uid, **options)
        label = "踢出并封禁 "+(f"{duration} 分钟" if duration else "永久")
    if ok is True:
        bot.store.stat(cid, "actions")
        bot.log(cid, "防刷屏："+label, target=uid)
        return "已"+label
    return ("已警告；达到上限但禁言失败，警告保留" if mode == "warn" else "处罚失败："+label)
