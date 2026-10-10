"""Human-readable group commands with authorization on every mutation."""
from .i18n import error_text, msg as _ui, join_text
import re
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
from .store import TOGGLES, NUMBER_LIMITS
from .word_library import describe_hit
from .variants import context_matches

SWITCHES = {
    "links": "block_links", "forward": "block_forward",
    "flood": "anti_flood", "captcha": "verify_enabled",
    "welcome": "welcome_enabled", "bots": "block_new_bots",
    "imageflood": "repeat_photo_enabled",
    "spam": "spam_guard_enabled",
    "mediaflood": "media_flood_enabled",
    "wordloose": "word_loose_match",
    "automute": "auto_mute_enabled",
    "nameguard": "name_guard_enabled",
}
ADMIN_ONLY = {
    "panel", "settings", "check", "id", "stats", "statweek", "top7",
    "schedule", "links", "forward", "flood", "captcha", "bots",
    "welcome", "addword", "delword", "words", "allowdomain", "deldomain", "domains",
    "setrules", "setwelcome", "addreply", "delreply", "replies", "setlog",
    "warn", "unwarn", "mute", "unmute", "ban", "unban", "kick", "dban",
    "imageflood", "imagelimit", "imagewindow", "ocr", "ocrconfidence",
    "spam", "mediaflood", "wordloose", "automute", "nameguard",
    "guard", "guardset", "addwords", "delwords",
    "wordtest", "wordexcept", "delwordexcept",
} | POINT_ADMIN_COMMANDS | ECONOMY_ADMIN_COMMANDS
COMMANDS = ADMIN_ONLY | POINT_MEMBER_COMMANDS | ECONOMY_MEMBER_COMMANDS | {"help", "rules", "menu", "language"}


def _deny_if_missing(bot, chat_id, permission, label):
    if not bot.bot_can(chat_id, permission):
        bot.send(chat_id, _ui('⚠️ 无法{p0}：请先授予机器人对应的管理员权限。', p0=label))
        return True
    return False


def _toggle(bot, chat_id, cmd, argument):
    setting = SWITCHES[cmd]
    if argument.lower() not in ("on", "off"):
        bot.send(chat_id, _ui('用法：/{p0} on 或 /{p1} off', p0=cmd, p1=cmd))
        return
    turn_on = argument.lower() == "on"
    if turn_on:
        if cmd in ("captcha", "bots", "automute", "nameguard") and not bot.is_supergroup(chat_id):
            bot.send(chat_id, _ui('⚠️ 本功能仅支持 Telegram 超级群组。'))
            return
        permission = ("can_restrict_members" if cmd in ("captcha", "bots", "automute",
                                                        "nameguard")
                      else "can_delete_messages" if cmd in (
                          "links", "forward", "flood", "imageflood",
                          "spam", "mediaflood", "wordloose") else None)
        if permission and _deny_if_missing(bot, chat_id, permission, _ui('启用该功能')):
            return
    bot.store.set(chat_id, setting, turn_on)
    bot.send(chat_id, _ui('✅ {p0} 已{p1}。', p0=cmd, p1=_ui('开启') if turn_on else _ui('关闭')))
    bot.log(chat_id, _ui('管理员更改 {p0} = {p1}', p0=cmd, p1=turn_on))


def _target_from_reply(message):
    reply = message.get("reply_to_message") or {}
    sender = reply.get("from") or {}
    if not sender.get("id") or sender.get("is_bot") or reply.get("sender_chat"):
        return None
    return int(sender["id"])


