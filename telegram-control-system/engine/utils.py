"""
═══════════════════════════════════════════════════════════════
  ENGINE UTILITIES — Text Processing, Caching, Rate Limiting
═══════════════════════════════════════════════════════════════
"""

import re
import time
import hashlib
import unicodedata
import asyncio
import logging
from typing import Dict, List, Tuple, Any, Optional, Set
from collections import OrderedDict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


# ═════════════════ TEXT UTILITIES ═════════════════

class TextUtils:
    """Text processing utilities"""

    @staticmethod
    def normalize(text: str) -> str:
        """Normalize Arabic text for better matching"""
        s = unicodedata.normalize("NFD", text)
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return re.sub(r"[\s\W_]+", "", s).lower()

    @staticmethod
    def fuzzy_match(a: str, b: str, threshold: int = 80) -> bool:
        """Fuzzy string matching"""
        return fuzz.ratio(TextUtils.normalize(a), TextUtils.normalize(b)) >= threshold

    @staticmethod
    def split_long(text: str, chunk: int = 4000) -> List[str]:
        """Split long text into chunks"""
        return [text[i:i + chunk] for i in range(0, len(text), chunk)]

    @staticmethod
    def make_dedupe_key(key_parts: Tuple[Any, ...]) -> str:
        """Create SHA1 deduplication key"""
        return hashlib.sha1("|".join(map(str, key_parts)).encode("utf-8")).hexdigest()

    @staticmethod
    def format_template(template: str, user: Any) -> str:
        """Format template with user variables"""
        return (template
                .replace("{first_name}", getattr(user, 'first_name', '') or '')
                .replace("{last_name}", getattr(user, 'last_name', '') or '')
                .replace("{username}", getattr(user, 'username', '') or '')
                .replace("{user_id}", str(getattr(user, 'id', '') or '')))

    @staticmethod
    def count_words(text: str) -> int:
        return len(text.split())

    @staticmethod
    def has_url(text: str) -> bool:
        return bool(re.search(r"https?://\S+|t\.me/\S+|telegram\.me/\S+", text))

    @staticmethod
    def has_mention(text: str) -> bool:
        return bool(re.search(r"@\w{3,}", text))

    @staticmethod
    def has_digits(text: str) -> bool:
        return bool(re.search(r"\d", text))

    @staticmethod
    def extract_telegram_link(text: str) -> Optional[str]:
        match = re.search(r"(https://t\.me/(?:c/)?(?:\d+|[A-Za-z0-9_]+)/?\d*)", text)
        return match.group(1) if match else None


# ═════════════════ TTL CACHE ═════════════════

class TTLCache:
    """Thread-safe TTL cache with automatic eviction"""

    def __init__(self, ttl_seconds: int = 7200, max_size: int = 10000):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._data: OrderedDict = OrderedDict()
        self._access_count = 0
        self._lock = asyncio.Lock()

    async def add(self, key: Any) -> None:
        async with self._lock:
            now = time.time()
            self._evict_expired(now)
            if len(self._data) >= self.max_size:
                self._data.popitem(last=False)
            self._data[key] = now + self.ttl
            self._data.move_to_end(key)
            self._access_count += 1

    async def contains(self, key: Any) -> bool:
        async with self._lock:
            self._evict_expired(time.time())
            return key in self._data

    async def remove(self, key: Any) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    async def size(self) -> int:
        async with self._lock:
            self._evict_expired(time.time())
            return len(self._data)

    def _evict_expired(self, now: float) -> None:
        if self._access_count % 1000 == 0:
            expired = [k for k, v in self._data.items() if v < now]
            for k in expired:
                self._data.pop(k, None)
        else:
            while self._data:
                key, expiry = next(iter(self._data.items()))
                if expiry < now:
                    self._data.pop(key)
                else:
                    break


# ═════════════════ RATE LIMITER ═════════════════

