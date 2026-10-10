"""Linear multi-pattern scanning without external dependencies or user regexes."""
from collections import deque


class Automaton:
    def __init__(self, patterns):
        self.edges, self.fail, self.output, self.link = [{}], [0], [[]], [0]
        for pattern, index in patterns:
            node = 0
            for char in pattern:
                child = self.edges[node].get(char)
                if child is None:
                    child = len(self.edges)
                    self.edges[node][char] = child
                    self.edges.append({})
                    self.fail.append(0)
                    self.output.append([])
                    self.link.append(0)
                node = child
            self.output[node].append(index)
        queue = deque(self.edges[0].values())
        while queue:
            node = queue.popleft()
            for char, child in self.edges[node].items():
                fallback = self.fail[node]
                while fallback and char not in self.edges[fallback]:
                    fallback = self.fail[fallback]
                fallback = self.edges[fallback].get(char, 0)
                self.fail[child] = fallback
                self.link[child] = fallback if self.output[fallback] else self.link[fallback]
                queue.append(child)

    def matches(self, text):
        node = 0
        for end, char in enumerate(text, 1):
            while node and char not in self.edges[node]:
                node = self.fail[node]
            node = self.edges[node].get(char, 0)
            terminal = node
            while terminal:
                for index in self.output[terminal]:
                    yield index, end
                terminal = self.link[terminal]


class KeywordMatcher:
    """Rules are (original, normalized, compact, mode, loose_allowed)."""
    def __init__(self, rules):
        self.rules = tuple(rules)
        self.plain = Automaton((r[1], i) for i, r in enumerate(self.rules) if r[3] != "exact")
        self.loose = Automaton((r[2], i) for i, r in enumerate(self.rules) if r[3] == "contains" and r[4])
        self.exact = {}
        for i, rule in enumerate(self.rules):
            if rule[3] == "exact":
                self.exact.setdefault(rule[1], []).append(i)

    def find(self, clean, *, dense=None, waived=frozenset(), excluded=frozenset(),
             context=None, exact_only=False):
        context = clean if context is None else context
        best = None
        def consider(index, end=None):
            nonlocal best
            original, phrase, _, mode, _ = self.rules[index]
            if best is not None and index >= best or phrase in excluded or (phrase, context) in waived:
                return
            if mode == "word" and end is not None:
                start = end-len(phrase)
                if (start and (clean[start-1].isalnum() or clean[start-1] == "_")
                        or end < len(clean) and (clean[end].isalnum() or clean[end] == "_")):
                    return
            best = index
        for index in self.exact.get(clean, ()):
            consider(index)
        if not exact_only:
            for index, end in self.plain.matches(clean):
                consider(index, end)
                if best == 0:
                    break
            if dense is not None and best != 0:
                for index, _ in self.loose.matches(dense):
                    consider(index)
                    if best == 0:
                        break
        return self.rules[best][0] if best is not None else None