def _schedule_command(bot, chat_id, actor, argument):
    if not argument or argument.strip() in ("help", "?"):
        bot.send(chat_id,
            _ui('📅 定时公告（以服务器 TZ 时区为准）\n/schedule daily 09:00 | 早安 | 大家早上好！\n/schedule weekly mon 19:30 | 周提醒 | 今天记得查看群规\n/schedule once 2026-10-20 15:00 | 活动提醒 | 活动即将开始\n/schedule list 查看任务\n/schedule pause 编号、/schedule resume 编号、/schedule del 编号\n每群最多 20 条，暂停或删除不会影响其他群。'))
        return
    action, _, rest = argument.partition(" ")
    action = action.lower()
    if action in ("once", "daily", "weekly"):
        mode, rule, title, content = parse_new_schedule(argument)
        when = next_occurrence(mode, rule, bot.store.tz)
        task_id = bot.store.add_schedule(
            chat_id, actor, title, content, mode, rule, when)
        bot.send(chat_id,
                 _ui('✅ 创建定时公告 #{p0}：{p1}\n下次发送：{p2}（{p3}）', p0=task_id, p1=title, p2=format_run(when, bot.store.tz), p3=bot.store.tz.key))
        bot.log(chat_id, _ui('创建定时公告 #{p0}', p0=task_id), actor=actor)
        return
    if action == "list":
        items = bot.store.schedules(chat_id)
        text = (_ui('📅 本群定时公告\n')
                + (join_text('\n', (_ui('#{p0} {p1} {p2} {p3}', p0=item['id'], p1=item['title'][:35], p2='✅' if item['enabled'] else '⏸', p3=format_run(item['next_run'], bot.store.tz)) for item in items)) or _ui('暂无定时消息')))
        bot.send(chat_id, text)
        return
    if action not in ("pause", "resume", "del"):
        raise ValueError(_ui('未知定时指令。发送 /schedule 查看格式'))
    try:
        task_id = int(rest.strip())
        if task_id <= 0:
            raise ValueError()
    except ValueError:
        raise ValueError(_ui('用法：/schedule {p0} 正整数编号', p0=action)) from None
    item = bot.store.schedule(chat_id, task_id)
    if not item:
        raise ValueError(_ui('该编号不属于本群，或任务已被删除'))
    if action == "del":
        bot.store.delete_schedule(chat_id, task_id)
    elif action == "pause":
        bot.store.toggle_schedule(chat_id, task_id, False)
    else:
        next_run = next_occurrence(item["mode"], item["rule"], bot.store.tz)
        bot.store.toggle_schedule(chat_id, task_id, True, next_run=next_run)
    names = {"del": _ui('删除'), "pause": _ui('暂停'), "resume": _ui('恢复')}
    bot.send(chat_id, _ui('✅ 定时公告 #{p0} 已{p1}。', p0=task_id, p1=names[action]))
    bot.log(chat_id, _ui('{p0}定时公告 #{p1}', p0=names[action], p1=task_id), actor=actor)


