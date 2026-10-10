"""Private, permission-checked admin forms using Telegram inline keyboards.

Drafts are bounded, expire after ten minutes, and are consumed before commit.
Callbacks contain short opaque tokens; rule contents never enter callback_data.
"""
from .i18n import error_text, msg as _ui, join_text, render
import secrets
import time
from datetime import datetime

from .moderation import normalize_domain, evaluate, WORD_MODES
from .reply_rules import parse_keywords, parse_buttons, media_from_reply
from .scheduler import parse_rule, next_occurrence, format_run
from .word_library import parse_import, IMPORT_BYTES, describe_hit
from .membership import is_current_member
from .flood_penalty import PENALTIES


FORMS = {
    "flood_mute_time": ("floodpenalty", (("minutes", _ui('发送禁言分钟数，整数1–525600（365天）；预览确认后只影响今后的处罚。')),)),
    "flood_ban_time": ("floodpenalty", (("minutes", _ui('发送封禁分钟数，整数1–525600；0表示永久。预览确认后只影响今后的处罚。')),)),
    "word_import": ("words", (("content", _ui('发送UTF-8词库文件（单次最多1 MiB）或粘贴文字。每行一词；支持 匹配方式+Tab+词 或JSONL。总条数不限，可分批追加。已有词保持原规则。')),)),
    "pack_exclude": ("wordpacks", (("text", _ui('发送要从内置词库排除的完整词条（2–80字）；不会豁免同名自定义规则。')),)),
    "pack_restore": ("wordpacks", (("text", _ui('发送要恢复的完整内置词条（2–80字）。')),)),
    "word_search": ("words", (("query", _ui('发送搜索文字（1–80字），按文字查找词库。')),)),
    "domain_add": ("domains", (("domains", _ui('发送域名，每行一个（最多80个）。例如 example.com；其子域名也会放行。')),)),
    "reply_add": ("replies", (("keywords", _ui('发送触发关键词，多个词用逗号隔开。')),
                               ("body", _ui('发送回复正文，或发送带说明的图片/视频/动图/文件。')),
                               ("buttons", _ui('发送链接按钮：官网 - https://example.com\n多个按钮用 && 隔开；不需要则发送 -。')))),
    "welcome_text": ("welcome", (("text", _ui('发送欢迎语（最多1500字），{name} 会替换为新人名字。')),)),
    "rules_text": ("welcome", (("text", _ui('发送群规（最多1500字）。')),)),
    "log_chat_id": ("logs", (("target", _ui('发送独立日志群或频道的负数ID（在目标群发 /id 获取）；发送 0 关闭。')),)),
    "item_add": ("shop", (("title", _ui('发送奖励名称（2–60字）。')), ("price", _ui('发送单价（1–10000积分）。')),
                            ("stock", _ui('发送库存（1–1000）。')), ("description", _ui('发送说明（最多300字）。')))),
    "raffle_add": ("raffle", (("title", _ui('发送免费抽奖标题（2–100字）。活动不收费，不扣积分。')),)),
    "schedule_daily": ("schedules", (("rule", _ui('发送每日时间，例如 09:00。')),
                                      ("title", _ui('发送公告标题（最多60字）。')), ("body", _ui('发送公告正文（最多3500字）。')))),
    "schedule_weekly": ("schedules", (("rule", _ui('发送星期和时间，例如 一 09:00。')),
                                       ("title", _ui('发送公告标题（最多60字）。')), ("body", _ui('发送公告正文（最多3500字）。')))),
    "schedule_once": ("schedules", (("rule", _ui('发送未来日期时间，例如 2026-12-20 09:00。')),
                                     ("title", _ui('发送公告标题（最多60字）。')), ("body", _ui('发送公告正文（最多3500字）。')))),
    "spam_test": ("spam", (("text", _ui('发送待测试文字（最多1000字）。仅测试文字规则，不计入刷屏，不执行处罚。')),)),
    "point_adjust": ("points", (("target", _ui('发送成员数字ID。')), ("amount", _ui('发送要调整的积分：正数增加，负数扣除，范围 -10000 到 10000。')))),
}
for _action in ("warn", "unwarn", "mute", "unmute", "ban", "kick", "unban"):
    FORMS["member_" + _action] = ("manage", (("target", _ui('发送目标成员数字ID。管理员可在群里回复对方 /id；成员可私聊机器人 /id。')),)
                               + ((("minutes", _ui('发送禁言分钟数（1–1440）。')),) if _action == "mute" else ()))


