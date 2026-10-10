"""Opt-in, auditable Chinese variants. No general phonetic/fuzzy matching."""
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from .keyword_matcher import Automaton
from .moderation import compact, normalize

ROOT = Path(__file__).with_name("wordlists")
VARIANT_SETTINGS = {"traditional": ("word_traditional_mode", "繁简字形"),
                    "homophone": ("word_homophone_mode", "定向同音／拼音"),
                    "split": ("word_short_split_mode", "短词拆字增强")}
_CLAUSES = re.compile(r"([\n\r。！？!?；;]+)")


def is_han(char):
    return "\u3400" <= char <= "\u9fff" or "\U00020000" <= char <= "\U0003134f"


@lru_cache(maxsize=1)
def traditional_table():
    table = {}
    for line in (ROOT / "opencc-TSCharacters.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        source, targets = line.split("\t", 1)
        target = targets.split()[0]
        if len(source) == len(target) == 1:
            table[ord(source)] = target
    return table


def simplify(text):
    # Character folding only, not the complete OpenCC phrase/regional converter.
    return normalize(text).translate(traditional_table())


def join_short_gaps(text):
    """Only 1-3 adjacent separator characters between Han; never cross lines."""
    out, pos = [], 0
    while pos < len(text):
        char = text[pos]
        if out and is_han(out[-1]) and char not in "\r\n" and unicodedata.category(char)[0] in "PZSM":
            end = pos
            while end < len(text) and text[end] not in "\r\n" and unicodedata.category(text[end])[0] in "PZSM":
                end += 1
            if end-pos <= 3 and end < len(text) and is_han(text[end]):
                pos = end
                continue
        out.append(char)
        pos += 1
    return "".join(out)


class AliasIndex:
    def __init__(self):
        self.entries = []
        for item in json.loads((ROOT / "aliases.json").read_text(encoding="utf-8")):
            for alias in item["aliases"]:
                self.entries.append((normalize(alias), normalize(item["word"]),
                                     tuple(normalize(c) for c in item.get("context", ()))))
        self.matcher = Automaton((entry[0], i) for i, entry in enumerate(self.entries))

    def clause(self, text):
        choices = {}
        for index, end in self.matcher.matches(text):
            alias, canonical, hints = self.entries[index]
            start = end-len(alias)
            # ASCII aliases must not capture fragments of ordinary/code words.
            if alias[0].isascii() and alias[0].isalpha() and start and text[start-1].isascii() and text[start-1].isalnum():
                continue
            if alias[-1].isascii() and alias[-1].isalpha() and end < len(text) and text[end].isascii() and text[end].isalpha():
                continue
            before, after = text[max(0, start-24):start], text[end:end+24]
            if hints and not any(h in before or h in after for h in hints):
                continue
            if start not in choices or end > choices[start][0]:
                choices[start] = (end, canonical)
        out, pos = [], 0
        while pos < len(text):
            choice = choices.get(pos)
            if choice:
                pos, value = choice
                out.append(value)
            else:
                out.append(text[pos])
                pos += 1
        return "".join(out)

    def rewrite(self, text):
        return "".join(part if i % 2 else self.clause(part) for i, part in enumerate(_CLAUSES.split(text)))


@lru_cache(maxsize=1)
def aliases():
    return AliasIndex()


class VariantIndex:
    def __init__(self, rules):
        self.entries = [(original, phrase) for original, phrase, dense, mode, _ in rules
                        if mode == "contains" and len(dense) >= 2 and any(map(is_han, phrase))]
        self.engines = {}

    def engine(self, traditional=False, ocr=False):
        key = (traditional, ocr)
        if key not in self.engines:
            patterns = []
            for i, (_, phrase) in enumerate(self.entries):
                pattern = simplify(phrase) if traditional else phrase
                patterns.append((compact(pattern) if ocr else pattern, i))
            self.engines[key] = Automaton(patterns)
        return self.engines[key]

    def find(self, text, *, traditional=False, ocr=False, context="", waived=(), excluded=()):
        best = None
        for index, _ in self.engine(traditional, ocr).matches(text):
            original, phrase = self.entries[index]
            if (best is not None and index >= best or phrase in excluded or (phrase, context) in waived
                    or ocr and len(compact(phrase)) < 4):
                continue
            best = index
            if best == 0:
                break
        return self.entries[best][0] if best is not None else None


def candidates(message, settings, *, ocr=False):
    active = [(key, settings.get(name, "off")) for key, (name, _) in VARIANT_SETTINGS.items()
              if settings.get(name, "off") in ("observe", "enforce")]
    if not active:
        return
    parts = [normalize(message.get("text") or message.get("caption") or "")]
    parts += [normalize(b.get("text") or "") for row in (message.get("reply_markup") or {}).get("inline_keyboard", [])
              for b in row if isinstance(b, dict)]
    context = normalize("\n".join(parts))
    choices = []
    for mask in range(1, 1 << len(active)):
        features = {key for i, (key, _) in enumerate(active) if mask & (1 << i)}
        observe = any(mode == "observe" for i, (_, mode) in enumerate(active) if mask & (1 << i))
        choices.append((features, observe))
    choices.sort(key=lambda row: (row[1], len(row[0])))
    for features, observe in choices:
        rewritten, changed = [], set()
        for original in parts:
            text = compact(original) if ocr else original
            if "traditional" in features:
                text = simplify(text)
                changed.add("traditional")  # Rules may change even if the input doesn't.
            # Resolve intact Latin/emoji aliases before joining Han gaps; otherwise
            # joining can erase an alias (洗💰) or miss a gap after shuadan.
            if "homophone" in features:
                result = aliases().rewrite(text)
                if result != text:
                    changed.add("homophone")
                text = result
            if "split" in features:
                joined = join_short_gaps(text)
                if joined != text:
                    changed.add("split")
                text = joined
            # A second bounded pass resolves newly joined aliases such as 唰⭐单.
            if "homophone" in features and "split" in features:
                result = aliases().rewrite(text)
                if result != text:
                    changed.add("homophone")
                text = result
            rewritten.append(text)
        if not features <= changed:
            continue
        kind = "／".join(label for key, (_, label) in VARIANT_SETTINGS.items() if key in features)
        yield rewritten, "traditional" in features, observe, kind, context


def context_matches(word, text, settings):
    """Validate a precise waiver without applying a group's punitive policy."""
    if normalize(word) in normalize(text):
        return True
    from .moderation import compile_words
    index = VariantIndex(compile_words([word]).rules)
    return any(index.find(part, traditional=traditional, context=context)
               for parts, traditional, _, _, context in candidates({"text": text}, settings)
               for part in parts)
