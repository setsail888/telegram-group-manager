"""Human-readable group commands with authorization on every mutation."""
import time

from .moderation import normalize, normalize_domain
from .economy_commands import (
    MEMBER_COMMANDS as ECONOMY_MEMBER_COMMANDS,
    ADMIN_COMMANDS as ECONOMY_ADMIN_COMMANDS,
    handle_member as handle_economy_member,
    handle_admin as handle_economy_admin,
)
from .points import (ADMIN_COMMANDS as POINT_ADMIN_COMMANDS,
                     MEMBER_COMMANDS as POINT_MEMBER_COMMANDS,
                     handle_admin_command as handle_points_admin,
                     handle_member_command as handle_points_member)
from .reply_rules import media_from_reply, parse_buttons, parse_keywords
from .scheduler import format_run, next_occurrence, parse_new_schedule
from .store import TOGGLES

SWITCHES = {
    "links": "block_links", "forward": "block_forward",
    "flood": "anti_flood", "captcha": "verify_enabled",
    "welcome": "welcome_enabled", "bots": "block_new_bots",
    "imageflood": "repeat_photo_enabled",
}
ADMIN_ONLY = {
    "panel", "settings", "check", "id", "stats", "statweek", "top7",
    "schedule", "links", "forward", "flood", "captcha", "bots",
    "welcome", "addword", "delword", "words", "allowdomain", "deldomain", "domains",
    "setrules", "setwelcome", "addreply", "delreply", "replies", "setlog",
    "warn", "unwarn", "mute", "unmute", "ban", "unban", "kick", "dban",
    "imageflood", "imagelimit", "imagewindow", "ocr", "ocrconfidence",
} | POINT_ADMIN_COMMANDS | ECONOMY_ADMIN_COMMANDS
COMMANDS = ADMIN_ONLY | POINT_MEMBER_COMMANDS | ECONOMY_MEMBER_COMMANDS | {"help", "rules"}


def _deny_if_missing(bot, chat_id, permission, label):
    if not bot.bot_can(chat_id, permission):
        bot.send(chat_id, f"⚠️ 无法{label}：请先授予机器人对应的管理员权限。")
        return True
    return False


def _toggle(bot, chat_id, cmd, argument):
    setting = SWITCHES[cmd]
    if argument.lower() not in ("on", "off"):
        bot.send(chat_id, f"用法：/{cmd} on 或 /{cmd} off")
        return
    turn_on = argument.lower() == "on"
    if turn_on:
        if cmd in ("captcha", "bots") and not bot.is_supergroup(chat_id):
            bot.send(chat_id, "⚠️ 本功能仅支持 Telegram 超级群组。")
            return
        permission = ("can_restrict_members" if cmd in ("captcha", "bots")
                      else "can_delete_messages" if cmd in (
                          "links", "forward", "flood", "imageflood") else None)
        if permission and _deny_if_missing(bot, chat_id, permission, "启用该功能"):
            return
    bot.store.set(chat_id, setting, turn_on)
    bot.send(chat_id, f"✅ {cmd} 已{'开启' if turn_on else '关闭'}。")
    bot.log(chat_id, f"管理员更改 {cmd} = {turn_on}")


def _target_from_reply(message):
    reply = message.get("reply_to_message") or {}
    sender = reply.get("from") or {}
    if not sender.get("id") or sender.get("is_bot") or reply.get("sender_chat"):
        return None
    return int(sender["id"])