class AdminUI:
    def __init__(self, bot):
        self.bot = bot
        self.drafts = {}
        self.word_choices = {}
        self.searches = {}
        self.exports = {}

    def cancel(self, uid):
        self.drafts.pop(uid, None)

    def _token(self, mapping, uid, cid, **values):
        now = time.monotonic()
        for key in list(mapping):
            if mapping[key]["expires"] <= now:
                del mapping[key]
        while len(mapping) >= 2000:
            del mapping[next(iter(mapping))]
        token = secrets.token_hex(6)
        mapping[token] = {"uid": uid, "cid": cid, "expires": now+600, **values}
        return token

    def _choice(self, mapping, uid, cid, token):
        choice = mapping.get(token)
        if (not choice or choice["uid"] != uid or choice["cid"] != cid
                or choice["expires"] <= time.monotonic()):
            raise ValueError(_ui('列表已过期，请重新打开。'))
        return choice

    def word_buttons(self, uid, cid, rows):
        modes = self.bot.store.word_modes(cid)
        return [[self.bot._button(_ui('✏️ {p0} · {p1}', p0=row['value'][:35], p1=WORD_MODES[modes.get(row['key'], 'contains')]),
                    _ui('ad:{p0}:word:{p1}', p0=cid, p1=self._token(self.word_choices, uid, cid, key=row['key'])))]
                for row in rows]

    def word_choice(self, uid, cid, token):
        choice = self._choice(self.word_choices, uid, cid, token)
        rule = self.bot.store.word_rule(cid, choice["key"])
        if not rule:
            raise ValueError(_ui('关键词已删除，请刷新列表。'))
        return rule

    def word_detail(self, uid, cid, token, mid):
        rule, b = self.word_choice(uid, cid, token), self.bot._button
        text = (_ui('📝 关键词：{p0}\n当前匹配：{p1}\n\n包含：文字中出现即命中，遵循拆字匹配开关。\n整词：两侧不能紧邻字母、数字、下划线或汉字，适合英文短词；不进行中文分词。\n整句：整条文字与关键词相同才命中。\n均忽略大小写、全半角和零宽字符；整词、整句不使用拆字匹配。\n图片识别仍需满足OCR的置信度与至少4字限制。', p0=rule['value'], p1=WORD_MODES[rule['mode']]))
        kb = [[b(label, _ui('ad:{p0}:wm_{p1}:{p2}', p0=cid, p1=mode, p2=token)) for mode, label in WORD_MODES.items()],
              [b(_ui('🗑 删除该关键词'), _ui('ad:{p0}:word_delete:{p1}', p0=cid, p1=token))],
              [b(_ui('⬅️ 返回词库列表'), _ui('wl:{p0}:0', p0=cid))]]
        return self.bot._edit_or_send(uid, mid, text, kb)

    def search_page(self, uid, cid, token, page, mid=None):
        choice = self._choice(self.searches, uid, cid, token)
        rows, count, page, pages = self.bot.store.search_words(cid, choice["query"], page)
        kb = self.word_buttons(uid, cid, rows)
        nav = []
        for label, index in ((_ui('◀️ 上一页'), page-1), (_ui('下一页 ▶️'), page+1)):
            if 0 <= index < pages:
                nav.append(self.bot._button(label, _ui('ad:{p0}:searchpage:{p1}_{p2}', p0=cid, p1=token, p2=index)))
        if nav:
            kb.append(nav)
        kb.append([self.bot._button(_ui('🔎 重新搜索'), _ui('ad:{p0}:begin:word_search', p0=cid))])
        kb += self.back(cid, "words")
        text = (_ui('🔎 搜索：{p0}\n共 {p1} 条 · 第 {p2}/{p3} 页\n点击条目设置匹配方式或删除。', p0=choice['query'], p1=count, p2=page + 1, p3=pages) + (_ui('\n没有符合的关键词。') if not rows else ""))
        return self.bot._edit_or_send(uid, mid, text, kb)

    def _draft(self, uid, cid, op, page, values=None):
        now = time.monotonic()
        self.drafts = {u: d for u, d in self.drafts.items() if d["expires"] > now}
        if len(self.drafts) >= 2000 and uid not in self.drafts:
            self.drafts.pop(next(iter(self.drafts)))
        draft = {"cid": cid, "op": op, "page": page, "step": 0,
                 "values": values or {}, "expires": now+600,
                 "nonce": secrets.token_hex(6), "confirm": False}
        self.drafts[uid] = draft
        return draft

    def back(self, cid, page):
        return [[self.bot._button(_ui('❌ 取消 / 返回'), _ui('v:{p0}:{p1}', p0=cid, p1=page))]]

    def _prompt(self, uid, draft):
        fields = FORMS[draft["op"]][1]
        return self.bot.send(uid, _ui('📝 第 {p0}/{p1} 步\n', p0=draft['step'] + 1, p1=len(fields))
                             + fields[draft["step"]][1] + _ui('\n十分钟内有效；可发送 /cancel。'),
                             reply_markup={"inline_keyboard": self.back(draft["cid"], draft["page"])})

    def _preview(self, uid, draft, summary, message_id=None):
        cid = draft["cid"]
        title = (self.bot.store.chat(cid) or {}).get("title", str(cid))
        keyboard = [[self.bot._button(_ui('✅ 确认执行'), _ui('ac:{p0}:{p1}', p0=cid, p1=draft['nonce']))]] + self.back(cid, draft["page"])
        full = _ui('🔎 操作预览\n群：{p0}（{p1}）\n{p2}\n\n确认后生效；十分钟内有效。', p0=title, p1=cid, p2=summary)
        full = render(full, self.bot.store.ui_language(uid))
        # Show the complete draft, including the end of a long announcement.
        # Only the final chunk owns the confirmation button and its message id.
        while len(full) > 3500:
            if not self.bot._edit_or_send(uid, message_id, full[:3500]):
                self.cancel(uid)
                raise ValueError(_ui('预览发送失败，未执行；请重新打开操作。'))
            full, message_id = full[3500:], None
        result = self.bot._edit_or_send(uid, message_id, full, keyboard)
        if not result:
            self.cancel(uid)
            raise ValueError(_ui('预览发送失败，未执行；请重新打开操作。'))
        draft["confirm"] = True
        draft["prompt_id"] = result.get("message_id") if isinstance(result, dict) else None
        return result

    def input(self, message):
        uid = (message.get("from") or {}).get("id")
        draft = self.drafts.get(uid)
        if not draft:
            return False
        cid = draft["cid"]
        if message.get("sender_chat"):
            return True
        text = (message.get("text") or message.get("caption") or "").strip()
        if text == "/cancel":
            self.cancel(uid)
            self.bot.send(uid, _ui('✅ 已取消。'), reply_markup={"inline_keyboard": self.back(cid, draft["page"])})
            return True
        if draft["expires"] <= time.monotonic() or not self.bot.is_admin(cid, uid):
            self.cancel(uid)
            self.bot.send(uid, _ui('编辑已过期或管理权限已失效，请重新打开面板。'))
            return True
        if draft["confirm"]:
            self.bot.send(uid, _ui('请点击上方确认按钮，或发送 /cancel。'))
            return True
        op, values = draft["op"], draft["values"]
        key = FORMS[op][1][draft["step"]][0]
        try:
            value = self._validate(op, key, text, message, values)
            values[key] = value
            draft["step"] += 1
            if draft["step"] < len(FORMS[op][1]):
                self._prompt(uid, draft)
            elif op == "spam_test":
                conf = self.bot.store.settings(cid)
                policy = self.bot.store.word_policy(cid)
                reason = evaluate({"text": text}, conf,
                    policy.custom,
                    [r["key"] for r in self.bot.store.filters(cid, "domain")],
                    exceptions=policy.exceptions)
                if not reason and conf["word_filter_enabled"]:
                    hit = policy.hit({"text": text}, loose=conf["word_loose_match"], settings=conf)
                    reason = describe_hit(hit, conf) if hit else None
                elif reason == _ui('违禁词'):
                    reason = describe_hit(policy.hit({"text": text}, loose=conf["word_loose_match"], settings=conf), conf)
                self.cancel(uid)
                self.bot.send(uid, _ui('🧪 文字规则测试：') + (reason or _ui('未命中')) + _ui('\n不包含图片、隐藏实体、频率或成员身份检查。'),
                              reply_markup={"inline_keyboard": self.back(cid, "spam")})
            elif op == "word_search":
                token = self._token(self.searches, uid, cid, query=value)
                self.cancel(uid)
                self.search_page(uid, cid, token, 0)
            elif op == "word_import":
                values["revision"] = self.bot.store.word_revision(cid)
                self.bot.word_ui.preview_import(uid, draft)
            elif op in ("flood_mute_time", "flood_ban_time"):
                setting = "flood_ban_minutes" if op == "flood_ban_time" else "flood_mute_minutes"
                self.bot.flood_ui.preview(uid, cid, {setting: value}, draft=draft)
            elif op in ("pack_exclude", "pack_restore"):
                values["revision"] = self.bot.store.word_revision(cid)
                self._preview(uid, draft, (_ui('排除内置词：') if op == "pack_exclude" else _ui('恢复内置词：')) + value)
            else:
                if op == "reply_add":
                    values["previous"] = next((r for r in self.bot.store.replies(cid)
                                               if r["key"] == values["keywords"][0]), None)
                if op in ("welcome_text", "rules_text", "log_chat_id"):
                    values["previous"] = self.bot.store.settings(cid)[op]
                labels = {"domains":_ui('域名'), "keywords":_ui('关键词'), "body":_ui('正文'), "buttons":_ui('链接按钮'),
                          "text":_ui('内容'), "target":_ui('目标ID'), "title":_ui('名称'), "price":_ui('单价'), "stock":_ui('库存'),
                          "description":_ui('说明'), "rule":_ui('时间规则'), "amount":_ui('积分调整'), "minutes":_ui('分钟数'), "media_type":_ui('媒体类型')}
                lines = []
                for k, v in values.items():
                    if k in ("previous", "media_id"):
                        continue
                    if k=="keywords":
                        v = v[1]
                    elif k=="buttons":
                        v = join_text('；', (_ui('{p0} → {p1}', p0=b['text'], p1=b['url']) for b in v)) or _ui('无')
                    elif k=="domains":
                        v = join_text('\n', v)
                    lines.append(_ui('{p0}：{p1}', p0=labels.get(k, k), p1=v))
                if op == "reply_add" and values["previous"]:
                    lines.insert(0, _ui('⚠️ 同关键词回复已存在，确认后替换。'))
                if op.startswith("schedule_"):
                    mode = op.split("_", 1)[1]
                    run = next_occurrence(mode, values["rule"], self.bot.store.tz)
                    lines.insert(0, _ui('时区：{p0}；下次：{p1}', p0=self.bot.store.tz, p1=format_run(run, self.bot.store.tz)))
                self._preview(uid, draft, join_text('\n', lines))
        except ValueError as exc:
            # Retain the current field for correction; no settings have been written.
            draft["step"] = min(draft["step"], len(FORMS[op][1])-1)
            self.bot.send(uid, _ui('⚠️ {p0}\n请重新发送这一步内容，或 /cancel。', p0=error_text(exc)[:250]))
        return True

    def _validate(self, op, key, text, message, values):
        if op in ("flood_mute_time", "flood_ban_time"):
            try:
                value = int(text)
            except ValueError:
                raise ValueError(_ui('请发送整数分钟数。')) from None
            if (0 if op == "flood_ban_time" else 1) <= value <= 525600:
                return value
            raise ValueError(_ui('时长范围为1–525600分钟；只有封禁支持0表示永久。'))
        if op == "word_import":
            if sum(d["op"] == "word_import" and "content" in d["values"] for d in self.drafts.values()) >= 4:
                raise ValueError(_ui('并发词库导入预览已满，请稍后重试。'))
            document = message.get("document")
            if document:
                if not document.get("file_id") or int(document.get("file_size") or 0) > IMPORT_BYTES:
                    raise ValueError(_ui('文件缺少ID或超过1 MiB，可分批追加。'))
                from .api import TelegramAPIError
                try:
                    text = self.bot.api.fetch_file(document["file_id"], max_bytes=IMPORT_BYTES)
                except TelegramAPIError:
                    raise ValueError(_ui('文件下载失败，请检查网络后重试。')) from None
            return parse_import(text)
        if op in ("pack_exclude", "pack_restore"):
            if not 2 <= len(text) <= 80:
                raise ValueError(_ui('词条需为2–80字。'))
            return text
        if key == "domains":
            fragments = [s.strip() for s in text.replace("，", "\n").replace(",", "\n").splitlines() if s.strip()]
            domains = [normalize_domain(s) for s in fragments]
            if not 1 <= len(domains) <= 80 or not all(domains):
                raise ValueError(_ui('请提供1–80个有效域名，整批尚未保存。'))
            return sorted(set(domains))
        if key == "keywords":
            return parse_keywords(text)
        if key == "buttons":
            return parse_buttons("" if text == "-" else text)
        if key in ("target", "price", "stock", "amount", "minutes"):
            try:
                value = int(text)
            except ValueError:
                raise ValueError(_ui('请发送整数。')) from None
            if key == "target":
                if op == "log_chat_id" and -2**63 < value <= 0 or op != "log_chat_id" and 0 < value < 2**63:
                    return value
            else:
                lo, hi = {"price": (1,10000), "stock": (1,1000), "amount": (-10000,10000), "minutes": (1,1440)}[key]
                if lo <= value <= hi and value != 0:
                    return value
            raise ValueError(_ui('数值超出允许范围。'))
        if key == "rule":
            mode, rule = parse_rule(op.split("_", 1)[1], text)
            next_occurrence(mode, rule, self.bot.store.tz)
            return rule
        if op == "reply_add" and key == "body":
            kind, file_id = media_from_reply({"reply_to_message": message})
            if kind:
                if not file_id or len(text) > 1000:
                    raise ValueError(_ui('媒体需包含有效文件ID，说明最多1000字。'))
                values["media_type"], values["media_id"] = kind, file_id
                return text
        lo, hi = (1, 1500)
        if op == "word_search":
            lo, hi = 1, 80
        elif op == "reply_add":
            lo, hi = 1, 1500
        elif key == "title":
            lo, hi = (2, 100) if op == "raffle_add" else (1, 60) if op.startswith("schedule_") else (2, 60)
        elif op.startswith("schedule_") and key == "body":
            lo, hi = 1, 3500
        elif key == "description":
            lo, hi = 1, 300
        elif op == "spam_test":
            lo, hi = 1, 1000
        if not lo <= len(text) <= hi:
            raise ValueError(_ui('内容长度应为 {p0}–{p1} 字。', p0=lo, p1=hi))
        return text

    def decorate(self, cid, page, text, keyboard):
        b = self.bot._button
        add = lambda label, op: b(label, _ui('ad:{p0}:begin:{p1}', p0=cid, p1=op))
        if page in ("domains", "replies", "shop", "raffle", "schedules", "audit"):
            text, extra = self.list_page(cid, page, 0)
            keyboard = self.bot._settings_controls(cid, page, self.bot.store.settings(cid)) + extra
        if page == "spam":
            conf = self.bot.store.settings(cid)
            text += (_ui('\n\n🔍 内容过滤执行：') + (_ui('仅观察') if conf["observe_only"] else _ui('正常处罚'))
                     + _ui('\n观察包括违禁词、链接、转发、刷屏、图片和频道过滤；不改变新人验证及手动管理操作。'))
            keyboard.append([add(_ui('🧪 测试文字规则'), "spam_test"), b(_ui('🔍 查看防护记录'), _ui('v:{p0}:audit', p0=cid))])
        elif page == "flood":
            text += _ui('\n「媒体连发」中可设置相册按一条计数；每张说明及内容重复检测仍生效。')
        elif page == "welcome":
            keyboard.append([add(_ui('✏️ 编辑欢迎语'), "welcome_text"), add(_ui('📜 编辑群规'), "rules_text")])
            text += _ui('\n\n欢迎语预览：{p0}\n群规预览：{p1}',
                        p0=self.bot.store.content_text(cid, 'welcome_text')[:500],
                        p1=self.bot.store.content_text(cid, 'rules_text')[:500])
        elif page == "logs":
            keyboard.append([add(_ui('✏️ 设置/关闭日志群'), "log_chat_id"), b(_ui('🔍 防护记录'), _ui('v:{p0}:audit', p0=cid))])
        elif page == "ocr":
            keyboard.extend([[b(_ui('关闭'), _ui('ad:{p0}:ocr:off', p0=cid)), b(_ui('仅审核'), _ui('ad:{p0}:ocr:review', p0=cid))],
                             [b(_ui('⚠️ 自动删除'), _ui('ad:{p0}:ocr:delete', p0=cid))]])
            text += _ui('\n按钮可直接选择模式；仅审核会写入防护记录。精确例外同样用于 OCR。')
        elif page == "points":
            keyboard.append([add(_ui('➕ / ➖ 调整成员积分'), "point_adjust")])
        elif page == "verify":
            conf = self.bot.store.settings(cid)
            text += _ui('\n最多答错 {p0} 次；用完后保持限制，超时移出后可重新加入。', p0=conf['verify_attempt_limit'])
        elif page == "words":
            text += _ui('\n词库列表支持逐词选择包含、整词或整句匹配。')
            keyboard.append([add(_ui('🔎 搜索词库'), "word_search")])
            keyboard.append([b(_ui('📚 分类词库'), _ui('v:{p0}:wordpacks', p0=cid)), add(_ui('📥 文件导入'), "word_import")])
            keyboard.append([b(_ui('📤 导出自定义词库'), _ui('ad:{p0}:wordexport:0', p0=cid))])
            keyboard.append([b(_ui('🈶 中文变体识别'), _ui('v:{p0}:wordvariants', p0=cid))])
        elif page == "wordpacks":
            text, keyboard = self.bot.word_ui.home(cid)
        elif page == "wordvariants":
            text, keyboard = self.bot.word_ui.variants(cid)
        elif page == "raid":
            conf = self.bot.store.settings(cid)
            until = self.bot.store.raid_until(cid)
            state = (_ui('保护中，至 ') + datetime.fromtimestamp(until, self.bot.store.tz).strftime("%m-%d %H:%M:%S")
                     if until else _ui('当前未处于保护期'))
            text = (_ui('🚨 集中入群保护\n{p0}\n自动检测：{p1}\n{p2} 秒内 {p3} 个不同普通成员入群，触发 {p4} 分钟保护。\n从触发者起，新人需完成验证；不追溯之前的成员。管理员和机器人不计入人数。\n到期自动停止对后续新人的额外验证；已经收到验证题的成员仍需答题。\n保护期限重启后保留，入群计数重启后重新累计。\n关闭自动检测不会提前停止已有保护，请使用下方停止按钮。', p0=state, p1=_ui('开') if conf['raid_enabled'] else _ui('关'), p2=conf['raid_window'], p3=conf['raid_join_limit'], p4=conf['raid_duration']))
            keyboard.append([b(_ui('🛡️ 立即开始保护'), _ui('ad:{p0}:raid:start', p0=cid)),
                             b(_ui('⏹ 停止额外保护'), _ui('ad:{p0}:raid:stop', p0=cid))])
        elif page == "permissions":
            member = self.bot.api.safe("getChatMember", chat_id=cid, user_id=self.bot.bot_id)
            chat = self.bot.api.safe("getChat", chat_id=cid)
            member = member if isinstance(member, dict) else {}
            chat = chat if isinstance(chat, dict) else {}
            is_owner = member.get("status") == "creator"
            right = lambda key: "✅" if is_owner or member.get(key) is True else _ui('❌ / 未能确认')
            text = (_ui('🔐 机器人权限检查\n群类型：{p0}\n管理身份：{p1}\n删除消息：{p2}\n限制成员：{p3}\nOCR引擎：{p4}\n\n内容清理需要删除消息权限；禁言、新人验证和集中入群保护需要限制成员权限及超级群组。\n如缺少权限，请在 Telegram 群设置中授予后刷新。', p0=chat.get('type', _ui('未能获取')), p1=member.get('status', _ui('未能获取')), p2=right('can_delete_messages'), p3=right('can_restrict_members'), p4=_ui('可用') if self.bot.ocr.available else _ui('未启用')))
            keyboard.append([b(_ui('🔄 重新检查'), _ui('v:{p0}:permissions', p0=cid))])
        elif page == "manage":
            text = _ui('👮 成员管理\n选择操作 → 输入数字ID → 预览确认。\n每次重新核验管理权限和目标身份；管理员与本机器人受保护。\n警告达到上限后临时禁言60分钟。')
            ops = [(_ui('⚠️ 警告'), "warn"), (_ui('清除警告'), "unwarn"), (_ui('🔇 禁言'), "mute"),
                   (_ui('🔊 解禁言'), "unmute"), (_ui('⛔ 封禁'), "ban"), (_ui('移出可重进'), "kick"), (_ui('解除封禁'), "unban")]
            keyboard.extend([[add(label, "member_"+op) for label, op in ops[i:i+2]] for i in range(0,len(ops),2)])
        elif page == "overview":
            text += _ui('\n内容过滤：') + (_ui('仅观察') if self.bot.store.settings(cid)["observe_only"] else _ui('正常处罚'))
        return text, keyboard

    def list_page(self, cid, page, index):
        bot, store, b = self.bot, self.bot.store, self.bot._button
        kb, rows = [], []
        if page == "domains":
            rows = store.filters(cid, "domain")
            title = _ui('🌐 域名白名单（包含子域名）')
            kb.append([b(_ui('➕ 批量添加域名'), _ui('ad:{p0}:begin:domain_add', p0=cid))])
        elif page == "replies":
            rows = store.replies(cid)
            title = _ui('💬 自动回复（点击条目查看或删除）')
            kb.append([b(_ui('➕ 添加/替换回复'), _ui('ad:{p0}:begin:reply_add', p0=cid))])
        elif page == "shop":
            rows = bot.economy.items(cid)
            title = _ui('🎁 虚拟积分奖励（点击下架）')
            kb.append([b(_ui('➕ 上架奖励'), _ui('ad:{p0}:begin:item_add', p0=cid)), b(_ui('🧾 待核销订单'), _ui('ad:{p0}:orders:0', p0=cid))])
        elif page == "orders":
            rows = bot.economy.orders(cid, status="pending", limit=40)
            title = _ui('🧾 最近40笔待核销订单（处理后刷新）')
        elif page == "schedules":
            rows = store.schedules(cid)
            title = _ui('📅 定时公告 · 时区 {p0}', p0=store.tz)
            kb.extend([[b(_ui('➕ 每日'), _ui('ad:{p0}:begin:schedule_daily', p0=cid)), b(_ui('➕ 每周'), _ui('ad:{p0}:begin:schedule_weekly', p0=cid))],
                       [b(_ui('➕ 单次公告'), _ui('ad:{p0}:begin:schedule_once', p0=cid))]])
        elif page == "raffle":
            active = bot.raffles.active(cid)
            rows = bot.raffles.history(cid, limit=8)
            title = _ui('🎉 免费抽奖 · 不扣积分\n') + (_ui('开放中 #{p0} {p1}', p0=active['id'], p1=active['title']) if active else _ui('当前没有开放活动'))
            if active:
                kb.append([b(_ui('🎲 开奖'), _ui('ad:{p0}:draw:{p1}', p0=cid, p1=active['id'])), b(_ui('取消活动'), _ui('ad:{p0}:raffle_cancel:{p1}', p0=cid, p1=active['id']))])
            else:
                kb.append([b(_ui('➕ 创建免费抽奖'), _ui('ad:{p0}:begin:raffle_add', p0=cid))])
        elif page == "audit":
            rows, index, pages = store.audit_page(cid, index)
            title = _ui('🔍 防护记录 · 保留30天/每群最多1000条\n仅保存消息ID、目标ID、原因和结果，不保存原文/图片。\n标记已复核不会恢复已删除消息，也不自动放宽规则。')
        else:
            raise ValueError(_ui('未知列表'))
        if page != "audit":
            pages = max(1,(len(rows)+7)//8)
            index = max(0,min(int(index),pages-1))
            rows = rows[index*8:index*8+8]
        lines = []
        outcomes = {"observed": _ui('观察'), "deleted": _ui('已删'), "cleared": _ui('批量清理已提交'), "failed": _ui('删除失败')}
        for n, row in enumerate(rows):
            if page == "audit":
                stamp = datetime.fromtimestamp(row['created_at'],store.tz).strftime("%m-%d %H:%M")
                lines.append(_ui('#{p0} {p1} · 消息{p2} · {p3} · {p4} · {p5}', p0=row['id'], p1=stamp, p2=row['message_id'], p3=row['target_id'], p4=_ui(row['reason']), p5=outcomes[row['outcome']]) + (_ui(' · 已复核') if row['reviewed_by'] else ""))
                if row.get("rule_key"):
                    lines.append(_ui('  规则：{p0}', p0=row['rule_key']) + (_ui(' · 分类：{p0}', p0=row['pack_id']) if row.get("pack_id") else _ui(' · 自定义')))
                if row.get("match_kind"):
                    from .variants import kind_text
                    lines.append(_ui('  识别：')+kind_text(row["match_kind"]))
                if row.get("penalty"):
                    from .flood_penalty import result_text
                    lines.append(_ui('  处罚：')+PENALTIES.get(row["penalty"], row["penalty"])+" · "+result_text(row["penalty_result"]))
                buttons = [b(_ui('✓ 复核 #{p0}', p0=row['id']), _ui('ad:{p0}:review:{p1}', p0=cid, p1=row['id']))]
                if str(cid).startswith("-100"):
                    buttons.append({"text": _ui('查看原消息'), "url": _ui('https://t.me/c/{p0}/{p1}', p0=str(cid)[4:], p1=row['message_id'])})
                kb.append(buttons)
            elif page in ("domains", "replies"):
                label = row["key"] if page == "domains" else row["keyword"]
                lines.append(label)
                kb.append([b(("🗑 " if page == "domains" else "💬 ")+label[:35], _ui('ad:{p0}:{p1}:{p2}', p0=cid, p1='domain' if page == 'domains' else 'reply', p2=index * 8 + n))])
            elif page == "shop":
                label = _ui('#{p0} {p1} · {p2}分 · 库存{p3}', p0=row['id'], p1=row['title'], p2=row['price'], p3=row['stock'])
                lines.append(label)
                kb.append([b(label[:55], _ui('ad:{p0}:item:{p1}', p0=cid, p1=row['id']))])
            elif page == "orders":
                lines.append(_ui('#{p0} {p1} · 成员{p2} · {p3}分', p0=row['id'], p1=row['item_title'], p2=row['member_id'], p3=row['cost']))
                kb.append([b(_ui('✅ 核销 #{p0}', p0=row['id']), _ui('ad:{p0}:fulfill:{p1}', p0=cid, p1=row['id'])), b(_ui('↩️ 退款'), _ui('ad:{p0}:refund:{p1}', p0=cid, p1=row['id']))])
            elif page == "schedules":
                label = _ui('#{p0} {p1} · {p2}', p0=row['id'], p1=row['title'], p2=_ui('运行') if row['enabled'] else _ui('暂停/结束'))
                lines.append(label)
                kb.append([b(label[:55], _ui('s:{p0}:{p1}', p0=cid, p1=row['id']))])
            elif page == "raffle":
                lines.append(_ui('#{p0} {p1} · {p2} · {p3}人', p0=row['id'], p1=row['title'], p2=row['status'], p3=row['count']) + (_ui(' · 获胜ID {p0}', p0=row['winner_id']) if row['winner_id'] else ""))
        nav = []
        if index:
            nav.append(b(_ui('⬅️ 上页'), _ui('ad:{p0}:{p1}:{p2}', p0=cid, p1=page, p2=index - 1)))
        if index+1 < pages:
            nav.append(b(_ui('下页 ➡️'), _ui('ad:{p0}:{p1}:{p2}', p0=cid, p1=page, p2=index + 1)))
        if nav:
            kb.append(nav)
        kb.append([b(_ui('🔄 刷新'), _ui('ad:{p0}:{p1}:{p2}', p0=cid, p1=page, p2=index))])
        return title+_ui('\n第 {p0}/{p1} 页\n', p0=index + 1, p1=pages)+(join_text('\n', lines) or _ui('暂无记录')), kb

    def callback(self, query, cid, parts):
        uid, qid = query["from"]["id"], query["id"]
        mid = query["message"].get("message_id")
        bot = self.bot
        try:
            if parts[0] == "ac":
                draft = self.drafts.get(uid)
                if (len(parts) != 3 or not draft or not draft["confirm"] or draft["cid"] != cid
                        or draft["nonce"] != parts[2] or draft["expires"] <= time.monotonic()
                        or draft.get("prompt_id") != mid):
                    raise ValueError(_ui('确认已失效，请重新打开操作。'))
                self.cancel(uid)  # One attempt; never retry an uncertain Telegram side effect.
                result = self.commit(uid, draft)
                bot.answer(qid, _ui('✅ 操作完成'))
                bot._edit_or_send(uid, mid, "✅ "+str(result), self.back(cid, draft["page"]))
                bot.log(cid, _ui('按钮操作 {p0}', p0=draft['op']), actor=uid)
                return
            self.cancel(uid)
            action = parts[2] if len(parts) >= 3 else ""
            arg = parts[3] if len(parts) == 4 else ""
            if len(parts) != 4:
                raise ValueError(_ui('按钮参数无效。'))
            if action == "begin":
                if arg not in FORMS:
                    raise ValueError(_ui('未知操作。'))
                draft = self._draft(uid, cid, arg, FORMS[arg][0])
                if arg in ("flood_mute_time", "flood_ban_time"):
                    draft["values"]["expected"] = bot.flood_ui.snapshot(cid)
                bot.answer(qid)
                return self._prompt(uid, draft)
            if action in ("pack", "packmode", "packpreset", "packpage", "pexclude", "wordexport", "variantmode", "variantpreset"):
                return bot.word_ui.callback(uid, cid, action, arg, mid, qid)
            if action in ("fpenalty", "crossmode", "fmutetime", "fbantime"):
                return bot.flood_ui.callback(uid, cid, action, arg, mid, qid)
            if action == "word":
                self.word_choice(uid, cid, arg)
                bot.answer(qid)
                return self.word_detail(uid, cid, arg, mid)
            if action == "searchpage":
                token, _, page_index = arg.partition("_")
                self._choice(self.searches, uid, cid, token)
                bot.answer(qid)
                return self.search_page(uid, cid, token, int(page_index), mid)
            if action in ("domains", "replies", "shop", "orders", "schedules", "raffle", "audit"):
                text, kb = self.list_page(cid, action, max(0,int(arg)))
                # Controls remain visible while paginating auto-replies.
                if action == "replies":
                    kb = bot._settings_controls(cid, "replies", bot.store.settings(cid)) + kb
                kb += self.back(cid, "shop" if action == "orders" else action) if action == "orders" else [[bot._button(_ui('⬅️ 返回设置'), _ui('g:{p0}', p0=cid))]]
                bot.answer(qid)
                return bot._edit_or_send(uid, mid, text, kb)
            if action == "review":
                if not bot.store.review_audit(cid, int(arg), uid):
                    raise ValueError(_ui('记录已过期。'))
                bot.answer(qid, _ui('已标记复核；规则保持当前设置'))
                text, kb = self.list_page(cid, "audit", 0)
                return bot._edit_or_send(uid, mid, text, kb+[[bot._button(_ui('⬅️ 返回设置'), _ui('g:{p0}', p0=cid))]])
            page, values, summary = "spam", {}, ""
            if action.startswith("wm_") or action == "word_delete":
                rule = self.word_choice(uid, cid, arg)
                mode = action[3:] if action.startswith("wm_") else None
                if mode is not None and mode not in WORD_MODES:
                    raise ValueError(_ui('未知关键词匹配方式。'))
                page, values = "words", {"expected": rule, "mode": mode}
                summary = _ui('关键词：{p0}\n', p0=rule['value']) + (_ui('删除该关键词。') if mode is None else
                    _ui('匹配方式：{p0} → {p1}', p0=WORD_MODES[rule['mode']], p1=WORD_MODES[mode]))
                action = "word_delete" if mode is None else "word_mode"
            elif action == "raid":
                if arg not in ("start", "stop"):
                    raise ValueError(_ui('未知入群防护操作。'))
                duration = bot.store.settings(cid)["raid_duration"]
                page, values = "raid", {"previous": bot.store.raid_until(cid), "action": arg, "duration": duration}
                summary = (_ui('立即开始 {p0} 分钟额外新人验证。', p0=duration) if arg == "start" else
                           _ui('停止额外新人验证；已有验证题仍有效，常规新人验证开关不受影响。'))
            elif action == "ocr":
                if arg not in ("off", "review", "delete"):
                    raise ValueError(_ui('无效OCR模式。'))
                page, values = "ocr", {"mode": arg}
                summary = _ui('OCR模式：{p0}。自动删除可能误判，建议先使用仅审核。', p0=arg)
            elif action in ("domain", "reply"):
                page = "domains" if action == "domain" else "replies"
                rows = bot.store.filters(cid, "domain") if action == "domain" else bot.store.replies(cid)
                index = int(arg)
                if not 0 <= index < len(rows):
                    raise ValueError(_ui('列表已变化，请刷新。'))
                values = dict(rows[index])
                summary = _ui('确认删除：')+values["key"]
                if action == "reply":
                    summary += _ui('\n回复正文：')+values["reply"]+_ui('\n媒体：')+str(values["media_type"])+_ui('\n按钮：')+values["buttons"]
            elif action == "item":
                page, values = "shop", bot.economy.item(cid, int(arg))
                if not values or not values["active"]:
                    raise ValueError(_ui('奖励已下架。'))
                summary = _ui('下架奖励 #{p0} {p1}；已有订单保留。', p0=values['id'], p1=values['title'])
            elif action in ("fulfill", "refund"):
                page = "shop"
                values = next((r for r in bot.economy.orders(cid,status="pending",limit=40) if r["id"]==int(arg)), None)
                if not values:
                    raise ValueError(_ui('订单已处理或列表已变化。'))
                summary = _ui('{p0} 订单 #{p1} · {p2} · {p3}分 · 成员{p4}', p0=_ui('核销') if action == 'fulfill' else _ui('退款并恢复库存'), p1=values['id'], p2=values['item_title'], p3=values['cost'], p4=values['member_id'])
            elif action in ("draw", "raffle_cancel"):
                page, values = "raffle", bot.raffles.active(cid)
                if not values or values["id"] != int(arg):
                    raise ValueError(_ui('活动已变化，请刷新。'))
                summary = _ui('{p0} #{p1} {p2}', p0=_ui('开奖（结果固定，不能重抽）') if action == 'draw' else _ui('取消活动'), p1=values['id'], p2=values['title'])
            else:
                raise ValueError(_ui('未知按钮。'))
            draft = self._draft(uid, cid, action, page, values)
            bot.answer(qid)
            return self._preview(uid, draft, summary, mid)
        except (ValueError, KeyError, OverflowError) as exc:
            return bot.answer(qid, error_text(exc)[:180], alert=True)

    def commit(self, uid, draft):
        bot, store, cid = self.bot, self.bot.store, draft["cid"]
        op, v = draft["op"], draft["values"]
        if op == "flood_config":
            return bot.flood_ui.commit(draft)
        if op.startswith("library_") or op in ("word_import", "pack_exclude", "pack_restore"):
            return bot.word_ui.commit(uid, draft)
        if op in ("word_mode", "word_delete"):
            store.edit_word_rule(cid, v["expected"]["key"], v["mode"], expected=v["expected"])
            return _ui('关键词已删除。') if v["mode"] is None else _ui('关键词匹配方式已更新。')
        if op == "raid":
            if store.raid_until(cid) != v["previous"]:
                raise ValueError(_ui('保护状态已变化，请重新操作。'))
            if v["action"] == "start" and (not bot.is_supergroup(cid) or not bot.bot_can(cid, "can_restrict_members")):
                raise ValueError(_ui('需要超级群组和机器人限制成员权限。'))
            store.set_raid_until(cid, int(time.time()) + v["duration"] * 60 if v["action"] == "start" else 0)
            bot.join_guard.clear(cid)
            return _ui('保护已开始。') if v["action"] == "start" else _ui('额外保护已停止；已有验证题仍有效。')
        if op == "domain_add":
            count = store.add_filters_bulk(cid, "domain", [(s,s) for s in v["domains"]])
            return _ui('已添加 {p0} 条域名白名单。', p0=count)
        if op in ("welcome_text", "rules_text", "log_chat_id"):
            if store.settings(cid)[op] != v["previous"]:
                raise ValueError(_ui('设置已被其他管理员修改，请重新操作。'))
            value = v["target"] if op == "log_chat_id" else v["text"]
            if op == "log_chat_id" and value:
                if value == cid or not bot.is_admin(value, uid):
                    raise ValueError(_ui('必须使用独立日志群，且你在目标群也有管理权限。'))
                member = bot.api.safe("getChatMember", chat_id=value, user_id=bot.bot_id)
                chat = bot.api.safe("getChat", chat_id=value)
                if (not isinstance(member,dict) or member.get("status") not in ("creator","administrator")
                        or not isinstance(chat,dict) or chat.get("type") not in ("group","supergroup","channel")
                        or chat.get("type")=="channel" and member.get("status")!="creator" and not member.get("can_post_messages")):
                    raise ValueError(_ui('机器人必须具有目标群管理权限及频道发消息权限。'))
            store.set(cid, op, value)
        elif op == "reply_add":
            key, label = v["keywords"]
            current = next((r for r in store.replies(cid) if r["key"]==key), None)
            if current != v["previous"]:
                raise ValueError(_ui('该回复已被其他管理员修改，请重新预览。'))
            store.add_reply(cid, key, label, v["body"], v["buttons"], v.get("media_type"), v.get("media_id"))
        elif op in ("domain", "reply"):
            rows = store.filters(cid, "domain") if op=="domain" else store.replies(cid)
            if next((r for r in rows if r["key"]==v["key"]), None) != v:
                raise ValueError(_ui('条目已变化，请重新预览。'))
            store.del_filter(cid,"domain",v["key"]) if op=="domain" else store.del_reply(cid,v["key"])
        elif op.startswith("schedule_"):
            mode = op.split("_",1)[1]
            run = next_occurrence(mode,v["rule"],store.tz)
            store.add_schedule(cid,uid,v["title"],v["body"],mode,v["rule"],run)
        elif op == "item_add":
            bot.economy.add_item(cid,uid,v["title"],v["description"],v["price"],v["stock"])
        elif op == "item":
            if bot.economy.item(cid,v["id"]) != v:
                raise ValueError(_ui('商品状态已变化，请重新预览。'))
            bot.economy.disable_item(cid,v["id"])
        elif op in ("fulfill", "refund"):
            bot.economy.process_order(cid,v["id"],uid,op)
        elif op == "raffle_add":
            bot.raffles.create(cid,uid,v["title"])
        elif op in ("draw", "raffle_cancel"):
            active = bot.raffles.active(cid)
            if not active or active["id"]!=v["id"]:
                raise ValueError(_ui('活动已变化，请重新预览。'))
            if op == "draw":
                result = bot.draw_raffle(cid,uid,expected_id=v["id"])
                # Announcement is best effort; result persists even when delivery fails.
                body = _ui('🎉 免费抽奖 #{p0} {p1}\n参与 {p2} 人 · 获胜成员ID：{p3}', p0=result['id'], p1=result['title'], p2=result['count'], p3=result['winner_id'])
                posted = bot.send(cid,body)
                return body + (_ui('\n群内公告发送失败，结果已保存。') if not posted else "")
            bot.raffles.cancel(cid,uid)
        elif op == "ocr":
            if v["mode"] != "off" and not bot.ocr.available:
                raise ValueError(_ui('OCR引擎未启用。需配置 WITH_OCR=1、OCR_ENABLED=1 并构建容器。'))
            if v["mode"]=="delete" and not bot.bot_can(cid,"can_delete_messages"):
                raise ValueError(_ui('机器人缺少删除消息权限。'))
            store.set(cid,"ocr_mode",v["mode"])
        elif op == "point_adjust":
            person = bot.api.safe("getChatMember",chat_id=cid,user_id=v["target"])
            if not is_current_member(person):
                raise ValueError(_ui('目标不是当前群成员。'))
            balance = store.change_points(cid,v["target"],v["amount"],uid)
            return _ui('积分已调整：成员 {p0}，当前余额 {p1}。', p0=v['target'], p1=balance)
        elif op.startswith("member_"):
            return self._member(cid,uid,op[7:],v)
        else:
            raise ValueError(_ui('未知操作。'))
        return _ui('已保存，请返回功能页面查看。')

    def _member(self,cid,uid,action,values):
        from .service import RESTRICTED
        bot, target = self.bot, values["target"]
        auto_warning = action == "warn"
        if not bot.can_target(cid,target):
            raise ValueError(_ui('目标是管理员、本机器人或身份无法核验。'))
        if action == "unwarn":
            bot.store.clear_warning(cid,target)
            return _ui('已清除警告。')
        if not bot.bot_can(cid,"can_restrict_members"):
            raise ValueError(_ui('机器人缺少限制成员权限。'))
        if action == "warn":
            count = bot.store.warning(cid,target)
            bot.store.stat(cid,"actions")
            bot.log(cid,_ui('警告 {p0}', p0=count),actor=uid,target=target)
            if count < bot.store.settings(cid)["warn_limit"]:
                return _ui('成员 {p0} 当前 {p1} 次警告。', p0=target, p1=count)
            action, values = "mute", {"minutes":60}
        if action in ("mute","unmute") and not bot.is_supergroup(cid):
            raise ValueError(_ui('禁言仅支持超级群组。'))
        if action == "mute":
            ok = bot.api.safe("restrictChatMember",chat_id=cid,user_id=target,
                              permissions=RESTRICTED,until_date=int(time.time())+values["minutes"]*60)
        elif action == "unmute":
            ok = bot.unrestrict(cid,target)
        elif action in ("ban","kick"):
            ok = bot.api.safe("banChatMember",chat_id=cid,user_id=target)
            if ok is True and action=="kick":
                ok = bot.api.safe("unbanChatMember",chat_id=cid,user_id=target,only_if_banned=True)
                if ok is not True:
                    bot.store.stat(cid,"actions")
                    bot.log(cid,_ui('已移出，解除封禁失败'),actor=uid,target=target)
                    raise ValueError(_ui('已移出成员，但解除封禁失败；请稍后使用「解除封禁」允许重新加入。'))
        else:
            ok = bot.api.safe("unbanChatMember",chat_id=cid,user_id=target,only_if_banned=True)
        if ok is not True:
            raise ValueError(_ui('Telegram操作未确认成功，请核对权限和成员状态后重新操作。'))
        if action=="mute" and auto_warning:
            bot.store.clear_warning(cid,target)
        bot.store.stat(cid,"actions")
        bot.log(cid,action,actor=uid,target=target)
        label = {"mute":_ui('禁言'),"unmute":_ui('解除禁言'),"ban":_ui('封禁'),"kick":_ui('移出，可重新加入'),"unban":_ui('解除封禁')}[action]
        return _ui('成员 {p0} 操作已完成：{p1}。', p0=target, p1=label)
