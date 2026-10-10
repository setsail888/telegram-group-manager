"""Preview-confirmed flood policies using the existing private admin controls."""
from .flood_penalty import FLOOD_SETTINGS, PENALTIES, penalty_label

MODES = {"off": "关闭", "observe": "仅观察", "enforce": "自动清理"}


class FloodUI:
    def __init__(self, bot):
        self.bot = bot

    def snapshot(self, cid):
        settings = self.bot.store.settings(cid)
        return {key: settings[key] for key in FLOOD_SETTINGS}

    def cross(self, cid):
        s, b = self.bot.store.settings(cid), self.bot._button
        text = ("👥 跨账号重复消息\n"
                f"模式：{MODES[s['cross_flood_mode']]} · 主防刷屏{'开' if s['anti_flood'] else '关'}\n"
                f"规则：同一群/话题 {s['cross_flood_window']} 秒内，至少{s['cross_flood_users']}个账号，"
                f"累计{s['cross_flood_limit']}条相同内容。文字至少{s['cross_flood_min_chars']}个非空白字符。\n"
                "比较完整正文/说明、媒体标识、按钮与隐藏链接，统一大小写/全半角/零宽；不做谐音或语义相似合并。"
                "相同媒体也可检测，普通短回复默认不计。无参数成员命令（如/checkin）不计入本项，仍受其他防护。\n"
                "命中后清理窗口内缓存的同内容消息，之后继续发送也拦截，安静一个窗口后解除。"
                "仅当前触发者按防刷屏处罚，早先作者只撤回、不追罚。\n"
                "管理员/机器人豁免，清理前核验身份；每个内容最多缓存100条，仅保存摘要和ID，重启清空。"
                "无法读取群历史、无法撤销此前已经执行的签到/兑换。\n"
                "默认关闭，先观察；全局观察优先。处罚见「防刷屏 → 惩罚」。")
        kb = [[b(("✅ " if s["cross_flood_mode"] == mode else "")+label, f"ad:{cid}:crossmode:{mode}")
               for mode, label in MODES.items()]]
        kb += [[b("⚖️ 惩罚", f"v:{cid}:floodpenalty"), b("🔍 防护记录", f"v:{cid}:audit")],
               [b("⬅️ 返回防刷屏", f"v:{cid}:flood")]]
        return text, kb

    def penalties(self, cid):
        s, b = self.bot.store.settings(cid), self.bot._button
        text = (f"⚖️ 反刷屏惩罚\n\n当前：{penalty_label(s)}\n"
                f"禁言时长：{s['flood_mute_minutes']} 分钟\n"
                f"封禁时长：{str(s['flood_ban_minutes'])+' 分钟' if s['flood_ban_minutes'] else '永久'}\n"
                f"警告累计上限：{s['warn_limit']}次，与手动警告共用；达限成功禁言后清零。\n"
                "先成功撤回触发消息，再处罚当前发送者。跨账号回溯只清理同内容，不追罚早先作者。\n"
                "警告、禁言、踢出、封禁仅支持超级群，需删除消息和限制成员权限。"
                "踢出允许重进；封禁到期才可重进，0分钟为永久。"
                "Telegram在超级群封禁时会同时撤销该成员的其他历史消息。\n"
                "「仅撤回消息，不处罚」也不计全局累计禁言；其他明确处罚不叠加全局累计禁言。")
        if s["flood_penalty"] == "delete" and s["auto_mute_enabled"]:
            text += "\n⚠️ 全局累计违规禁言已开启；旧版「单次仅删除」仍沿用该累计规则，可选择「仅撤回消息，不处罚」退出。"
        options = (("warn", "⚠️ 警告"), ("mute", "🔇 禁言"), ("kick", "🚪 踢出"), ("ban", "⛔ 踢出＋封禁"), ("none", "仅撤回消息，不处罚"))
        kb = [[b(("✅ " if s["flood_penalty"] == mode else "")+label, f"ad:{cid}:fpenalty:{mode}")
               for mode, label in options[:3]],
              [b(("✅ " if s["flood_penalty"] == mode else "")+label, f"ad:{cid}:fpenalty:{mode}")
               for mode, label in options[3:]],
              [b("⏱️ 设置禁言时长", f"v:{cid}:floodmutetime"), b("⏱️ 设置封禁时长", f"v:{cid}:floodbantime")],
              [b("⬅️ 返回防刷屏", f"v:{cid}:flood")]]
        return text, kb

    def duration(self, cid, ban=False):
        s, b = self.bot.store.settings(cid), self.bot._button
        key, action, form = ("flood_ban_minutes", "fbantime", "flood_ban_time") if ban else ("flood_mute_minutes", "fmutetime", "flood_mute_time")
        text = f"⏱️ {'封禁' if ban else '禁言'}时长\n当前：{s[key]}分钟" + ("（0为永久）" if ban else "")
        text += "\n支持1–525600分钟（365天）。点击预设或输入整数，预览确认后生效；只影响今后的处罚。"
        values = [(10, "10分钟"), (60, "1小时"), (1440, "1天"), (10080, "7天"), (43200, "30天"), (525600, "365天")]
        if ban:
            values.append((0, "永久"))
        buttons = [b(label, f"ad:{cid}:{action}:{value}") for value, label in values]
        kb = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
        kb += [[b("✏️ 自定义分钟数", f"ad:{cid}:begin:{form}")]]+self.bot.admin_ui.back(cid, "floodpenalty")
        return text, kb

    def preview(self, uid, cid, changes, mid=None, draft=None):
        admin = self.bot.admin_ui
        draft = draft or admin._draft(uid, cid, "flood_config", "crossflood" if "cross_flood_mode" in changes else "floodpenalty",
                                      {"expected": self.snapshot(cid)})
        draft["op"] = "flood_config"
        draft["values"]["changes"] = changes
        summaries = []
        for key, value in changes.items():
            if key == "flood_penalty":
                summaries.append("防刷屏惩罚 → "+PENALTIES[value])
            elif key == "cross_flood_mode":
                summaries.append("跨账号重复 → "+MODES[value])
            else:
                summaries.append(("封禁时长" if key == "flood_ban_minutes" else "禁言时长")+f" → {value}分钟"+("（永久）" if value == 0 else ""))
        summaries.append("确认后持续保存；十分钟只限制本次确认。已存在的禁言/封禁期限不会自动更改。")
        if changes.get("flood_penalty") == "ban":
            summaries.append("⚠️ Telegram封禁会同时撤销该成员的其他历史消息。")
        return admin._preview(uid, draft, "\n".join(summaries), mid)

    def callback(self, uid, cid, action, arg, mid, qid):
        if action == "fpenalty":
            if arg not in PENALTIES:
                raise ValueError("未知处罚方式。")
            changes = {"flood_penalty": arg}
        elif action == "crossmode":
            if arg not in MODES:
                raise ValueError("未知跨账号检测模式。")
            changes = {"cross_flood_mode": arg}
        else:
            value = int(arg)
            if not (0 if action == "fbantime" else 1) <= value <= 525600:
                raise ValueError("时长范围为1–525600分钟；封禁可用0表示永久。")
            changes = {"flood_ban_minutes" if action == "fbantime" else "flood_mute_minutes": value}
        self.bot.answer(qid)
        return self.preview(uid, cid, changes, mid)

    def commit(self, draft):
        cid, values = draft["cid"], draft["values"]
        changes = values["changes"]
        if changes.get("cross_flood_mode") == "enforce" or "flood_penalty" in changes:
            if not self.bot.bot_can(cid, "can_delete_messages"):
                raise ValueError("机器人缺少删除消息权限。")
        if changes.get("flood_penalty") in ("warn", "mute", "kick", "ban"):
            if not self.bot.is_supergroup(cid) or not self.bot.bot_can(cid, "can_restrict_members"):
                raise ValueError("成员处罚需要超级群和限制成员权限。")
        self.bot.store.set_flood_settings(cid, changes, expected=values["expected"])
        self.bot.cross_guard.clear(cid)
        return "防刷屏设置已保存，持续生效。"