def _schedule_command(bot, chat_id, actor, argument):
    if not argument or argument.strip() in ("help", "?"):
        bot.send(chat_id,
            "📅 定时公告（以服务器 TZ 时区为准）\n"
            "/schedule daily 09:00 | 早安 | 大家早上好！\n"
            "/schedule weekly mon 19:30 | 周提醒 | 今天记得查看群规\n"
            "/schedule once 2026-10-20 15:00 | 活动提醒 | 活动即将开始\n"
            "/schedule list 查看任务\n"
            "/schedule pause 编号、/schedule resume 编号、/schedule del 编号\n"
            "每群最多 20 条，暂停或删除不会影响其他群。")
        return
    action, _, rest = argument.partition(" ")
    action = action.lower()
    if action in ("once", "daily", "weekly"):
        mode, rule, title, content = parse_new_schedule(argument)
        when = next_occurrence(mode, rule, bot.store.tz)
        task_id = bot.store.add_schedule(
            chat_id, actor, title, content, mode, rule, when)
        bot.send(chat_id,
                 f"✅ 创建定时公告 #{task_id}：{title}\n"
                 f"下次发送：{format_run(when, bot.store.tz)}"
                 f"（{bot.store.tz.key}）")
        bot.log(chat_id, f"创建定时公告 #{task_id}", actor=actor)
        return
    if action == "list":
        items = bot.store.schedules(chat_id)
        text = ("📅 本群定时公告\n"
                + ("\n".join(
                    f"#{item['id']} {item['title'][:35]} "
                    f"{'✅' if item['enabled'] else '⏸'} "
                    f"{format_run(item['next_run'], bot.store.tz)}"
                    for item in items) or "暂无定时消息"))
        bot.send(chat_id, text)
        return
    if action not in ("pause", "resume", "del"):
        raise ValueError("未知定时指令。发送 /schedule 查看格式")
    try:
        task_id = int(rest.strip())
        if task_id <= 0:
            raise ValueError()
    except ValueError:
        raise ValueError(f"用法：/schedule {action} 正整数编号") from None
    item = bot.store.schedule(chat_id, task_id)
    if not item:
        raise ValueError("该编号不属于本群，或任务已被删除")
    if action == "del":
        bot.store.delete_schedule(chat_id, task_id)
    elif action == "pause":
        bot.store.toggle_schedule(chat_id, task_id, False)
    else:
        next_run = next_occurrence(item["mode"], item["rule"], bot.store.tz)
        bot.store.toggle_schedule(chat_id, task_id, True, next_run=next_run)
    names = {"del": "删除", "pause": "暂停", "resume": "恢复"}
    bot.send(chat_id, f"✅ 定时公告 #{task_id} 已{names[action]}。")
    bot.log(chat_id, f"{names[action]}定时公告 #{task_id}", actor=actor)


def _action(bot, message, command, argument, actor):
    chat_id = message["chat"]["id"]
    target = _target_from_reply(message)
    if command == "unban" and target is None:
        try:
            target = int(argument.strip())
        except ValueError:
            pass
    if target is None:
        bot.send(chat_id, f"⚠️ /{command} 请回复目标成员的消息。"
                 + ("或使用 /unban 数字ID。" if command == "unban" else ""))
        return
    if not bot.can_target(chat_id, target):
        bot.send(chat_id, "⚠️ 为安全起见，不能对机器人、管理员或无法核验身份的目标执行操作。")
        return
    if command in ("warn", "unwarn"):
        if command == "warn":
            count = bot.store.warning(chat_id, target)
            bot.store.stat(chat_id, "actions")
            bot.log(chat_id, f"警告 {count}/{bot.store.settings(chat_id)['warn_limit']}",
                    actor=actor, target=target)
            if count >= bot.store.settings(chat_id)["warn_limit"]:
                if _deny_if_missing(bot, chat_id, "can_restrict_members", "自动禁言"):
                    return
                from .service import RESTRICTED
                successful = bot.api.safe(
                    "restrictChatMember", chat_id=chat_id, user_id=target,
                    permissions=RESTRICTED,
                    until_date=int(time.time()) + 3600)
                if successful is True:
                    bot.store.clear_warning(chat_id, target)
                    bot.store.stat(chat_id, "actions")
                    bot.log(chat_id, "达到警告上限，禁言 60 分钟", actor=actor, target=target)
        else:
            bot.store.clear_warning(chat_id, target)
            bot.log(chat_id, "清除警告", actor=actor, target=target)
        return  # Silent in public chat by design.
    if _deny_if_missing(bot, chat_id, "can_restrict_members", "管理成员"):
        return
    if command == "unmute":
        success = bot.unrestrict(chat_id, target)
    elif command == "mute":
        try:
            minutes = int(argument or "60")
        except ValueError:
            bot.send(chat_id, "禁言时长请填写整数分钟，例如 /mute 60")
            return
        if not 1 <= minutes <= 10080:
            bot.send(chat_id, "禁言时长为 1 到 10080 分钟。")
            return
        from .service import RESTRICTED
        success = bot.api.safe(
            "restrictChatMember", chat_id=chat_id, user_id=target,
            permissions=RESTRICTED, until_date=int(time.time()) + minutes * 60) is True
    elif command in ("ban", "dban", "kick"):
        success = bot.api.safe("banChatMember", chat_id=chat_id, user_id=target) is True
        if success and command == "kick":
            success = bot.api.safe("unbanChatMember", chat_id=chat_id, user_id=target,
                                   only_if_banned=True) is True
    elif command == "unban":
        success = bot.api.safe("unbanChatMember", chat_id=chat_id, user_id=target,
                               only_if_banned=True) is True
    else:
        return
    if not success:
        bot.send(chat_id, "⚠️ 执行失败，请检查机器人权限或目标用户状态。")
        return
    if command == "dban":
        source_id = (message.get("reply_to_message") or {}).get("message_id")
        if source_id:
            bot.api.safe("deleteMessage", chat_id=chat_id, message_id=source_id)
        # Admin command itself deliberately remains visible (Rose issue).
    bot.store.stat(chat_id, "actions")
    bot.log(chat_id, command, actor=actor, target=target)
    # No success notice in the public group: audit logs can go to another chat.


