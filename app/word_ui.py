"""Private library controls reuse AdminUI's authorization and single-use drafts."""
import hashlib
import json
from collections import Counter

from .moderation import WORD_MODES
from .word_library import catalog, PACK_MODES


class WordUI:
    def __init__(self, bot):
        self.bot = bot

    def home(self, cid):
        store, b = self.bot.store, self.bot._button
        packs, modes = catalog(), store.pack_modes(cid)
        total = len({w for pack in packs.values() for w in pack.words})
        excluded = [r[0] for r in store.db.execute("SELECT key FROM word_exclusions WHERE chat_id=? ORDER BY key LIMIT 20", (cid,))]
        count = store.db.execute("SELECT count(*) FROM word_exclusions WHERE chat_id=?", (cid,)).fetchone()[0]
        text = (f"📚 分类违禁词库 · {len(packs)}类 / {total}条去重短语\n"
                "原创招揽组合与有许可证的公开词库；没有其他机器人的私有词库或实测命中率数据。\n"
                "所有分类默认关闭，建议先观察，再按群主题逐类启用处罚。\n"
                "分类观察只记录，不覆盖链接、刷屏等原有处罚；全局观察优先于所有分类。\n"
                "自定义同名词优先；关闭总违禁词开关后，分类也不执行。\n"
                f"本群排除 {count} 条（前20条）：" + ("、".join(excluded) or "无"))
        kb = [[b(f"{pack.name} · {len(pack.words)} · {PACK_MODES[modes.get(pack.id, 'off')]}",
                 f"ad:{cid}:pack:{pack.id}")] for pack in packs.values()]
        kb += [[b("🧪 定向分类全部先观察", f"ad:{cid}:packpreset:recommended")],
               [b("🧪 所有分类全部先观察", f"ad:{cid}:packpreset:all")],
               [b("➖ 排除内置词", f"ad:{cid}:begin:pack_exclude"),
                b("➕ 恢复内置词", f"ad:{cid}:begin:pack_restore")],
               [b("⬅️ 违禁词管理", f"v:{cid}:words")]]
        return text, kb

    def detail(self, uid, cid, pack_id, page=0, mid=None):
        pack, store, admin, b = catalog()[pack_id], self.bot.store, self.bot.admin_ui, self.bot._button
        pages = max(1, (len(pack.words)+7)//8)
        page = max(0, min(page, pages-1))
        state = store.pack_modes(cid).get(pack_id, "off")
        text = (f"📚 {pack.name}\n{len(pack.words)}条 · {PACK_MODES[state]} · 第{page+1}/{pages}页\n"
                f"{pack.description}\n点击词条可排除，仅影响本群内置词库。\n\n"
                + "\n".join(pack.words[page*8:page*8+8]))
        kb = [[b(label, f"ad:{cid}:packmode:{pack_id}_{mode}") for mode, label in PACK_MODES.items()]]
        for word in pack.words[page*8:page*8+8]:
            token = admin._token(admin.word_choices, uid, cid, key=word, pack=pack_id)
            kb.append([b("➖ 排除 "+word[:35], f"ad:{cid}:pexclude:{token}")])
        nav = []
        if page:
            nav.append(b("◀️ 上一页", f"ad:{cid}:packpage:{pack_id}_{page-1}"))
        if page+1 < pages:
            nav.append(b("下一页 ▶️", f"ad:{cid}:packpage:{pack_id}_{page+1}"))
        if nav:
            kb.append(nav)
        return self.bot._edit_or_send(uid, mid, text, kb+admin.back(cid, "wordpacks"))

    def preview_import(self, uid, draft):
        cid, bundle = draft["cid"], draft["values"]["content"]
        entries = bundle["entries"]
        existing = {r[0] for r in self.bot.store.db.execute("SELECT key FROM filters WHERE chat_id=? AND kind='word'", (cid,))}
        added = sum(key not in existing for key, _, _ in entries)
        fingerprint = hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()
        counts = Counter(mode for _, _, mode in entries)
        summary = (f"导入 {len(entries)} 个不同词条，文件内重复 {bundle['duplicates']} 行。\n"
                   f"新增 {added} 条；已存在 {len(entries)-added} 条保持原内容和匹配方式。\n"
                   + " / ".join(f"{WORD_MODES[k]} {v}" for k, v in counts.items())
                   + f"\n内容指纹：{fingerprint}\n前12条预览：\n"
                   + "\n".join(f"{WORD_MODES[mode]}：{label}" for _, label, mode in entries[:12])
                   + "\n整批原子保存；不会自动打开违禁词总开关或更改处罚。")
        return self.bot.admin_ui._preview(uid, draft, summary)

    def callback(self, uid, cid, action, arg, mid, qid):
        bot, admin, store = self.bot, self.bot.admin_ui, self.bot.store
        if action in ("pack", "packpage"):
            pack_id, page = (arg, 0) if action == "pack" else (arg.rsplit("_", 1)[0], int(arg.rsplit("_", 1)[1]))
            if pack_id not in catalog():
                raise ValueError("未知词库分类。")
            bot.answer(qid)
            return self.detail(uid, cid, pack_id, page, mid)
        if action == "wordexport":
            cursor = {"after": "", "part": 1, "revision": store.word_revision(cid)} if arg == "0" else admin._choice(admin.exports, uid, cid, arg)
            if cursor["revision"] != store.word_revision(cid):
                raise ValueError("词库已变化，请重新导出，避免遗漏。")
            data, last, more, count = store.export_words(cid, cursor["after"])
            bot.answer(qid)
            if not count:
                return bot.send(uid, "当前没有可导出的自定义词条。", reply_markup={"inline_keyboard": admin.back(cid, "words")})
            sent = bot.api.safe("sendDocument", chat_id=uid, caption=f"自定义词库第{cursor['part']}部分，共{count}条；不含内置分类、排除词及精确例外。",
                _files={"document": (f"groupguard{cid}-words-{cursor['part']}.jsonl", data, "application/x-ndjson")})
            if not sent:
                raise ValueError("导出发送失败，请重试；词库未修改。")
            kb = admin.back(cid, "words")
            if more:
                token = admin._token(admin.exports, uid, cid, after=last, part=cursor["part"]+1, revision=cursor["revision"])
                kb.insert(0, [bot._button("📤 下载下一部分", f"ad:{cid}:wordexport:{token}")])
            return bot.send(uid, f"✅ 已导出{count}条。" + ("还有更多，请下载下一部分。" if more else "已到末尾。"), reply_markup={"inline_keyboard": kb})
        values = {"revision": store.word_revision(cid)}
        if action in ("packmode", "packpreset"):
            if action == "packmode":
                pack_id, mode = arg.rsplit("_", 1)
                if pack_id not in catalog() or mode not in PACK_MODES:
                    raise ValueError("未知词库分类或模式。")
                values["changes"] = {pack_id: mode}
            elif arg in ("recommended", "all"):
                values["changes"] = {p.id: "observe" for p in catalog().values() if arg == "all" or p.recommended}
            else:
                raise ValueError("未知组合。")
            summary = "\n".join(f"{catalog()[key].name} → {PACK_MODES[mode]}" for key, mode in values["changes"].items())
            summary += "\n观察不处罚；启用处罚可能误伤引用，请先测试。全局观察及总开关仍优先。"
            op = "library_modes"
        else:
            choice = admin._choice(admin.word_choices, uid, cid, arg)
            if "pack" not in choice:
                raise ValueError("词库按钮已失效。")
            values["text"] = choice["key"]
            op, summary = "pack_exclude", "从本群内置词库排除："+choice["key"]+"\n同名自定义规则仍生效。"
        draft = admin._draft(uid, cid, op, "wordpacks", values)
        bot.answer(qid)
        return admin._preview(uid, draft, summary, mid)

    def commit(self, uid, draft):
        cid, values, op = draft["cid"], draft["values"], draft["op"]
        store = self.bot.store
        if op == "word_import":
            count = store.import_words(cid, values["content"]["entries"], expected=values["revision"])
            return f"新增{count}条自定义词，已有规则保持原样。"
        if op == "library_modes":
            if "enforce" in values["changes"].values() and not self.bot.bot_can(cid, "can_delete_messages"):
                raise ValueError("机器人缺少删除消息权限。")
            store.set_pack_modes(cid, values["changes"], expected=values["revision"])
            return "分类词库模式已更新。"
        if op in ("pack_exclude", "pack_restore"):
            store.exclude_word(cid, values["text"], exclude=op == "pack_exclude", expected=values["revision"])
            return "内置词排除设置已更新。"
        raise ValueError("未知词库操作。")
