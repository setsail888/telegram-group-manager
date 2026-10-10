"""Personal and group UI languages, with fresh admin checks on every group change."""
from .i18n import CURRENT_LANGUAGE, LOCALES, msg as _ui
from .command_menu import install_commands


class LanguageUI:
    def __init__(self, bot):
        self.bot = bot

    def page(self, uid, cid=None, mid=None):
        bot, owner = self.bot, cid if cid is not None else uid
        current = bot.store.ui_language(owner)
        keyboard = [[bot._button(("✅ " if code == current else "")+name,
                    f"lang:set:{owner}:{code}")] for code, name in LOCALES.items()]
        keyboard.append([bot._button(_ui("📖 功能说明"), f"lang:help:{owner}")])
        keyboard += [[bot._button(_ui("⬅️ 返回"), f"g:{cid}" if cid is not None else "groups")]]
        scope = _ui("本群系统提示") if cid is not None else _ui("我的私聊界面")
        return bot._edit_or_send(uid, mid, _ui("🌐 界面语言 / Language\n范围：{p0}\n当前：{p1}\n个人语言与群内语言分别保存。自定义欢迎语、群规、公告和关键词原文不会被翻译。",
            p0=scope, p1=LOCALES[current]), keyboard)

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
        except (ValueError, KeyError):
            return self.bot.answer(qid, _ui("语言设置失败，请检查权限或重新打开菜单。"), alert=True)
