"""Versioned optional dictionaries and strict, non-executable word import."""
import json
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path

from .moderation import compile_words, message_word_hit, normalize, word_waivers

ROOT = Path(__file__).with_name("wordlists")
PACK_MODES = {"off": "关闭", "observe": "仅观察", "enforce": "执行处罚"}
IMPORT_BYTES = 1_048_576


@dataclass(frozen=True)
class WordPack:
    id: str
    name: str
    description: str
    recommended: bool
    words: tuple
    matcher: object

    @cached_property
    def ocr_rules(self):
        from .ocr import compile_ocr_words
        return compile_ocr_words(self.words)


@lru_cache(maxsize=1)
def catalog():
    definitions = json.loads((ROOT / "curated.json").read_text(encoding="utf-8"))
    excluded = json.loads((ROOT / "public_exclusions.json").read_text(encoding="utf-8"))
    for definition in definitions:
        definition["words"] = definition["phrases"] + [a+b for a in definition.get("products", ())
                                                        for b in definition.get("suffixes", ())]
    for key, label in (("public_ads", "开源广告扩展"), ("public_adult", "开源成人词扩展"),
                       ("public_danger", "开源危险品词扩展")):
        definitions.append({"id": key, "name": label, "recommended": False,
            "description": "Apache-2.0公开词库，包含宽泛短词，误伤风险高；默认关闭，建议仅观察。不是私有机器人词库或实测命中率排名。",
            "words": (ROOT / (key+".txt")).read_text(encoding="utf-8-sig").splitlines()})
    packs = {}
    for item in definitions:
        skipped = {normalize(w) for w in excluded.get(item["id"], ())}
        words = tuple(sorted({normalize(w) for w in item["words"]
                              if 2 <= len(normalize(w)) <= 80 and normalize(w) not in skipped}))
        packs[item["id"]] = WordPack(item["id"], item["name"], item["description"],
                                    item["recommended"], words, compile_words(words))
    return packs


def parse_import(content):
    if isinstance(content, bytes):
        if len(content) > IMPORT_BYTES:
            raise ValueError("单个文件最多1 MiB；可分批导入，词库总条数不设固定上限。")
        try:
            content = content.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("文件需使用 UTF-8 编码。") from None
    if not isinstance(content, str) or len(content.encode("utf-8")) > IMPORT_BYTES:
        raise ValueError("单次导入最多1 MiB，可分批追加。")
    unique, total = {}, 0
    for number, line in enumerate(content.lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("{"):
                entry = json.loads(line)
                if not isinstance(entry, dict) or set(entry) - {"word", "mode", "label"}:
                    raise ValueError()
                word, mode = entry["word"], entry.get("mode", "contains")
                label = entry.get("label", word)
            else:
                fields = line.split("\t", 1)
                mode, word = fields if len(fields) == 2 else ("contains", line)
                label = word
            if (not isinstance(word, str) or not isinstance(label, str) or "\x00" in word or "\x00" in label
                    or mode not in ("contains", "word", "exact") or not 2 <= len(normalize(word)) <= 80
                    or not 1 <= len(label) <= 80):
                raise ValueError()
            word.encode("utf-8")
            label.encode("utf-8")
            key = normalize(word)
            row = (key, label, mode)
            if key in unique and unique[key][2] != mode:
                raise ValueError("相同词的匹配方式冲突")
            unique.setdefault(key, row)
            total += 1
        except (ValueError, KeyError, TypeError, RecursionError):
            raise ValueError(f"第{number}行无效或匹配方式冲突；整批尚未保存。格式：每行一词，或 contains/word/exact + Tab + 词。") from None
    if not unique:
        raise ValueError("文件中没有有效词条，未修改词库。")
    return {"entries": tuple(unique.values()), "total": total, "duplicates": total-len(unique)}


class WordPolicy:
    def __init__(self, rows, modes, exceptions, packs, exclusions):
        self.keys = frozenset(normalize(r["key"]) for r in rows)
        self.custom = compile_words(tuple(r["key"] for r in rows), modes)
        self.modes = modes
        self.exceptions = word_waivers(exceptions)
        self.excluded = self.keys | frozenset(exclusions)
        self.packs = [(catalog()[key], mode) for key, mode in packs.items()
                      if mode != "off" and key in catalog()]

    @cached_property
    def ocr_rules(self):
        from .ocr import compile_ocr_words
        return compile_ocr_words(tuple(r[0] for r in self.custom.rules), self.modes)

    def pack_hit(self, message, *, loose=False, include_observe=True):
        for mode in ("enforce", "observe") if include_observe else ("enforce",):
            for pack, state in self.packs:
                if state != mode:
                    continue
                word = message_word_hit(message, pack.matcher, loose=loose, exceptions=self.exceptions,
                                        excluded=self.excluded)
                if word is not None:
                    return {"word": word, "pack": pack.id, "observe": mode == "observe"}
        return None

    def hit(self, message, *, loose=False):
        word = message_word_hit(message, self.custom, loose=loose, exceptions=self.exceptions)
        return {"word": word, "pack": "", "observe": False} if word is not None else self.pack_hit(message, loose=loose)