def _action(bot, message, command, argument, actor):
    chat_id = message["chat"]["id"]
    target = _target_from_reply(message)
    if command == "unban" and target is None:
        try:
            target = int(argument.strip())
        except ValueError:
            pass
    if target is None:
        bot.send(chat_id, _ui('⚠️ /{p0} 请回复目标成员的消息。', p0=command)
                 + (_ui('或使用 /unban 数字ID。') if command == "unban" else ""))
        return
    if not bot.can_target(chat_id, target):
        bot.send(chat_id, _ui('⚠️ 为安全起见，不能对机器人、管理员或无法核验身份的目标执行操作。'))
        return
    if command in ("warn", "unwarn"):
        if command == "warn":
            count = bot.store.warning(chat_id, target)
            bot.store.stat(chat_id, "actions")
            bot.log(chat_id, _ui('警告 {p0}/{p1}', p0=count, p1=bot.store.settings(chat_id)['warn_limit']),
                    actor=actor, target=target)
            if count >= bot.store.settings(chat_id)["warn_limit"]:
                if _deny_if_missing(bot, chat_id, "can_restrict_members", _ui('自动禁言')):
                    return
                from .service import RESTRICTED
                successful = bot.api.safe(
                    "restrictChatMember", chat_id=chat_id, user_id=target,
                    permissions=RESTRICTED,
                    until_date=int(time.time()) + 3600)
                if successful is True:
                    bot.store.clear_warning(chat_id, target)
                    bot.store.stat(chat_id, "actions")
                    bot.log(chat_id, _ui('达到警告上限，禁言 60 分钟'), actor=actor, target=target)
        else:
            bot.store.clear_warning(chat_id, target)
            bot.log(chat_id, _ui('清除警告'), actor=actor, target=target)
        return  # Silent in public chat by design.
    if _deny_if_missing(bot, chat_id, "can_restrict_members", _ui('管理成员')):
        return
    if command == "unmute":
        success = bot.unrestrict(chat_id, target)
    elif command == "mute":
        try:
            minutes = int(argument or "60")
        except ValueError:
            bot.send(chat_id, _ui('禁言时长请填写整数分钟，例如 /mute 60'))
            return
        if not 1 <= minutes <= 10080:
            bot.send(chat_id, _ui('禁言时长为 1 到 10080 分钟。'))
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
        bot.send(chat_id, _ui('⚠️ 执行失败，请检查机器人权限或目标用户状态。'))
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
    if command in ("rules", "help", "menu", "language"):
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
        if command == "menu":
            if sender.get("is_bot") or message.get("sender_chat"):
                return False
            bot.send(chat_id, _ui('👤 点击打开本群成员服务：签到、积分、商城、订单、免费抽奖和群规。'),
                     reply_markup={"inline_keyboard": [[{"text": _ui('👤 成员服务'),
                     "url": _ui('https://t.me/{p0}?start=member_{p1}', p0=bot.username, p1=abs(chat_id))}]]})
        elif command == "language":
            bot.send(chat_id, _ui("🌐 私聊机器人切换个人语言；管理员可在群设置中修改群内提示语言。"),
                     reply_markup={"inline_keyboard": [[{"text": _ui("🌐 界面语言 / Language"),
                     "url": f"https://t.me/{bot.username}?start=language"}]]})
        elif command == "rules":
            bot.send(chat_id, _ui('📖 群规\n{p0}', p0=bot.store.content_text(chat_id, 'rules_text')))
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
            bot.send(chat_id, _ui('⚙️ 私聊 @{p0}，发送 /start 打开管理面板。', p0=bot.username))
        elif command == "settings":
            state = join_text('\n', (_ui('{p0}: {p1}', p0=key, p1=_ui('开') if settings[key] else _ui('关')) for key in sorted(TOGGLES)))
            bot.send(chat_id, _ui('⚙️ 本群设置\n{p0}\n日志目标：{p1}', p0=state, p1=settings['log_chat_id'] or _ui('关闭')))
        elif command == "check":
            bot.send(chat_id, _ui('🛡️ 权限检测\n删除消息：{p0}\n限制成员：{p1}', p0='✅' if bot.bot_can(chat_id, 'can_delete_messages') else '❌', p1='✅' if bot.bot_can(chat_id, 'can_restrict_members') else '❌'))
        elif command == "id":
            target = _target_from_reply(message)
            bot.send(chat_id, _ui('🆔 当前群组 ID：{p0}\n你的ID：{p1}', p0=chat_id, p1=user_id)
                     + (_ui('\n回复成员ID：{p0}', p0=target) if target else ""))
        elif command == "stats":
            data = bot.store.stats(chat_id)
            bot.send(chat_id, _ui('📊 今日消息 {p0} | 删除 {p1} | 新成员 {p2} | 处罚 {p3}', p0=data.get('messages', 0), p1=data.get('deleted', 0), p2=data.get('joins', 0), p3=data.get('actions', 0)))
        elif command == "statweek":
            data = bot.store.stats_days(chat_id, 7)
            bot.send(chat_id, _ui('📈 最近7天：消息 {p0} | 删除 {p1} | 入群 {p2} | 处罚 {p3}', p0=data.get('messages', 0), p1=data.get('deleted', 0), p2=data.get('joins', 0), p3=data.get('actions', 0)))
        elif command == "top7":
            ranking = bot.store.top_activity(chat_id, days=7, limit=10)
            bot.send(chat_id, _ui('🏆 最近7天活跃成员（ID/消息数）\n') + (
                join_text('\n', (_ui('{p0}. {p1} — {p2}', p0=index, p1=item['user_id'], p2=item['count']) for index, item in enumerate(ranking, 1))) or _ui('暂无记录')))
        elif command == "schedule":
            _schedule_command(bot, chat_id, user_id, argument)
        elif command in POINT_ADMIN_COMMANDS:
            handle_points_admin(bot, message, command, argument)
        elif command in ECONOMY_ADMIN_COMMANDS:
            handle_economy_admin(bot, message, command, argument)
        elif command == "ocr":
            mode = argument.lower().strip()
            if mode not in ("off", "review", "delete"):
                raise ValueError(_ui('用法：/ocr off | review | delete'))
            if mode != "off" and not bot.ocr.available:
                raise ValueError(
                    _ui('OCR 引擎尚未启用。请配置 WITH_OCR=1、OCR_ENABLED=1 并构建容器。'))
            if mode == "review" and not settings["log_chat_id"]:
                raise ValueError(_ui('先建立独立管理日志群并执行 /setlog 目标群ID，才能启用 OCR review'))
            if mode == "delete" and not bot.bot_can(chat_id, "can_delete_messages"):
                raise ValueError(_ui('删除模式需要机器人拥有删除消息的管理员权限'))
            bot.store.set(chat_id, "ocr_mode", mode)
            bot.send(chat_id, _ui('🔎 OCR 已设为 {p0}。review 仅记录到管理日志，不删除；delete 有误判风险。', p0=mode))
            bot.log(chat_id, _ui('OCR 模式修改为 {p0}', p0=mode), actor=user_id)
        elif command == "ocrconfidence":
            try:
                threshold = int(argument)
            except ValueError:
                raise ValueError(_ui('用法：/ocrconfidence 70（40–95）')) from None
            bot.store.set(chat_id, "ocr_confidence", threshold)
            bot.send(chat_id, _ui('🔎 OCR 可信度门槛已设为 {p0}。', p0=threshold))
        elif command == "imagelimit":
            try:
                limit = int(argument)
            except ValueError:
                raise ValueError(_ui('用法：/imagelimit 3（2-10 次）')) from None
            bot.store.set(chat_id, "repeat_photo_limit", limit)
            bot.send(chat_id, _ui('✅ 重复图片阈值设为 {p0} 次。', p0=limit))
        elif command == "imagewindow":
            try:
                seconds = int(argument)
            except ValueError:
                raise ValueError(_ui('用法：/imagewindow 60（30-600 秒）')) from None
            bot.store.set(chat_id, "repeat_photo_window", seconds)
            bot.send(chat_id, _ui('✅ 图片检测时间窗口设为 {p0} 秒。', p0=seconds))
        elif command == "guard":
            conf = bot.store.settings(chat_id)
            bot.send(chat_id, _ui('🛡️ 防护高级设置\n/spam on|off 智能反垃圾：{p0}\n/wordloose on|off 拆字识别：{p1}\n/mediaflood on|off 媒体连发：{p2}\n/automute on|off 自动禁言：{p3}\n/nameguard on|off 超长昵称限制：{p4}\n调整阈值：/guardset flood_limit 7\n推荐在私聊 /start 的双列管理面板点击调整。', p0=conf['spam_guard_enabled'], p1=conf['word_loose_match'], p2=conf['media_flood_enabled'], p3=conf['auto_mute_enabled'], p4=conf['name_guard_enabled']))
        elif command == "guardset":
            name, sep, raw = argument.partition(" ")
            if name not in NUMBER_LIMITS or not sep:
                raise ValueError(_ui('用法：/guardset flood_limit 7（其他项见私聊管理面板）'))
            try:
                value = int(raw.strip())
            except ValueError:
                raise ValueError(_ui('设置值必须为整数')) from None
            bot.store.set(chat_id, name, value)
            bot.send(chat_id, _ui('✅ {p0} 已设为 {p1}。', p0=name, p1=value))
            bot.log(chat_id, _ui('修改防护阈值 {p0}={p1}', p0=name, p1=value), actor=user_id)
        elif command in ("addwords", "delwords"):
            labels = [v.strip() for v in re.split(r"[,，、;\n]+", argument) if v.strip()]
            if not 1 <= len(labels) <= 80:
                raise ValueError(_ui('请用逗号/换行分隔违禁词，每次 1–80 条'))
            pairs = []
            for value in labels:
                key = normalize(value)
                if not 2 <= len(key) <= 80:
                    raise ValueError(_ui('每条违禁词需为 2–80 个字符；没有保存任何内容'))
                pairs.append((key, value))
            if command == "addwords":
                changed = bot.store.add_filters_bulk(chat_id, "word", pairs)
            else:
                changed = sum(bot.store.del_filter(chat_id, "word", k)
                              for k in set(key for key, _value in pairs))
            bot.send(chat_id, _ui('✅ 已{p0} {p1} 条违禁词。', p0=_ui('导入') if command == 'addwords' else _ui('删除'), p1=changed))
            bot.log(chat_id, _ui('批量{p0}违禁词 {p1} 条', p0=_ui('导入') if command == 'addwords' else _ui('删除'), p1=changed),
                    actor=user_id)
        elif command == "wordtest":
            if not 2 <= len(argument) <= 1000:
                raise ValueError(_ui('用法：/wordtest 测试文字（2–1000字，不会删除）'))
            hit = bot.store.word_policy(chat_id).hit({"text": argument}, loose=settings["word_loose_match"], settings=settings)
            bot.send(chat_id, _ui('🧪 违禁词测试（不处罚）：') + describe_hit(hit, settings))
        elif command in ("wordexcept", "delwordexcept"):
            phrase, sep, context = argument.partition("|")
            keyword, context = normalize(phrase.strip()), normalize(context.strip())
            if not sep or not 2 <= len(keyword) <= 80 or not 3 <= len(context) <= 200:
                raise ValueError(_ui('用法：/{p0} 违禁词 | 完整允许文本', p0=command))
            if command == "wordexcept" and not context_matches(keyword, context, settings):
                raise ValueError(_ui('允许文本必须包含对应违禁词，才能避免错误配置'))
            key = _ui('{p0}|{p1}', p0=keyword, p1=context)
            if command == "wordexcept":
                bot.store.add_filter(chat_id, "word_exception", key, key)
            else:
                bot.store.del_filter(chat_id, "word_exception", key)
            bot.send(chat_id, _ui('✅ 定向例外已更新：只有完整匹配的消息豁免该违禁词。'))
            bot.log(chat_id, _ui('修改违禁词定向例外'), actor=user_id)
        elif command in SWITCHES:
            _toggle(bot, chat_id, command, argument)
        elif command in ("addword", "delword"):
            key = normalize(argument)
            if not 2 <= len(key) <= 80:
                raise ValueError(_ui('违禁词长度应为 2-80 个字符'))
            if command == "addword":
                bot.store.add_filter(chat_id, "word", key, argument)
            else:
                bot.store.del_filter(chat_id, "word", key)
            bot.send(chat_id, _ui('✅ 违禁词已更新。'))
        elif command == "words":
            words = [r["value"] for r in bot.store.filter_page(chat_id, "word", 0, page_size=30)]
            bot.send(chat_id, _ui('⛔ 自定义违禁词（前30条）：\n') + (join_text(', ', words) or _ui('暂无'))
                     + _ui('\n完整搜索、分类词库和文件导入请使用 /panel。'))
        elif command in ("allowdomain", "deldomain"):
            domain = normalize_domain(argument)
            if not domain:
                raise ValueError(_ui('请输入有效域名，例如 example.com'))
            if command == "allowdomain":
                bot.store.add_filter(chat_id, "domain", domain, domain)
            else:
                bot.store.del_filter(chat_id, "domain", domain)
            bot.send(chat_id, _ui('✅ 域名白名单已更新。'))
        elif command == "domains":
            domains = [r["value"] for r in bot.store.filters(chat_id, "domain")]
            bot.send(chat_id, _ui('🌐 允许的域名：\n') + (join_text(', ', domains[:75]) or _ui('暂无')))
        elif command == "setrules":
            if not argument:
                raise ValueError(_ui('用法：/setrules 群规内容'))
            bot.store.set(chat_id, "rules_text", argument)
            bot.send(chat_id, _ui('✅ 群规已保存。'))
        elif command == "setwelcome":
            if not argument:
                raise ValueError(_ui('用法：/setwelcome 欢迎 {name}！'))
            bot.store.set(chat_id, "welcome_text", argument)
            bot.send(chat_id, _ui('✅ 欢迎语已保存。'))
        elif command in ("addreply", "delreply"):
            if command == "addreply":
                parts = argument.split("|", 2)
                if len(parts) < 2 or not 1 <= len(parts[1].strip()) <= 1000:
                    raise ValueError(_ui('用法：/addreply 关键词1,关键词2 | 回复内容 | 按钮名称 - https://网址'))
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
            bot.send(chat_id, _ui('✅ 自动回复已更新。'))
        elif command == "replies":
            replies = bot.store.replies(chat_id)
            bot.send(chat_id, _ui('💬 自动回复：\n') + (
                join_text('\n', (_ui('{p0}{p1}', p0='🖼️ ' if x['media_type'] else '', p1=x['keyword']) for x in replies[:60])) or _ui('暂无')))
        elif command == "setlog":
            if argument.lower() == "off":
                bot.store.set(chat_id, "log_chat_id", 0)
            else:
                try:
                    target = int(argument)
                except ValueError:
                    raise ValueError(_ui('用法：/setlog -100123456789 或 /setlog off'))
                # Prevent one group's admin from redirecting private logs to
                # an unrelated group or exposing moderation notices publicly.
                if target == chat_id:
                    raise ValueError(_ui('日志群必须是独立群组/频道，不能设为当前群。'))
                if target >= 0 or not bot.is_admin(target, user_id):
                    raise ValueError(_ui('你必须同时是日志目标群的管理员。'))
                member = bot.api.safe("getChatMember", chat_id=target,
                                      user_id=bot.bot_id)
                if not member or member.get("status") not in ("administrator", "creator"):
                    raise ValueError(_ui('请先把机器人设为日志目标群的管理员。'))
                destination = bot.api.safe("getChat", chat_id=target)
                if (not destination or
                    (destination.get("type") == "channel" and
                     member.get("status") != "creator" and
                     not member.get("can_post_messages", False))):
                    raise ValueError(_ui('机器人无法向目标频道发布日志。'))
                bot.store.set(chat_id, "log_chat_id", target)
            bot.send(chat_id, _ui('✅ 日志目标已更新；处罚仍不会在当前群公开播报。'))
        elif command in ("warn", "unwarn", "mute", "unmute", "ban", "unban", "kick", "dban"):
            _action(bot, message, command, argument, user_id)
    except ValueError as exc:
        bot.send(chat_id, _ui('⚠️ {p0}', p0=error_text(exc)[:180]))
    return True
