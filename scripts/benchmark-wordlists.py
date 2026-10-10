"""Warm contains-only scan microbenchmark; excludes DB, OCR and Telegram I/O."""
import json
import sys
import time
import timeit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.moderation import compile_words, match_words, normalize
from app.word_library import WordPolicy, catalog


def main():
    text = ("普通群聊消息，讨论软件开发、阅读笔记和生活经验。"*40)[:1000]
    clean = normalize(text)
    results = []
    for size in (10_000, 50_000):
        words = tuple(f"独立广告规则{i:06}完成" for i in range(size))
        started = time.perf_counter()
        matcher = compile_words(words)
        build_ms = (time.perf_counter()-started)*1000
        optimized = lambda: match_words(text, matcher)
        # Same literal contains-only semantics as a sequential preprocessed list.
        sequential = lambda: next((r[0] for r in matcher.rules if r[1] in clean), None)
        assert optimized() is None and sequential() is None
        iterations = 100
        measure = lambda f: min(timeit.repeat(f, number=iterations, repeat=3))*1000/iterations
        results.append({"rules": size, "characters": len(text), "build_ms": round(build_ms, 3),
                        "warm_scan_ms": round(measure(optimized), 4),
                        "sequential_reference_ms": round(measure(sequential), 4),
                        "plain_trie_states": len(matcher.plain.edges),
                        "iterations": iterations, "repeats": 3})
    settings = {"word_traditional_mode": "enforce", "word_homophone_mode": "enforce", "word_short_split_mode": "enforce"}
    started = time.perf_counter()
    policy = WordPolicy([{"key": word} for word in words], {}, [], {key: "enforce" for key in catalog()}, [])
    variant_scan = lambda: policy.hit({"text": text}, settings=settings)
    assert variant_scan() is None
    cold_ms = (time.perf_counter()-started)*1000
    variant = {"custom_rules": len(words), "packs": len(catalog()), "characters": len(text),
               "all_variants": "enforce", "additional_cold_ms": round(cold_ms, 3),
               "warm_scan_ms": round(measure(variant_scan), 4)}
    print(json.dumps({"python": sys.version.split()[0], "results": results, "variants": variant}, indent=2))


if __name__ == "__main__":
    main()
