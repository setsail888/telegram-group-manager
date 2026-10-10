"""Private library controls reuse AdminUI's authorization and single-use drafts."""
from .i18n import msg as _ui, join_text
import hashlib
import json
from collections import Counter

from .moderation import WORD_MODES
from .word_library import catalog, PACK_MODES
from .variants import VARIANT_SETTINGS


class WordUI:
    def __init__(self, bot):
        self.bot = bot

    def home(self, cid):
        store, b = self.bot.store, self.bot._button
        packs, modes = catalog(), store.pack_modes(cid)
        total = len({w for pack in packs.values() for w in pack.words})
        excluded = [r[0] for r in store.db.execute("SELECT key FROM word_exclusions WHERE chat_id=? ORDER BY key LIMIT 20", (cid,))]
        count = store.db.execute("SELECT count(*) FROM word_exclusions WHERE chat_id=?", (cid,)).fetchone()[0]
        text = (_ui('📚 分类违禁词库 · {p0}类 / {p1}条去重短语\n原创招揽组合与有许可证的公开词库；没有其他机器人的私有词库或实测命中率数据。\n所有分类默认关闭，建议先观察，再按群主题逐类启用处罚。\n分类观察只记录，不覆盖链接、刷屏等原有处罚；全局观察优先于所有分类。\n自定义同名词优先；关闭总违禁词开关后，分类也不执行。\n本群排除 {p2} 条（前20条）：', p0=len(packs), p1=total, p2=count) + (join_text('、', excluded) or _ui('无')))
        kb = [[b(_ui('{p0} · {p1} · {p2}', p0=pack.name, p1=len(pack.words), p2=PACK_MODES[modes.get(pack.id, 'off')]),
                 _ui('ad:{p0}:pack:{p1}', p0=cid, p1=pack.id))] for pack in packs.values()]
        kb += [[b(_ui('🧪 定向分类全部先观察'), _ui('ad:{p0}:packpreset:recommended', p0=cid))],
               [b(_ui('🧪 所有分类全部先观察'), _ui('ad:{p0}:packpreset:all', p0=cid))],
               [b(_ui('🈶 中文变体识别'), _ui('v:{p0}:wordvariants', p0=cid))],
               [b(_ui('➖ 排除内置词'), _ui('ad:{p0}:begin:pack_exclude', p0=cid)),
                b(_ui('➕ 恢复内置词'), _ui('ad:{p0}:begin:pack_restore', p0=cid))],
               [b(_ui('⬅️ 违禁词管理'), _ui('v:{p0}:words', p0=cid))]]
        return text, kb

    def variants(self, cid):
        modes, b = self.bot.store.variant_modes(cid), self.bot._button
        text = (_ui('🈶 中文变体识别\n只增强已添加或已启用分类中的「包含」规则；整词、整句规则保持原精度。\n繁简字形：賭博→赌博、刷單→刷单（字表转换，不是完整语义转换）。\n定向同音／拼音：唰单／shuadan→刷单；菠菜需同句附近有平台、投注等提示词。不是任意同音字通杀。\n短词拆字增强：刷⭐单→刷单，允许1–3个间隔符，不跨换行或按钮；可能误伤正常引用。\n拼音＋中文混写：shua单返佣、刷dan返yong→刷单返佣；只扩展至少两个汉字的包含词，不做首字母缩写或纯拼音通杀。\n四项默认关闭；组合命中时，所需功能或分类任一为观察，就仅观察。总开关和全局观察优先。\n先在分类词库选择「两字短词（严格）」或添加自定义词，再测试并观察。\n\n')
                + join_text('\n', (_ui('{p0}：{p1}', p0=label, p1=PACK_MODES[modes[key]]) for key, (_, label) in VARIANT_SETTINGS.items())))
        kb = []
        for key, (_, label) in VARIANT_SETTINGS.items():
            kb.append([b(_ui('{p0} · 当前{p1}', p0=label, p1=PACK_MODES[modes[key]]), _ui('v:{p0}:wordvariants', p0=cid))])
            kb.append([b(("✅ " if modes[key] == mode else "") + mode_label, _ui('ad:{p0}:variantmode:{p1}_{p2}', p0=cid, p1=key, p2=mode))
                       for mode, mode_label in PACK_MODES.items()])
        kb += [[b(_ui('🧪 四项全部先观察'), _ui('ad:{p0}:variantpreset:observe', p0=cid))],
               [b(_ui('🧪 测试关键词'), _ui('w:{p0}:test', p0=cid))],
               [b(_ui('📚 分类词库'), _ui('v:{p0}:wordpacks', p0=cid))]] + self.bot.admin_ui.back(cid, "words")
        return text, kb

    def detail(self, uid, cid, pack_id, page=0, mid=None):
        pack, store, admin, b = catalog()[pack_id], self.bot.store, self.bot.admin_ui, self.bot._button
        pages = max(1, (len(pack.words)+7)//8)
        page = max(0, min(page, pages-1))
        state = store.pack_modes(cid).get(pack_id, "off")
        text = (_ui('📚 {p0}\n{p1}条 · {p2} · 第{p3}/{p4}页\n{p5}\n点击词条可排除，仅影响本群内置词库。\n\n', p0=pack.name, p1=len(pack.words), p2=PACK_MODES[state], p3=page + 1, p4=pages, p5=pack.description)
                + join_text('\n', pack.words[page * 8:page * 8 + 8]))
        kb = [[b(label, _ui('ad:{p0}:packmode:{p1}_{p2}', p0=cid, p1=pack_id, p2=mode)) for mode, label in PACK_MODES.items()]]
        for word in pack.words[page*8:page*8+8]:
            token = admin._token(admin.word_choices, uid, cid, key=word, pack=pack_id)
            kb.append([b(_ui('➖ 排除 ')+word[:35], _ui('ad:{p0}:pexclude:{p1}', p0=cid, p1=token))])
        nav = []
        if page:
            nav.append(b(_ui('◀️ 上一页'), _ui('ad:{p0}:packpage:{p1}_{p2}', p0=cid, p1=pack_id, p2=page - 1)))
        if page+1 < pages:
            nav.append(b(_ui('下一页 ▶️'), _ui('ad:{p0}:packpage:{p1}_{p2}', p0=cid, p1=pack_id, p2=page + 1)))
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
        summary = (_ui('导入 {p0} 个不同词条，文件内重复 {p1} 行。\n新增 {p2} 条；已存在 {p3} 条保持原内容和匹配方式。\n', p0=len(entries), p1=bundle['duplicates'], p2=added, p3=len(entries) - added)
                   + join_text(' / ', (_ui('{p0} {p1}', p0=WORD_MODES[k], p1=v) for k, v in counts.items()))
                   + _ui('\n内容指纹：{p0}\n前12条预览：\n', p0=fingerprint)
                   + join_text('\n', (_ui('{p0}：{p1}', p0=WORD_MODES[mode], p1=label) for _, label, mode in entries[:12]))
                   + _ui('\n整批原子保存；不会自动打开违禁词总开关或更改处罚。'))
        return self.bot.admin_ui._preview(uid, draft, summary)

    def callback(self, uid, cid, action, arg, mid, qid):
        bot, admin, store = self.bot, self.bot.admin_ui, self.bot.store
        if action in ("pack", "packpage"):
            pack_id, page = (arg, 0) if action == "pack" else (arg.rsplit("_", 1)[0], int(arg.rsplit("_", 1)[1]))
            if pack_id not in catalog():
                raise ValueError(_ui('未知词库分类。'))
            bot.answer(qid)
            return self.detail(uid, cid, pack_id, page, mid)
        if action == "wordexport":
            cursor = {"after": "", "part": 1, "revision": store.word_revision(cid)} if arg == "0" else admin._choice(admin.exports, uid, cid, arg)
            if cursor["revision"] != store.word_revision(cid):
                raise ValueError(_ui('词库已变化，请重新导出，避免遗漏。'))
            data, last, more, count = store.export_words(cid, cursor["after"])
            bot.answer(qid)
            if not count:
                return bot.send(uid, _ui('当前没有可导出的自定义词条。'), reply_markup={"inline_keyboard": admin.back(cid, "words")})
            sent = bot.api.safe("sendDocument", chat_id=uid, caption=_ui('自定义词库第{p0}部分，共{p1}条；不含内置分类、排除词及精确例外。', p0=cursor['part'], p1=count),
                _files={"document": (_ui('groupguard{p0}-words-{p1}.jsonl', p0=cid, p1=cursor['part']), data, "application/x-ndjson")})
            if not sent:
                raise ValueError(_ui('导出发送失败，请重试；词库未修改。'))
            kb = admin.back(cid, "words")
            if more:
                token = admin._token(admin.exports, uid, cid, after=last, part=cursor["part"]+1, revision=cursor["revision"])
                kb.insert(0, [bot._button(_ui('📤 下载下一部分'), _ui('ad:{p0}:wordexport:{p1}', p0=cid, p1=token))])
            return bot.send(uid, _ui('✅ 已导出{p0}条。', p0=count) + (_ui('还有更多，请下载下一部分。') if more else _ui('已到末尾。')), reply_markup={"inline_keyboard": kb})
        values = {"revision": store.word_revision(cid)}
        if action in ("variantmode", "variantpreset"):
            if action == "variantmode":
                feature, mode = arg.rsplit("_", 1)
                if feature not in VARIANT_SETTINGS or mode not in PACK_MODES:
                    raise ValueError(_ui('未知变体识别模式。'))
                values["changes"] = {feature: mode}
            elif arg == "observe":
                values["changes"] = {key: "observe" for key in VARIANT_SETTINGS}
            else:
                raise ValueError(_ui('未知变体组合。'))
            values["expected"] = store.variant_modes(cid)
            summary = join_text('\n', (_ui('{p0} → {p1}', p0=VARIANT_SETTINGS[key][1], p1=PACK_MODES[mode]) for key, mode in values['changes'].items()))
            summary += _ui('\n仅增强已启用的包含规则；短词及同音可能误伤引用，请先测试观察。确认请求十分钟内有效，设置保存后持续生效。')
            op = "library_variants"
        elif action in ("packmode", "packpreset"):
            if action == "packmode":
                pack_id, mode = arg.rsplit("_", 1)
                if pack_id not in catalog() or mode not in PACK_MODES:
                    raise ValueError(_ui('未知词库分类或模式。'))
                values["changes"] = {pack_id: mode}
            elif arg in ("recommended", "all"):
                values["changes"] = {p.id: "observe" for p in catalog().values() if arg == "all" or p.recommended}
            else:
                raise ValueError(_ui('未知组合。'))
            summary = join_text('\n', (_ui('{p0} → {p1}', p0=catalog()[key].name, p1=PACK_MODES[mode]) for key, mode in values['changes'].items()))
            summary += _ui('\n观察不处罚；启用处罚可能误伤引用，请先测试。全局观察及总开关仍优先。')
            op = "library_modes"
        else:
            choice = admin._choice(admin.word_choices, uid, cid, arg)
            if "pack" not in choice:
                raise ValueError(_ui('词库按钮已失效。'))
            values["text"] = choice["key"]
            op, summary = "pack_exclude", _ui('从本群内置词库排除：')+choice["key"]+_ui('\n同名自定义规则仍生效。')
        draft = admin._draft(uid, cid, op, "wordvariants" if op == "library_variants" else "wordpacks", values)
        bot.answer(qid)
        return admin._preview(uid, draft, summary, mid)

    def commit(self, uid, draft):
        cid, values, op = draft["cid"], draft["values"], draft["op"]
        store = self.bot.store
        if op == "word_import":
            count = store.import_words(cid, values["content"]["entries"], expected=values["revision"])
            return _ui('新增{p0}条自定义词，已有规则保持原样。', p0=count)
        if op == "library_variants":
            if "enforce" in values["changes"].values() and not self.bot.bot_can(cid, "can_delete_messages"):
                raise ValueError(_ui('机器人缺少删除消息权限。'))
            store.set_variant_modes(cid, values["changes"], expected=values["expected"], revision=values["revision"])
            return _ui('中文变体识别已更新；已保存的设置持续生效。')
        if op == "library_modes":
            if "enforce" in values["changes"].values() and not self.bot.bot_can(cid, "can_delete_messages"):
                raise ValueError(_ui('机器人缺少删除消息权限。'))
            store.set_pack_modes(cid, values["changes"], expected=values["revision"])
            return _ui('分类词库模式已更新。')
        if op in ("pack_exclude", "pack_restore"):
            store.exclude_word(cid, values["text"], exclude=op == "pack_exclude", expected=values["revision"])
            return _ui('内置词排除设置已更新。')
        raise ValueError(_ui('未知词库操作。'))