class RateLimiter:
    """Per-user rate limiter with sliding window"""

    def __init__(self, max_requests: int = 4, window: int = 3600):
        self.max_requests = max_requests
        self.window = window
        self.buckets: Dict[int, List[float]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, user_id: int) -> bool:
        async with self._lock:
            now = time.time()
            timestamps = self.buckets.get(user_id, [])
            timestamps = [t for t in timestamps if now - t < self.window]
            self.buckets[user_id] = timestamps
            if len(timestamps) >= self.max_requests:
                return False
            timestamps.append(now)
            return True

    async def get_count(self, user_id: int) -> int:
        async with self._lock:
            now = time.time()
            timestamps = [t for t in self.buckets.get(user_id, []) if now - t < self.window]
            self.buckets[user_id] = timestamps
            return len(timestamps)

    async def reset(self, user_id: int) -> None:
        async with self._lock:
            self.buckets.pop(user_id, None)


# ═════════════════ CIRCUIT BREAKER ═════════════════

class CircuitBreaker:
    """Circuit breaker pattern for fault tolerance"""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 1800):
        self.failures: Dict[str, int] = {}
        self.last_failure: Dict[str, float] = {}
        self.threshold = failure_threshold
        self.timeout = recovery_timeout
        self._lock = asyncio.Lock()

    async def is_open(self, key: str) -> bool:
        async with self._lock:
            failures = self.failures.get(key, 0)
            if failures < self.threshold:
                return False
            last = self.last_failure.get(key, 0)
            if time.time() - last > self.timeout:
                self.failures[key] = max(0, failures - 1)
                return False
            return True

    async def record_failure(self, key: str) -> None:
        async with self._lock:
            self.failures[key] = self.failures.get(key, 0) + 1
            self.last_failure[key] = time.time()

    async def record_success(self, key: str) -> None:
        async with self._lock:
            if key in self.failures:
                self.failures[key] = max(0, self.failures[key] - 1)

    async def get_status(self, key: str) -> dict:
        async with self._lock:
            failures = self.failures.get(key, 0)
            last_fail = self.last_failure.get(key, 0)
            is_open = failures >= self.threshold and (time.time() - last_fail) <= self.timeout
            return {
                "failures": failures,
                "is_open": is_open,
                "last_failure": datetime.datetime.fromtimestamp(last_fail).isoformat() if last_fail else None,
                "threshold": self.threshold,
            }


import datetime  # noqa: E402


# ═════════════════ MESSAGE FORMATTER ═════════════════

