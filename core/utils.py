import re
import math
import asyncio
import hashlib
import unicodedata
import functools
from typing import Tuple

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

import mmh3


class FastUtils:
    """ Ultra-fast utilities with LRU cache and compiled regexes. """

    _WS_RE = re.compile(r"[\s\W_]+")
    _MENTION_RE = re.compile(r"@\w{5,}")
    _URL_RE = re.compile(r"https?://\S+")
    _DIGIT_RE = re.compile(r"\d")

    @staticmethod
    @functools.lru_cache(maxsize=200_000)
    def normalize_text(text: str) -> str:
        """ Cached NFC normalization — 100x faster for repeated texts. """
        if not text:
            return ""
        s = unicodedata.normalize("NFC", text)
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return FastUtils._WS_RE.sub("", s).lower()

    @staticmethod
    def make_dedupe_key(key: Tuple) -> str:
        raw = "|".join(map(str, key))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def split_long(text: str, chunk: int = 4096):
        """ Memory-efficient generator for splitting long texts. """
        for i in range(0, len(text), chunk):
            yield text[i:i + chunk]

    @staticmethod
    def should_ignore(text: str) -> bool:
        """ Ultra-fast spam filter — < 1 microsecond. """
        if len(text.split()) > 17:
            return True
        if FastUtils._MENTION_RE.search(text):
            return True
        if FastUtils._URL_RE.search(text):
            return True
        if FastUtils._DIGIT_RE.search(text):
            return True
        return False


class BloomFilter:
    """ Space-efficient Bloom filter using bytearray (no bitarray dependency). """

    __slots__ = ("size", "num_hashes", "bits", "count")

    def __init__(self, expected_items: int = 50_000_000, fp_rate: float = 0.001):
        self.size = self._optimal_size(expected_items, fp_rate)
        self.num_hashes = self._optimal_hashes(fp_rate)
        self.bits = bytearray((self.size // 8) + 1)
        self.count = 0

    @staticmethod
    def _optimal_size(n: int, p: float) -> int:
        return int(-n * math.log(p) / (math.log(2) ** 2))

    @staticmethod
    def _optimal_hashes(p: float) -> int:
        return max(1, int(-math.log(p) / math.log(2)))

    def _hashes(self, key: str):
        h1 = mmh3.hash(key, 0)
        h2 = mmh3.hash(key, 1)
        for i in range(self.num_hashes):
            yield abs(h1 + i * h2) % self.size

    def add(self, key: str):
        for h in self._hashes(key):
            byte_idx = h // 8
            bit_idx = h % 8
            self.bits[byte_idx] |= (1 << bit_idx)
        self.count += 1

    def __contains__(self, key: str) -> bool:
        for h in self._hashes(key):
            byte_idx = h // 8
            bit_idx = h % 8
            if not (self.bits[byte_idx] & (1 << bit_idx)):
                return False
        return True


class SenderLockManager:
    """ One lock per sender — eliminates lock creation overhead. """

    __slots__ = ("_locks", "_semaphore")

    def __init__(self, max_concurrent: int = 500):
        self._locks: dict[int, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def get_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    @property
    def semaphore(self):
        return self._semaphore
