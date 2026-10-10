"""Opt-in OCR worker for Telegram photos, using local Tesseract only.

No cloud OCR, image persistence, raw OCR logs, or network connection to
third-party OCR providers. A single worker and an 8-item bound avoid blocking
the Telegram polling loop or overwhelming small NAS servers.
"""
import csv
import io
import logging
import math
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import BoundedSemaphore

from .api import TelegramAPIError
from .moderation import normalize, word_waivers
from .keyword_matcher import KeywordMatcher
from functools import lru_cache

LOG = logging.getLogger("groupguard.ocr")
MAX_OCR_FILE_BYTES = 3_000_000


def extract_tesseract(data):
    """Read text and a conservative aggregate confidence from Tesseract TSV."""
    if not isinstance(data, bytes) or len(data) > MAX_OCR_FILE_BYTES or not data:
        return ("", 0.0)
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "chi_sim+eng", "tsv"],
            input=data, capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ("", 0.0)
    if proc.returncode or len(proc.stdout) > 160_000:
        return ("", 0.0)
    tokens = []
    scores = []
    try:
        rows = csv.DictReader(io.StringIO(proc.stdout.decode("utf-8", "replace")),
                              delimiter="\t")
        for row in rows:
            word = row.get("text", "").strip()
            if not word:
                continue
            score = float(row.get("conf", "-1"))
            if score < 0 or not math.isfinite(score):
                continue
            tokens.append(word[:80])
            scores.append(score)
            if len(tokens) >= 300:
                break
    except (ValueError, TypeError, csv.Error):
        return ("", 0.0)
    if not scores:
        return ("", 0.0)
    return (" ".join(tokens)[:4000], sum(scores) / len(scores))


def _ocr_compact(text):
    return re.sub(r"[\s\W_]+", "", normalize(text), flags=re.UNICODE)


@lru_cache(maxsize=4)
def _ocr_rules(keywords, modes):
    modes, dense_rules, word_rules, positions = dict(modes), [], [], {}
    for index, phrase in enumerate(keywords):
        original, candidate = normalize(phrase), _ocr_compact(phrase)
        if not 4 <= len(candidate) <= 80:
            continue
        positions.setdefault(candidate, index)
        mode = modes.get(original, "contains")
        if mode == "word":
            word_rules.append((original, original, candidate, mode, False))
        else:
            dense_rules.append((candidate, candidate, candidate, mode, False))
    return KeywordMatcher(dense_rules), KeywordMatcher(word_rules), positions


def compile_ocr_words(keywords, modes=None):
    return _ocr_rules(tuple(keywords), tuple(sorted((normalize(k), v) for k, v in (modes or {}).items())))


def matched_words(text, confidence, keywords, threshold=65, *, exceptions=(), modes=None,
                  excluded=frozenset(), compiled=None):
    """Return normalized phrases only for high-confidence >=4-character matches."""
    if not text or not math.isfinite(confidence) or confidence < threshold:
        return []
    normalized_text = _ocr_compact(text)
    exempt = set()
    for phrase, context in word_waivers(exceptions):
        if normalized_text == _ocr_compact(context):
            exempt.add(_ocr_compact(phrase))
    dense, words, positions = compiled if compiled is not None else compile_ocr_words(keywords, modes)
    hits = set()
    for matcher, content, compacted in ((dense, normalized_text, True), (words, normalize(text), False)):
        blocked = {_ocr_compact(w) if compacted else normalize(w) for w in excluded}
        blocked.update(exempt if compacted else {r[1] for r in words.rules if r[2] in exempt})
        for _ in range(4):
            word = matcher.find(content, excluded=blocked)
            if word is None:
                break
            hits.add(_ocr_compact(word))
            blocked.add(word)
    return sorted(hits, key=positions.__getitem__)[:4]


class OCRWorker:
    def __init__(self, api, *, enabled=False, extractor=None, max_pending=8):
        self.api = api
        self.available = bool(enabled and (extractor is not None or shutil.which("tesseract")))
        self.extractor = extractor or extract_tesseract
        self._capacity = BoundedSemaphore(max_pending)
        self._ready = Queue()
        self._executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="groupguard-ocr")
            if self.available else None)

    def submit(self, file_id, context):
        if not self.available or not self._capacity.acquire(blocking=False):
            return False

        def work():
            try:
                data = self.api.fetch_photo(file_id, max_bytes=MAX_OCR_FILE_BYTES)
                return self.extractor(data)
            except TelegramAPIError:
                return ("", 0.0)
            except Exception:
                LOG.warning("OCR 处理失败，跳过本图片")
                return ("", 0.0)

        try:
            future = self._executor.submit(work)
        except RuntimeError:
            self._capacity.release()
            return False

        def complete(fut):
            try:
                text, confidence = fut.result()
                self._ready.put((dict(context), str(text)[:4000], float(confidence)))
            except Exception:
                self._ready.put((dict(context), "", 0.0))
            finally:
                self._capacity.release()

        future.add_done_callback(complete)
        return True

    def results(self, limit=10):
        for _ in range(limit):
            try:
                yield self._ready.get_nowait()
            except Empty:
                return

    def shutdown(self):
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
