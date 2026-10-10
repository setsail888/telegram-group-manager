"""Personal and group UI languages, with fresh admin checks on every group change."""
from .i18n import CURRENT_LANGUAGE, LOCALES, msg as _ui
from .command_menu import install_commands


class LanguageUI:
    def __init__(self, bot):
        self.bot = bot

    def page(self, uid, cid=None, mid=None, *, panel_cid=None):
        bot, owner = self.bot, cid if cid is not None else uid
        current = bot.store.ui_language(owner)
        buttons = [bot._button(("✅ " if code == current else "")+name,
                   f"lang:pick:{panel_cid}:{code}" if panel_cid is not None else
                   f"lang:set:{owner}:{code}") for code, name in LOCALES.items()]
        keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        if panel_cid is not None:
            keyboard.append([bot._button(_ui("💬 单独设置群内提示语言"), f"lang:group:{panel_cid}")])
        help_route = f"lang:info:{panel_cid}" if panel_cid is not None else f"lang:help:{owner}"
        keyboard.append([bot._button(_ui("📖 功能说明"), help_route)])
        back = f"g:{panel_cid}" if panel_cid is not None else f"lang:panel:{cid}" if cid is not None else "groups"
        keyboard.append([bot._button(_ui("⬅️ 返回"), back)])
        if cid is not None:
            text = _ui("💬 群内提示语言\n当前：{p0}\n只改变本群系统提示、验证与命令回复。控制面板仍使用你自己的界面语言。自定义内容保留原文。", p0=LOCALES[current])
        else:
            text = _ui("🌐 选择控制面板语言 / Select your language\n当前：{p0}\n选择后立即切换你的管理面板、功能说明和成员服务。其他管理员和群内提示的语言保持各自设置。", p0=LOCALES[current])
        return bot._edit_or_send(uid, mid, text, keyboard)

    def callback(self, query):
        uid, qid = query["from"]["id"], query["id"]
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or chat.get("id") != uid:
            return self.bot.answer(qid, _ui("请私聊机器人操作。"), alert=True)
        parts = query.get("data", "").split(":")
        try:
            if len(parts) == 2 and parts[1] == "self":
                self.bot.answer(qid)
                return self.page(uid, mid=message.get("message_id"))
            if (len(parts) == 3 and parts[1] in ("panel", "info")
                    or len(parts) == 4 and parts[1] == "pick"):
                cid = int(parts[2])
                if cid >= 0 or not self.bot.store.chat(cid) or not self.bot.is_admin(cid, uid):
                    raise ValueError(_ui("没有权限管理这个群组。"))
                if parts[1] == "pick" and parts[3] not in LOCALES:
                    raise ValueError(_ui("未知界面语言。"))
                self.bot.admin_ui.cancel(uid)
                self.bot._word_wizards.pop(uid, None)
                if parts[1] == "info":
                    self.bot.answer(qid)
                    return self.bot.feature_help.language(uid, uid, message.get("message_id"),
                                                          back=f"lang:panel:{cid}")
                if parts[1] == "pick":
                    locale = parts[3]
                    self.bot.store.set_ui_language(uid, locale)
                    CURRENT_LANGUAGE.set(locale)
                    self.bot.answer(qid, _ui("✅ 语言已保存。"))
                    result = self.bot._private_panel(uid, cid, message.get("message_id"), verified_admin=True)
                    install_commands(self.bot.api, locale, chat_id=uid)
                    return result
                self.bot.answer(qid)
                return self.page(uid, panel_cid=cid, mid=message.get("message_id"))
            if len(parts) == 3 and parts[1] in ("group", "help"):
                owner, locale = int(parts[2]), None
            elif len(parts) == 4 and parts[1] == "set":
                owner, locale = int(parts[2]), parts[3]
            else:
                raise ValueError(_ui("未知语言操作。"))
            if owner < 0:
                if not self.bot.store.chat(owner) or not self.bot.is_admin(owner, uid):
                    raise ValueError(_ui("没有权限管理这个群组。"))
            elif owner != uid:
                raise ValueError(_ui("只能修改自己的私聊语言。"))
            if parts[1] == "help":
                self.bot.admin_ui.cancel(uid)
                self.bot._word_wizards.pop(uid, None)
                self.bot.answer(qid)
                return self.bot.feature_help.language(uid, owner, message.get("message_id"))
            if locale is not None:
                self.bot.store.set_ui_language(owner, locale)
                if owner == uid:
                    CURRENT_LANGUAGE.set(locale)
                install_commands(self.bot.api, locale, chat_id=owner)
            self.bot.answer(qid, _ui("✅ 语言已保存。") if locale else "")
            return self.page(uid, cid=owner if owner < 0 else None, mid=message.get("message_id"))
        except (ValueError, KeyError, OverflowError):
            return self.bot.answer(qid, _ui("语言设置失败，请检查权限或重新打开菜单。"), alert=True)
