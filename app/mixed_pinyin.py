"""Bounded Chinese + full-pinyin matching, without general homophone folding."""
import json
from functools import lru_cache
from pathlib import Path
import unicodedata

from .keyword_matcher import Automaton


@lru_cache(maxsize=1)
def readings():
    return json.loads((Path(__file__).with_name("wordlists") / "pinyin-characters.json").read_text(encoding="utf-8"))


def latin_letter(char):
    value = unicodedata.normalize("NFD", char.lower()).replace("u\u0308", "v")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return value if len(value) == 1 and "a" <= value <= "z" else None


def encoded(text):
    """Map each emitted letter back to its original character and offset.

    A barrier stays a barrier: no joining sentences, buttons, digits or emoji.
    Only up to three spaces/apostrophes/hyphens are ignorable inside a match.
    """
    table, out, offsets, starts, ends = readings(), [], [], [], []
    for i, char in enumerate(text):
        starts.append(len(out))
        value = table.get(char) or latin_letter(char) or char
        if char in " '\u2019-\t":
            # Retain runs over three separators as barriers.
            run = 1
            while i-run >= 0 and text[i-run] in " '\u2019-\t":
                run += 1
                if run > 3:
                    break
            value = "" if run <= 3 else "\x00"
        out.extend(value)
        offsets.extend([i] * len(value))
        ends.append(len(out))
    return "".join(out), offsets, starts, ends


class MixedPinyinIndex:
    def __init__(self, entries, simplify=None):
        self.rules = {}
        patterns = []
        table = readings()
        for index, (original, phrase) in enumerate(entries):
            raw_phrase = phrase
            phrase = simplify(phrase) if simplify else phrase
            # Pinyin short-hands and one-Han rules are deliberately excluded.
            if sum(c in table for c in phrase) < 2:
                continue
            spelling, slots, position = [], {}, 0
            for char in phrase:
                value = table.get(char, char)
                if char in table:
                    slots[position] = (char, len(value))
                spelling.append(value)
                position += len(value)
            pattern = "".join(spelling)
            patterns.append((pattern, index))
            self.rules[index] = (original, raw_phrase, pattern, slots)
        self.matcher = Automaton(patterns)

    def find(self, text, *, context="", waived=(), excluded=(), ocr=False, encoding=None):
        spelling, offsets, starts, ends = encoding if encoding is not None else encoded(text)
        best = None
        table = readings()
        for index, end in self.matcher.matches(spelling):
            original, phrase, pattern, slots = self.rules[index]
            if (best is not None and index >= best[0] or phrase in excluded
                    or (phrase, context) in waived or ocr and len(phrase) < 4):
                continue
            start = end - len(pattern)
            a, b = offsets[start], offsets[end-1]+1
            # Never enter/exit halfway through a Han syllable or English token.
            if starts[a] != start or ends[b-1] != end:
                continue
            if (a and (latin_letter(text[a-1]) or text[a-1] == "_" or text[a-1].isascii() and text[a-1].isalnum())
                    or b < len(text) and (latin_letter(text[b]) or text[b] == "_" or text[b].isascii() and text[b].isalnum())):
                continue
            span = text[a:b]
            if not any(c in table for c in span) or not any(latin_letter(c) for c in span):
                continue
            valid = True
            for pos in range(a, b):
                char = text[pos]
                if char not in table:
                    continue
                slot = slots.get(starts[pos]-start)
                if slot != (char, ends[pos]-starts[pos]):
                    valid = False
                    break
            if valid:
                best = (index, original)
                if index == 0:
                    break
        return best[1] if best else None
