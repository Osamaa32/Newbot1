"""
═══════════════════════════════════════════════════════════════════════════════
Utilities — Text processing, Caching, Rate Limiting, Circuit Breaker, Formatting
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz
from telethon import TelegramClient, types
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import (
    ChannelParticipant,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantSelf,
    InputPeerUser,
    User,
)

logger = logging.getLogger("telegram-bot")


# ─── Text Utilities ───

class TextUtils:
    LINK_RE = re.compile(r"(https://t\.me/(?:c/)?(?:\d+|[A-Za-z0-9_]+)/?\d*)(?:\?comment=\d+)?")

    @staticmethod
    def normalize_text(text: str) -> str:
        s = unicodedata.normalize("NFD", text)
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return re.sub(r"[\s\W_]+", "", s).lower()

    @staticmethod
    def fuzzy_match(a: str, b: str, threshold: int = 80) -> bool:
        return fuzz.ratio(TextUtils.normalize_text(a), TextUtils.normalize_text(b)) >= threshold

    @staticmethod
    def split_long(text: str, chunk: int = 4000) -> List[str]:
        return [text[i : i + chunk] for i in range(0, len(text), chunk)]

    @staticmethod
    def make_dedupe_key(key: Tuple[Any, ...]) -> str:
        return hashlib.sha1("|".join(map(str, key)).encode("utf-8")).hexdigest()

    @staticmethod
    def format_auto_reply(template: str, user: Any) -> str:
        return (
            template
            .replace("{first_name}", getattr(user, "first_name", "") or "")
            .replace("{last_name}", getattr(user, "last_name", "") or "")
            .replace("{username}", getattr(user, "username", "") or "")
            .replace("{user_id}", str(getattr(user, "id", "") or ""))
        )

    @staticmethod
    def escape_md(text: str) -> str:
        escape_chars = r"_\*\[\]\(\)~`>\#\+\-=\|\{\}\.\!"
        return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", str(text))


# ─── TTL Cache ───

class TTLCacheSet:
    def __init__(self, ttl_seconds: int = 7200):
        self.ttl = ttl_seconds
        self._data: OrderedDict = OrderedDict()
        self._access_count = 0

    def add(self, key: Any) -> None:
        now = time.time()
        self._evict_expired(now)
        self._data[key] = now + self.ttl
        self._data.move_to_end(key)
        self._access_count += 1

    def __contains__(self, key: Any) -> bool:
        self._evict_expired(time.time())
        return key in self._data

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


# ─── Circuit Breaker ───

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 1800):
        self.failures: Dict[str, int] = {}
        self.last_failure: Dict[str, float] = {}
        self.threshold = failure_threshold
        self.timeout = recovery_timeout

    def is_open(self, phone: str) -> bool:
        if self.failures.get(phone, 0) < self.threshold:
            return False
        last = self.last_failure.get(phone, 0)
        if time.time() - last > self.timeout:
            self.failures[phone] = 0
            return False
        return True

    def record_failure(self, phone: str) -> None:
        self.failures[phone] = self.failures.get(phone, 0) + 1
        self.last_failure[phone] = time.time()

    def record_success(self, phone: str) -> None:
        if phone in self.failures:
            self.failures[phone] = max(0, self.failures[phone] - 1)


# ─── Rate Limiter ───

class RateLimiter:
    def __init__(self, max_requests: int = 4, window: int = 3600):
        self.max_requests = max_requests
        self.window = window
        self.buckets: Dict[int, List[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        timestamps = self.buckets.get(user_id, [])
        timestamps = [t for t in timestamps if now - t < self.window]
        self.buckets[user_id] = timestamps
        if len(timestamps) >= self.max_requests:
            return False
        timestamps.append(now)
        return True


# ─── Messenger ───

class Messenger:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    async def safe_send(self, client: TelegramClient, dst: Any, text: str,
                        tag: str = "SEND", parse_mode: str = "Markdown") -> Optional[types.Message]:
        if len(text) > 4000:
            last = None
            for part in TextUtils.split_long(text, 4000):
                last = await self.safe_send(client, dst, part, tag=tag, parse_mode=parse_mode)
            return last

        from telethon.errors import FloodWaitError, MessageTooLongError, UserIsBlockedError

        delay = 1
        for attempt in range(3):
            try:
                return await client.send_message(dst, text, parse_mode=parse_mode, link_preview=False)
            except FloodWaitError as e:
                self.logger.warning(f"{tag} FloodWait {e.seconds}s (attempt {attempt + 1})")
                await asyncio.sleep(min(delay, e.seconds))
                delay *= 2
            except MessageTooLongError:
                for part in TextUtils.split_long(text, 4000):
                    await client.send_message(dst, part, parse_mode=parse_mode, link_preview=False)
                return None
            except UserIsBlockedError:
                self.logger.warning(f"{tag} blocked by {dst}")
                return None
            except Exception as ex:
                self.logger.error(f"{tag} error: {ex}", exc_info=True)
                return None
        return None


# ─── Message Formatter ───

class MessageFormatter:
    def __init__(self, fallback_group_id: int) -> None:
        self.fallback_group_id = fallback_group_id

    def message_key(self, ev: Any) -> Tuple[Any, ...]:
        text = ev.message.message or ""
        m = TextUtils.LINK_RE.search(text)
        return ("link", m.group(1)) if m else ("id", ev.chat_id, ev.message.id)

    async def build_forward_text(self, ev: Any) -> str:
        from telethon import events
        text = ev.message.message or "—"
        sender = await ev.get_sender()
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
        else:
            sender_line = "👤 **المرسل:** غير معروف"
            dm_line = "🔗 **مراسلة مباشرة:** غير متاحة"

        chat = ev.chat or await ev.get_chat()
        if chat:
            chat_username = getattr(chat, "username", None)
            chat_title = getattr(chat, "title", None)
            if chat_username:
                group_line = f"📍 **المجموعة:** @{chat_username}"
            elif chat_title:
                group_line = f"📍 **المجموعة:** {chat_title}"
            elif chat.id == ev.peer_id:
                group_line = "📍 **المحادثة:** خاصة"
            else:
                group_line = "📍 **المجموعة:** غير معروفة"
        else:
            group_line = "📍 **المجموعة:** غير معروفة"

        link_line = "📜 **الرابط:** غير متاح"
        if ev.chat_id and chat:
            chat_username_for_link = getattr(chat, "username", None)
            if str(ev.chat_id).startswith("-100"):
                channel_id_raw = str(ev.chat_id)[4:]
                link_line = f"📜 **الرابط:** [اضغط هنا](https://t.me/c/{channel_id_raw}/{ev.message.id})"
            elif chat_username_for_link:
                link_line = f"📜 **الرابط:** [اضغط هنا](https://t.me/{chat_username_for_link}/{ev.message.id})"

        return f"`{text}`\n\n{sender_line}\n{dm_line}\n{group_line}\n{link_line}"


# ─── Fallback Router ───

class FallbackRouter:
    def __init__(self, db: Any, state: Any, messenger: Messenger, formatter: MessageFormatter) -> None:
        self.db = db
        self.state = state
        self.messenger = messenger
        self.formatter = formatter
        self._entity_cache: Dict[int, Any] = {}
        self._member_cache: Dict[int, bool] = {}

    async def _get_fallback_entity(self, client: TelegramClient):
        from utils import TextUtils
        cid = id(client)
        if cid in self._entity_cache:
            return self._entity_cache[cid]
        try:
            fallback_id = int(await self.db.get_setting("fallback_group_id", "-1002353780992"))
            entity = await client.get_entity(fallback_id)
            self._entity_cache[cid] = entity
            return entity
        except Exception:
            return None

    async def _is_member_of_fallback(self, client: TelegramClient) -> bool:
        cid = id(client)
        if cid in self._member_cache:
            return self._member_cache[cid]
        try:
            me = await client.get_me()
            entity = await self._get_fallback_entity(client)
            if not entity:
                self._member_cache[cid] = False
                return False
            res = await client(GetParticipantRequest(entity, me.id))
            is_in = isinstance(res.participant, (ChannelParticipant, ChannelParticipantSelf, ChannelParticipantAdmin, ChannelParticipantCreator))
            self._member_cache[cid] = is_in
            return is_in
        except Exception:
            self._member_cache[cid] = False
            return False

    async def _try_forward_normal(self, client: TelegramClient, ev: Any) -> bool:
        if not await self._is_member_of_fallback(client):
            return False
        try:
            fallback_id = int(await self.db.get_setting("fallback_group_id", "-1002353780992"))
            await client.forward_messages(fallback_id, ev.message)
            return True
        except Exception:
            return False

    async def _try_forward_textual(self, client: TelegramClient, ev: Any, prefix: str = "") -> bool:
        if not await self._is_member_of_fallback(client):
            return False
        try:
            fallback_id = int(await self.db.get_setting("fallback_group_id", "-1002353780992"))
            fwd_txt = await self.formatter.build_forward_text(ev)
            if prefix:
                fwd_txt = f"{prefix}\n\n{fwd_txt}"
            await self.messenger.safe_send(client, fallback_id, fwd_txt, tag="FALLBACK_TXT")
            return True
        except Exception:
            return False

    async def forward_any(self, ev: Any, warn_prefix: str = "") -> None:
        src_bot = next((b for b in self.state.bots if b.client is ev.client), None)
        if not src_bot:
            return
        if await self._try_forward_normal(src_bot.client, ev):
            return
        for b in self.state.bots:
            if b.client is src_bot.client:
                continue
            if await self._try_forward_normal(b.client, ev):
                return
        if await self._try_forward_textual(src_bot.client, ev, prefix=warn_prefix):
            return
        for b in self.state.bots:
            if b.client is src_bot.client:
                continue
            if await self._try_forward_textual(b.client, ev, prefix=warn_prefix):
                return
