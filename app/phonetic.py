"""Token trie for polyphones, opt-in initials and contextual homophones.

A Han edge accepts full readings without multiplying all rule combinations.
Ambiguous expansions require independent nearby solicitation, with stricter
short-word checks. Work is bounded even for heavily ambiguous initial strings.
"""
from functools import lru_cache
import json
import re

from .mixed_pinyin import readings, latin_letter
from .word_library import ROOT

_CLAUSE = re.compile(r"[\n\r。！？!?；;]")
_PROTECTED = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]*(?:`|$)|(?:https?|tg|ftp)://[^\s<>，。！？；]+|(?<!\w)[@/][\w\u3400-\u9fff]+", re.I)
_WARNING = re.compile(r"(?:警惕|谨防|抵制|拒绝|切勿|不要(?:相信|参与|点击|购买)|反诈提醒)[^，,]{0,12}$")
_WARNING_AFTER = re.compile(r"^[^，,]{0,8}(?:是骗局|是诈骗|属于骗局|涉嫌诈骗|不要相信)")
MAX_STATES = 1024
MAX_WORK = 200_000


@lru_cache(maxsize=1)
def polyphones():
    return json.loads((ROOT / "pinyin-polyphones.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def phrase_data():
    phrases = json.loads((ROOT / "pinyin-phrases.json").read_text(encoding="utf-8"))
    lengths = {}
    for word in phrases:
        lengths.setdefault(word[0], set()).add(len(word))
    return phrases, {c: sorted(v, reverse=True) for c, v in lengths.items()}


@lru_cache(maxsize=1)
def contexts():
    data = json.loads((ROOT / "phonetic-contexts.json").read_text(encoding="utf-8"))
    data["short"] = {word: tuple(group["hints"]) for group in data["short_groups"] for word in group["words"]}
    data["benign"] = frozenset(map(tuple, data["benign_pairs"]))
    data["acronyms"] = frozenset(data["acronyms"])
    return data


def preferred(text):
    """Longest phrase correction, otherwise primary reading; no Cartesian product."""
    table, (phrases, lengths) = readings(), phrase_data()
    result, pos = [], 0
    while pos < len(text):
        word = next((text[pos:pos+n] for n in lengths.get(text[pos], ()) if text[pos:pos+n] in phrases), None)
        if word:
            result.extend(phrases[word])
            pos += len(word)
        else:
            result.append(table.get(text[pos]))
            pos += 1
    return result


def prepare(text):
    symbols, offsets, run = [], [], 0
    for pos, char in enumerate(text):
        if char in " '\u2019-\t":
            run += 1
            if run <= 3:
                continue
            char = "\x00"
        else:
            run = 0
        symbols.append(latin_letter(char) or char)
        offsets.append(pos)
    return "".join(symbols), offsets, preferred(text), tuple((m.start(), m.end()) for m in _PROTECTED.finditer(text))


def solicitation(text, a, b, phrase, span, short=False):
    """Cue must be outside the matched span, within 24 chars in its clause."""
    before, after = text[max(0, a-24):a], text[b:b+24]
    before = _CLAUSE.split(before)[-1]
    after = _CLAUSE.split(after)[0]
    data = contexts()
    if (_WARNING.search(before) or _WARNING_AFTER.search(after)
            or any(source in phrase and phrase.replace(source, safe) == span for source, safe in data["benign"])):
        return False
    if not any(cue in before or cue in after for cue in data["solicitation"]):
        return False
    return not short or any(cue in before or cue in after for cue in data["short"].get(phrase, ()))


def has_solicitation(text):
    # Cheap gate before constructing any fuzzy index; terminal checks remain local.
    return any(cue in text for cue in contexts()["solicitation"])


def token_boundary(text, a, b):
    def token(c):
        return latin_letter(c) or c == "_" or c.isascii() and c.isalnum()
    return not (a and token(text[a-1]) or b < len(text) and token(text[b]))


class PhoneticIndex:
    def __init__(self, entries, simplify=None, *, polyphonic_only=False):
        self.edges, self.outputs, self.depth = [{}], [[]], [0]
        self.characters = [""]
        self.rules, self.spellings, self.initials = {}, {}, {}
        table, multiple = readings(), polyphones()
        for index, (original, raw) in enumerate(entries):
            phrase = simplify(raw) if simplify else raw
            han = sum(c in table for c in phrase)
            if han < 2 or polyphonic_only and not any(c in multiple for c in phrase):
                continue
            node = 0
            for char in phrase:
                child = self.edges[node].get(char)
                if child is None:
                    child = len(self.edges)
                    self.edges[node][char] = child
                    self.edges.append({})
                    self.outputs.append([])
                    self.depth.append(self.depth[node]+1)
                    self.characters.append(char)
                node = child
            self.outputs[node].append(index)
            self.rules[index] = (original, raw, phrase, preferred(phrase), han)

    def maps(self, node):
        if node not in self.spellings:
            full, initials = {}, {}
            table, multiple = readings(), polyphones()
            for char, child in self.edges[node].items():
                if char not in table:
                    continue
                for value in multiple.get(char, (table[char],)):
                    full.setdefault(value, set()).add(child)
                    initials.setdefault(value[0], set()).add(child)
            self.spellings[node] = {k: tuple(sorted(v)) for k, v in full.items()}
            self.initials[node] = {k: tuple(sorted(v)) for k, v in initials.items()}
        return self.spellings[node], self.initials[node]

    def find(self, text, *, initials=False, homophones=False, pinyin=False, context="", waived=(),
             excluded=(), ocr=False, prepared=None, before=None):
        # Abbreviations and new homophones never drive OCR deletion.
        if ocr and (initials or homophones):
            return None
        fuzzy = initials or homophones
        if fuzzy and not has_solicitation(text):
            return None
        stream, offsets, pronunciation, protected = prepared if prepared is not None else prepare(text)
        table, waiting, best, work = readings(), {}, None, 0
        root_sounds, root_initials = self.maps(0)
        root_starts = self.edges[0].keys() | root_initials.keys()
        for pos, char in enumerate(stream):
            states = waiting.pop(pos, set())
            source_pos = offsets[pos]
            previous = text[source_pos-1] if source_pos else ""
            can_start = not previous or not (latin_letter(previous) or previous == "_" or previous.isascii() and previous.isalnum())
            possible = char in root_starts or homophones and pronunciation[source_pos] in root_sounds
            if not states and not (can_start and possible):
                continue
            # node, start, exact Han anchors, full syllables, initials, homophone trace
            if can_start and possible:
                states.add((0, pos, 0, 0, 0, ()))
            for node, start, anchors, full, short, trace in sorted(states):
                transitions = []
                child = self.edges[node].get(char)
                if child is not None:
                    transitions.append((pos+1, child, anchors+(char in table), full, short, trace))
                spellings, initials_map = self.maps(node)
                if "a" <= char <= "z":
                    if pinyin or initials:
                        for length in range(1, min(6, len(stream)-pos)+1):
                            value = stream[pos:pos+length]
                            if not all("a" <= c <= "z" for c in value):
                                break
                            for target in spellings.get(value, ()):
                                transitions.append((pos+length, target, anchors, full+1, short, trace))
                    if initials:
                        transitions.extend((pos+1, target, anchors, full, short+1, trace)
                                           for target in initials_map.get(char, ()))
                elif homophones and char in table and len(trace) < 2:
                    from .variants import traditional_table
                    for target in spellings.get(pronunciation[offsets[pos]], ()):
                        if (target != child and char.translate(traditional_table()) !=
                                self.characters[target].translate(traditional_table())):
                            transitions.append((pos+1, target, anchors, full, short,
                                                trace+((self.depth[node], pronunciation[offsets[pos]]),)))
                for end, target, exact, spelled, abbreviated, substitutions in transitions:
                    work += 1
                    if work > MAX_WORK:
                        return best[1] if best else None
                    for index in self.outputs[target]:
                        original, raw, phrase, sounds, han = self.rules[index]
                        if (best and index >= best[0] or before is not None and index >= before
                                or raw in excluded or (raw, context) in waived
                                or ocr and len(phrase) < 4):
                            continue
                        a, b = offsets[start], offsets[end-1]+1
                        if not token_boundary(text, a, b) or any(a < y and b > x for x, y in protected):
                            continue
                        # Each requested feature must actually contribute to this hit.
                        if initials and not abbreviated or homophones and not substitutions:
                            continue
                        if not fuzzy and (not exact or not spelled):
                            continue
                        if pinyin and not initials and (not exact or not spelled):
                            continue
                        if initials and (han < 4 and exact < 1 or not exact and han < 4):
                            continue
                        if initials and not exact and (a and text[a-1] in table or b < len(text) and text[b] in table):
                            continue
                        if substitutions and (len(substitutions) > max(1, min(2, han//4))
                                              or exact < (han+1)//2
                                              or any(sounds[n] != value for n, value in substitutions)):
                            continue
                        span = text[a:b]
                        if homophones and any(
                                span[n] in table and span[n+1] in " '\u2019-\t" and
                                next((c for c in span[n+1:] if c not in " '\u2019-\t"), "") in table
                                for n in range(len(span)-1)):
                            # Han-to-Han gaps belong to the separate split mode.
                            continue
                        if fuzzy and (span in contexts()["acronyms"]
                                      or not solicitation(text, a, b, phrase, span, short=han < 4)):
                            continue
                        best = (index, original)
                        if index == 0:
                            return original
                    if self.edges[target]:
                        state = (target, start, exact, spelled, abbreviated, substitutions)
                        bucket = waiting.setdefault(end, set())
                        if len(bucket) < MAX_STATES:
                            bucket.add(state)
        return best[1] if best else None