class MessageFormatter:
    """Format messages for forwarding and display"""

    @staticmethod
    async def format_forward(event) -> str:
        """Format a message for forwarding"""
        text = event.message.message or "—"
        sender = await event.get_sender()

        if sender:
            username = getattr(sender, "username", None)
            fn = getattr(sender, "first_name", "") or ""
            ln = getattr(sender, "last_name", "") or ""
            disp = f"{fn} {ln}".strip() or f"مستخدم (ID: {sender.id})"

            if username:
                sender_line = f"👤 **المرسل:** [@{username}](https://t.me/{username})"
                if disp != f"مستخدم (ID: {sender.id})":
                    sender_line += f" ({disp})"
            else:
                sender_line = f"👤 **المرسل:** {disp}"

            dm_line = f"🔗 **مراسلة مباشرة:** [اضغط هنا](tg://user?id={sender.id})"
            sender_id = sender.id
        else:
            sender_line = "👤 **المرسل:** غير معروف"
            dm_line = "🔗 **مراسلة مباشرة:** غير متاحة"
            sender_id = None

        # Chat info
        chat = event.chat or await event.get_chat()
        if chat:
            chat_username = getattr(chat, "username", None)
            chat_title = getattr(chat, "title", None)
            if chat_username:
                group_line = f"📍 **المجموعة:** @{chat_username}"
            elif chat_title:
                group_line = f"📍 **المجموعة:** {chat_title}"
            else:
                group_line = "📍 **المحادثة:** خاصة"
        else:
            group_line = "📍 **المجموعة:** غير معروفة"

        # Message link
        link_line = "📜 **الرابط:** غير متاح"
        if event.chat_id and str(event.chat_id).startswith("-100"):
            channel_id_raw = str(event.chat_id)[4:]
            link_line = f"📜 **الرابط:** [اضغط هنا](https://t.me/c/{channel_id_raw}/{event.message.id})"
        elif chat and getattr(chat, "username", None):
            link_line = f"📜 **الرابط:** [اضغط هنا](https://t.me/{chat.username}/{event.message.id})"

        return (
            f"`{text}`\n\n"
            f"{sender_line}\n"
            f"{dm_line}\n"
            f"{group_line}\n"
            f"{link_line}"
        )

    @staticmethod
    def format_status_change(phone: str, status: str, emoji: str = "🔄") -> str:
        return f"{emoji} **{phone}** → `{status}`"

    @staticmethod
    def format_account_list(accounts: list) -> str:
        lines = ["📱 **قائمة الحسابات:**\n"]
        status_emojis = {
            "active": "🟢", "pending": "⏳", "paused": "⏸️",
            "error": "🔴", "banned": "🚫", "flood": "⏳", "connecting": "🔄",
        }
        for acc in accounts:
            emoji = status_emojis.get(acc.status, "⚪")
            lines.append(
                f"{emoji} **{acc.phone}** | `{acc.status}` | {acc.mode}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_stats(stats: dict) -> str:
        return (
            f"📊 **إحصائيات النظام**\n\n"
            f"📱 الحسابات: **{stats['accounts']['total']}** "
            f"(🟢 {stats['accounts'].get('active', 0)} نشط)\n"
            f"🔗 الجروبات: **{stats['groups']['total']}**\n"
            f"🔑 الكلمات: **{stats['keywords']['total']}**\n"
            f"🚫 المحظورون: **{stats['blocked_users']}**\n"
            f"📨 التوجيهات اليوم: **{stats['forwards_today']}**\n"
            f"📋 المهام: ⏳ {stats['tasks'].get('pending', 0)} | "
            f"🔄 {stats['tasks'].get('processing', 0)} | "
            f"✅ {stats['tasks'].get('completed', 0)}"
        )


# ═════════════════ SAFETY HELPERS ═════════════════

async def safe_send(client, destination, text: str,
                    buttons=None, parse_mode: str = "markdown",
                    max_retries: int = 3) -> Optional[Any]:
    """Safely send a message with retry logic"""
    from telethon.errors import FloodWaitError, MessageTooLongError, UserIsBlockedError

    if len(text) > 4000:
        parts = TextUtils.split_long(text, 4000)
        last = None
        for part in parts:
            last = await safe_send(client, destination, part, buttons=buttons, parse_mode=parse_mode)
            buttons = None
        return last

    delay = 1
    for attempt in range(max_retries):
        try:
            return await client.send_message(
                destination, text,
                parse_mode=parse_mode,
                buttons=buttons,
                link_preview=False,
            )
        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}s (attempt {attempt + 1})")
            await asyncio.sleep(min(delay, e.seconds))
            delay *= 2
        except MessageTooLongError:
            for part in TextUtils.split_long(text, 4000):
                await client.send_message(destination, part, parse_mode=parse_mode, link_preview=False)
            return None
        except UserIsBlockedError:
            logger.warning(f"User blocked: {destination}")
            return None
        except Exception as e:
            logger.error(f"Send error: {e}")
            return None
    return None


async def safe_forward(client, destination, message) -> Optional[Any]:
    """Safely forward a message"""
    from telethon.errors import FloodWaitError
    try:
        return await client.forward_messages(destination, message)
    except FloodWaitError as e:
        logger.warning(f"Forward FloodWait {e.seconds}s")
        await asyncio.sleep(min(e.seconds, 30))
        return None
    except Exception as e:
        logger.error(f"Forward error: {e}")
        return None