def handle_command(bot, message):
    text = message.get("text") or ""
    if not text.startswith("/"):
        return False
    head, _, argument = text.partition(" ")
    command, at, mentioned = head[1:].partition("@")
    command = command.lower()
    if at and mentioned.casefold() != bot.username.casefold():
        return False
    if command not in COMMANDS:
        return False
    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}
    user_id = sender.get("id")
    if command in ("rules", "help"):
        # Don't let a non-admin cloak advertising text inside /rules URL.
        if argument.strip() or not user_id:
            return False
        key = (chat_id, user_id, command)
        now = time.monotonic()
        previous = bot._public_command_time.get(key)
        if previous is not None and now - previous < 30:
            return False  # Feed excessive repeated commands to flood filter.
        bot._public_command_time[key] = now
        if len(bot._public_command_time) > 15000:
            for old, tick in list(bot._public_command_time.items()):
                if now - tick > 60:
                    del bot._public_command_time[old]
        if command == "rules":
            bot.send(chat_id, f"📖 群规\n{bot.store.settings(chat_id)['rules_text']}")
        else:
            bot.send(chat_id, bot.help_text())
        return True
    if command in POINT_MEMBER_COMMANDS:
        return handle_points_member(bot, message, command, argument)
    if command in ECONOMY_MEMBER_COMMANDS:
        return handle_economy_member(bot, message, command, argument)
    # Anonymous admin messages are never accepted as control commands:
    # authorization must be tied to an auditable user id.
    if not user_id or not bot.is_admin(chat_id, user_id):
        return False
    argument = argument.strip()
    try:
        settings = bot.store.settings(chat_id)
        if command == "panel":
            bot.send(chat_id, f"⚙️ 私聊 @{bot.username}，发送 /start 打开管理面板。")
        elif command == "settings":
            state = "\n".join(f"{key}: {'开' if settings[key] else '关'}"
                              for key in TOGGLES)
            bot.send(chat_id, f"⚙️ 本群设置\n{state}\n日志目标：{settings['log_chat_id'] or '关闭'}")
        elif command == "check":
            bot.send(chat_id, "🛡️ 权限检测\n"
                     f"删除消息：{'✅' if bot.bot_can(chat_id, 'can_delete_messages') else '❌'}\n"
                     f"限制成员：{'✅' if bot.bot_can(chat_id, 'can_restrict_members') else '❌'}")
        elif command == "id":
            bot.send(chat_id, f"🆔 当前群组 ID：{chat_id}")
        elif command == "stats":
            data = bot.store.stats(chat_id)
            bot.send(chat_id, f"📊 今日消息 {data.get('messages',0)} | "
                     f"删除 {data.get('deleted',0)} | "
                     f"新成员 {data.get('joins',0)} | "
                     f"处罚 {data.get('actions',0)}")
        elif command == "statweek":
            data = bot.store.stats_days(chat_id, 7)
            bot.send(chat_id, f"📈 最近7天：消息 {data.get('messages',0)} | "
                     f"删除 {data.get('deleted',0)} | "
                     f"入群 {data.get('joins',0)} | "
                     f"处罚 {data.get('actions',0)}")
        elif command == "top7":
            ranking = bot.store.top_activity(chat_id, days=7, limit=10)
            bot.send(chat_id, "🏆 最近7天活跃成员（ID/消息数）\n" + (
                "\n".join(f"{index}. {item['user_id']} — {item['count']}"
                          for index, item in enumerate(ranking, 1)) or "暂无记录"))
        elif command == "schedule":
            _schedule_command(bot, chat_id, user_id, argument)
        elif command in POINT_ADMIN_COMMANDS:
            handle_points_admin(bot, message, command, argument)
        elif command in ECONOMY_ADMIN_COMMANDS:
            handle_economy_admin(bot, message, command, argument)
        elif command == "ocr":
            mode = argument.lower().strip()
            if mode not in ("off", "review", "delete"):
                raise ValueError("用法：/ocr off | review | delete")
            if mode != "off" and not bot.ocr.available:
                raise ValueError(
                    "OCR 引擎尚未启用。请配置 WITH_OCR=1、OCR_ENABLED=1 并构建容器。")
            if mode == "review" and not settings["log_chat_id"]:
                raise ValueError("先建立独立管理日志群并执行 /setlog 目标群ID，才能启用 OCR review")
            if mode == "delete" and not bot.bot_can(chat_id, "can_delete_messages"):
                raise ValueError("删除模式需要机器人拥有删除消息的管理员权限")
            bot.store.set(chat_id, "ocr_mode", mode)
            bot.send(chat_id, f"🔎 OCR 已设为 {mode}。"
                     "review 仅记录到管理日志，不删除；delete 有误判风险。")
            bot.log(chat_id, f"OCR 模式修改为 {mode}", actor=user_id)
        elif command == "ocrconfidence":
            try:
                threshold = int(argument)
            except ValueError:
                raise ValueError("用法：/ocrconfidence 70（40–95）") from None
            bot.store.set(chat_id, "ocr_confidence", threshold)
            bot.send(chat_id, f"🔎 OCR 可信度门槛已设为 {threshold}。")
        elif command == "imagelimit":
            try:
                limit = int(argument)
            except ValueError:
                raise ValueError("用法：/imagelimit 3（2-10 次）") from None
            bot.store.set(chat_id, "repeat_photo_limit", limit)
            bot.send(chat_id, f"✅ 重复图片阈值设为 {limit} 次。")
        elif command == "imagewindow":
            try:
                seconds = int(argument)
            except ValueError:
                raise ValueError("用法：/imagewindow 60（30-600 秒）") from None
            bot.store.set(chat_id, "repeat_photo_window", seconds)
            bot.send(chat_id, f"✅ 图片检测时间窗口设为 {seconds} 秒。")
        elif command in SWITCHES:
            _toggle(bot, chat_id, command, argument)
        elif command in ("addword", "delword"):
            key = normalize(argument)
            if not 2 <= len(key) <= 80:
                raise ValueError("违禁词长度应为 2-80 个字符")
            if command == "addword":
                bot.store.add_filter(chat_id, "word", key, argument)
            else:
                bot.store.del_filter(chat_id, "word", key)
            bot.send(chat_id, "✅ 违禁词已更新。")
        elif command == "words":
            words = [r["value"] for r in bot.store.filters(chat_id, "word")]
            bot.send(chat_id, "⛔ 违禁词：\n" + (", ".join(words[:75]) or "暂无"))
        elif command in ("allowdomain", "deldomain"):
            domain = normalize_domain(argument)
            if not domain:
                raise ValueError("请输入有效域名，例如 example.com")
            if command == "allowdomain":
                bot.store.add_filter(chat_id, "domain", domain, domain)
            else:
                bot.store.del_filter(chat_id, "domain", domain)
            bot.send(chat_id, "✅ 域名白名单已更新。")
        elif command == "domains":
            domains = [r["value"] for r in bot.store.filters(chat_id, "domain")]
            bot.send(chat_id, "🌐 允许的域名：\n" + (", ".join(domains[:75]) or "暂无"))
        elif command == "setrules":
            if not argument:
                raise ValueError("用法：/setrules 群规内容")
            bot.store.set(chat_id, "rules_text", argument)
            bot.send(chat_id, "✅ 群规已保存。")
        elif command == "setwelcome":
            if not argument:
                raise ValueError("用法：/setwelcome 欢迎 {name}！")
            bot.store.set(chat_id, "welcome_text", argument)
            bot.send(chat_id, "✅ 欢迎语已保存。")
        elif command in ("addreply", "delreply"):
            if command == "addreply":
                parts = argument.split("|", 2)
                if len(parts) < 2 or not 1 <= len(parts[1].strip()) <= 1000:
                    raise ValueError("用法：/addreply 关键词1,关键词2 | 回复内容 | 按钮名称 - https://网址")
                keyword, reply = parts[0].strip(), parts[1].strip()
                buttons = parse_buttons(parts[2]) if len(parts) == 3 else []
                media_type, media_id = media_from_reply(message)
            else:
                keyword = argument
            key, labels = parse_keywords(keyword)
            if command == "addreply":
                bot.store.add_reply(chat_id, key, labels, reply,
                                    buttons=buttons, media_type=media_type,
                                    media_id=media_id)
            else:
                bot.store.del_reply(chat_id, key)
            bot.send(chat_id, "✅ 自动回复已更新。")
        elif command == "replies":
            replies = bot.store.replies(chat_id)
            bot.send(chat_id, "💬 自动回复：\n" + (
                "\n".join(f"{'🖼️ ' if x['media_type'] else ''}{x['keyword']}"
                          for x in replies[:60]) or "暂无"))
        elif command == "setlog":
            if argument.lower() == "off":
                bot.store.set(chat_id, "log_chat_id", 0)
            else:
                try:
                    target = int(argument)
                except ValueError:
                    raise ValueError("用法：/setlog -100123456789 或 /setlog off")
                # Prevent one group's admin from redirecting private logs to
                # an unrelated group or exposing moderation notices publicly.
                if target == chat_id:
                    raise ValueError("日志群必须是独立群组/频道，不能设为当前群。")
                if target >= 0 or not bot.is_admin(target, user_id):
                    raise ValueError("你必须同时是日志目标群的管理员。")
                member = bot.api.safe("getChatMember", chat_id=target,
                                      user_id=bot.bot_id)
                if not member or member.get("status") not in ("administrator", "creator"):
                    raise ValueError("请先把机器人设为日志目标群的管理员。")
                destination = bot.api.safe("getChat", chat_id=target)
                if (not destination or
                    (destination.get("type") == "channel" and
                     member.get("status") != "creator" and
                     not member.get("can_post_messages", False))):
                    raise ValueError("机器人无法向目标频道发布日志。")
                bot.store.set(chat_id, "log_chat_id", target)
            bot.send(chat_id, "✅ 日志目标已更新；处罚仍不会在当前群公开播报。")
        elif command in ("warn", "unwarn", "mute", "unmute", "ban", "unban", "kick", "dban"):
            _action(bot, message, command, argument, user_id)
    except ValueError as exc:
        bot.send(chat_id, f"⚠️ {str(exc)[:180]}")
    return True
