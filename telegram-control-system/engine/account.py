"""
═══════════════════════════════════════════════════════════════
  TELETHON ACCOUNT ENGINE — Single Account Handler
═══════════════════════════════════════════════════════════════
"""

import asyncio
import logging
import datetime
from typing import Optional, Dict, Any, List, Tuple

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
from telethon.tl.types import (
    ChannelParticipant, ChannelParticipantSelf,
    ChannelParticipantAdmin, ChannelParticipantCreator,
)
from telethon.errors import (
    FloodWaitError, UserAlreadyParticipantError,
    AuthKeyDuplicatedError, SessionRevokedError,
)

from shared.models import AccountStatus
from engine.utils import TextUtils, safe_send, safe_forward, MessageFormatter

logger = logging.getLogger(__name__)


class AccountEngine:
    """Manages a single Telegram account via Telethon"""

    def __init__(self, phone: str, api_id: int, api_hash: str,
                 target_group_id: int, mode: str = "both",
                 session_string: Optional[str] = None):
        self.phone = phone
        self.api_id = api_id
        self.api_hash = api_hash
        self.target_group_id = target_group_id
        self.mode = mode.lower()
        self.session_string = session_string

        self.client: Optional[TelegramClient] = None
        self.me = None
        self.status = AccountStatus.PENDING
        self.last_error: Optional[str] = None
        self.connected_at: Optional[datetime.datetime] = None
        self.message_count = 0
        self.reply_count = 0

        # Filters config (loaded dynamically)
        self.filters: Dict[str, Any] = {}
        self.keywords: List[str] = []
        self.excluded_groups: set = set()
        self.blocked_users: Dict[int, Tuple[str, str]] = {}

        # Trigger phrases
        self.direct_triggers: List[str] = []
        self.blocked_phrases: List[str] = []
        self.auto_replies: List[str] = []
        self._auto_reply_index = 0

        # Handlers
        self._message_handler = None

        # Forward caches (shared externally)
        self.forward_cache = None
        self.reply_cache = None

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected()

    @property
    def display_name(self) -> str:
        if self.me:
            fn = getattr(self.me, 'first_name', '') or ''
            ln = getattr(self.me, 'last_name', '') or ''
            return f"{fn} {ln}".strip() or self.phone
        return self.phone

    def _next_auto_reply(self, default: str) -> str:
        if not self.auto_replies:
            return default
        msg = self.auto_replies[self._auto_reply_index]
        self._auto_reply_index = (self._auto_reply_index + 1) % len(self.auto_replies)
        return msg

    async def connect(self) -> bool:
        """Connect the account"""
        try:
            session = StringSession(self.session_string) if self.session_string else StringSession()
            self.client = TelegramClient(session, self.api_id, self.api_hash)
            await self.client.connect()

            if not await self.client.is_user_authorized():
                self.status = AccountStatus.PENDING
                self.last_error = "Session not authorized"
                return False

            self.me = await self.client.get_me()
            self.status = AccountStatus.ACTIVE
            self.connected_at = datetime.datetime.utcnow()
            self.last_error = None

            # Setup message handler
            self._setup_handlers()

            logger.info(f"Account {self.phone} connected as {self.display_name}")
            return True

        except AuthKeyDuplicatedError:
            self.status = AccountStatus.BANNED
            self.last_error = "Auth key duplicated"
            logger.error(f"Account {self.phone}: Auth key duplicated")
            return False
        except SessionRevokedError:
            self.status = AccountStatus.ERROR
            self.last_error = "Session revoked"
            logger.error(f"Account {self.phone}: Session revoked")
            return False
        except Exception as e:
            self.status = AccountStatus.ERROR
            self.last_error = str(e)
            logger.error(f"Account {self.phone} connection error: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect the account"""
        try:
            if self.client:
                await self.client.disconnect()
                self.client = None
            self.status = AccountStatus.PAUSED
            logger.info(f"Account {self.phone} disconnected")
        except Exception as e:
            logger.error(f"Error disconnecting {self.phone}: {e}")

    def _setup_handlers(self) -> None:
        """Setup message event handlers"""
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(incoming=True),
        )

    async def _on_new_message(self, event) -> None:
        """Handle new incoming messages"""
        try:
            # Skip if not in correct mode
            if self.mode == "reply" and not self.reply_cache:
                return

            text = event.message.message or ""
            chat_id = event.chat_id
            sender_id = event.message.sender_id

            # Skip excluded groups
            if chat_id in self.excluded_groups:
                return

            # Apply filters
            if await self._should_ignore(event, text):
                return

            # Check keywords for forwarding
            if self.forward_cache and self.mode in ("forward", "both"):
                await self._handle_forward(event, text, chat_id)

            # Check for auto-reply
            if self.reply_cache and self.mode in ("reply", "both"):
                await self._handle_reply(event, text, chat_id, sender_id)

        except Exception as e:
            logger.error(f"Message handling error for {self.phone}: {e}")

    async def _should_ignore(self, event, text: str) -> bool:
        """Check if message should be ignored based on filters"""
        filters = self.filters

        # Outgoing filter
        if filters.get("filter_outgoing", True) and event.out:
            return True

        # Private chat filter
        if filters.get("filter_private", True) and event.is_private:
            return True

        # Bot filter
        sender = event.message.sender
        if filters.get("filter_bots", True) and getattr(sender, "bot", False):
            return True

        # Admin filter
        if filters.get("filter_admins", True):
            try:
                participant = await event.client.get_participant(event.chat_id, event.message.sender_id)
                if isinstance(participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                    return True
            except Exception:
                pass

        # Mention filter
        if filters.get("filter_mention", True) and TextUtils.has_mention(text):
            return True

        # Link filter
        if filters.get("filter_links", True) and TextUtils.has_url(text):
            return True

        # Digit filter
        if filters.get("filter_digits", True) and TextUtils.has_digits(text):
            return True

        # Word count filter
        word_limit = filters.get("word_count_limit", 17)
        if TextUtils.count_words(text) > word_limit:
            return True

        return False

    async def _handle_forward(self, event, text: str, chat_id: int) -> None:
        """Handle message forwarding"""
        # Check keywords
        if not self.keywords:
            return

        # Build keyword regex
        import re
        kw_pattern = "|".join(map(re.escape, self.keywords))
        if not re.search(kw_pattern, text, re.IGNORECASE):
            return

        # Dedup check
        dedupe_key = ("msg", chat_id, event.message.id)
        key_hash = TextUtils.make_dedupe_key(dedupe_key)

        if await self.forward_cache.contains(key_hash):
            return

        await self.forward_cache.add(key_hash)

        # Format and forward
        try:
            formatted = await MessageFormatter.format_forward(event)
            await safe_send(self.client, self.target_group_id, formatted)
            self.message_count += 1
        except Exception as e:
            logger.error(f"Forward error: {e}")

    async def _handle_reply(self, event, text: str, chat_id: int, sender_id: int) -> None:
        """Handle auto-reply"""
        # Check if sender is blocked
        if sender_id in self.blocked_users:
            return

        # Check keywords match
        import re
        if not self.keywords:
            return
        kw_pattern = "|".join(map(re.escape, self.keywords))
        if not re.search(kw_pattern, text, re.IGNORECASE):
            return

        # Check direct trigger
        if not any(TextUtils.fuzzy_match(text, trigger) for trigger in self.direct_triggers):
            return

        # Check blocked phrase
        norm_text = TextUtils.normalize(text)
        blocked_norms = {TextUtils.normalize(p) for p in self.blocked_phrases}
        if norm_text in blocked_norms:
            return

        # Dedup check
        dedupe_key = ("reply", chat_id, event.message.id)
        key_hash = TextUtils.make_dedupe_key(dedupe_key)

        if await self.reply_cache.contains(key_hash):
            return

        await self.reply_cache.add(key_hash)

        # Send reply
        try:
            sender = await event.get_sender()
            default_reply = self._next_auto_reply(
                "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك"
            )
            reply_text = TextUtils.format_template(default_reply, sender)

            await safe_send(self.client, sender_id, reply_text)
            self.reply_count += 1

        except Exception as e:
            logger.error(f"Reply error: {e}")

    async def join_group(self, group_link: str) -> Tuple[bool, str]:
        """Join a group/channel"""
        try:
            entity = await self.client.get_entity(group_link)
            await self.client(JoinChannelRequest(entity))
            return True, f"✅ انضمام ناجح: {group_link}"
        except UserAlreadyParticipantError:
            return True, f"ℹ️ عضو مسبقاً: {group_link}"
        except FloodWaitError as e:
            return False, f"⏸️ FloodWait {e.seconds}s"
        except Exception as e:
            return False, f"❌ خطأ: {str(e)[:100]}"

    async def check_group_membership(self, group_link: str) -> Tuple[bool, str]:
        """Check if account is member of a group"""
        try:
            entity = await self.client.get_entity(group_link)
            me = await self.client.get_me()
            result = await self.client(GetParticipantRequest(entity, me.id))
            participant = result.participant
            is_member = isinstance(participant, (
                ChannelParticipant, ChannelParticipantSelf,
                ChannelParticipantAdmin, ChannelParticipantCreator,
            ))
            return is_member, "عضو" if is_member else "ليس عضو"
        except Exception as e:
            return False, str(e)[:100]

    async def get_session_string(self) -> Optional[str]:
        """Get current session string"""
        if self.client and self.client.session:
            try:
                return StringSession.save(self.client.session)
            except Exception:
                return None
        return self.session_string

    def to_dict(self) -> dict:
        return {
            "phone": self.phone,
            "display_name": self.display_name,
            "status": self.status.value,
            "mode": self.mode,
            "target_group_id": self.target_group_id,
            "is_connected": self.is_connected,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_error": self.last_error,
            "message_count": self.message_count,
            "reply_count": self.reply_count,
        }
