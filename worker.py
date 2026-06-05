"""
═══════════════════════════════════════════════════════════════════════════════
Account Worker — Telethon-based user accounts for monitoring & auto-reply
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    UserIsBlockedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.types import (
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantSelf,
    ChannelParticipant,
)

from database import Database
from models import AccountStatus
from utils import TextUtils, Messenger, MessageFormatter, FallbackRouter

logger = logging.getLogger("telegram-bot")


class AccountWorker:
    """A single Telethon account that monitors groups and auto-replies."""

    def __init__(
        self,
        db: Database,
        state: Any,
        phone: str,
        api_id: int,
        api_hash: str,
        target_group_id: int,
        mode: str = "both",
        session_string: Optional[str] = None,
        logger_override: Optional[logging.Logger] = None,
    ) -> None:
        self.db = db
        self.state = state
        self.phone = phone
        self.api_id = api_id
        self.api_hash = api_hash
        self.target_group_id = target_group_id
        self.mode = mode.lower()
        self.session_string = session_string
        self.logger = logger_override or logger

        self.client = TelegramClient(
            StringSession(session_string) if session_string else StringSession(),
            api_id,
            api_hash,
        )

        self.messenger = Messenger(self.logger)
        self.formatter = MessageFormatter(state.fallback_group_id if hasattr(state, "fallback_group_id") else -1002353780992)
        self.fallback = FallbackRouter(db, state, self.messenger, self.formatter)

        # Register handlers
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_message, events.NewMessage(outgoing=True))

    async def connect(self) -> bool:
        """Connect and verify authorization."""
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                self.logger.warning(f"{self.phone}: Session not authorized")
                return False
            me = await self.client.get_me()
            self.logger.info(f"{self.phone}: Connected as {me.first_name} (ID: {me.id})")
            await self.db.update_account_status(self.phone, AccountStatus.ACTIVE)
            return True
        except Exception as e:
            self.logger.error(f"{self.phone}: Connection failed: {e}")
            await self.db.update_account_status(self.phone, AccountStatus.ERROR, str(e))
            return False

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
            await self.db.update_account_status(self.phone, AccountStatus.PAUSED)
            self.logger.info(f"{self.phone}: Disconnected")
        except Exception as e:
            self.logger.error(f"{self.phone}: Disconnect error: {e}")

    def is_connected(self) -> bool:
        return self.client.is_connected()

    # ─── Message Handling ───

    async def _on_message(self, ev: events.NewMessage.Event) -> None:
        """Main message handler — applies filters and dispatches."""
        chat_id = ev.chat_id
        text = ev.message.message or ""

        # Quick reject: empty or system messages
        if not text or text.startswith("✉"):
            return

        # Load dynamic filters from DB
        filters = await self.db.get_filters()

        # Apply filters
        if filters.get("private", (True, None))[0] and ev.is_private:
            return
        if filters.get("outgoing", (True, None))[0] and ev.out:
            return

        # Check excluded groups
        excluded = await self.db.get_excluded_groups()
        if chat_id in excluded:
            return

        if filters.get("mention", (True, None))[0] and re.search(r"@\w{5,}", text):
            return
        if filters.get("links", (True, None))[0] and re.search(r"https?://\S+", text):
            return

        word_limit = filters.get("word_count", (True, 17))[1] or 17
        if len(text.split()) > word_limit:
            return

        if filters.get("digits", (True, None))[0] and re.search(r"\d", text):
            return

        sender = ev.message.sender
        sender_id = ev.message.sender_id
        if filters.get("bots", (True, None))[0] and getattr(sender, "bot", False):
            return

        if filters.get("admins", (True, None))[0]:
            try:
                part = await ev.client.get_participant(chat_id, sender_id)
                if isinstance(part, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                    return
            except Exception:
                pass

        # Check blocked users
        if sender_id in self.state.blocked_users:
            return

        # Dispatch
        await self._dispatch(ev)

    async def _dispatch(self, ev: events.NewMessage.Event) -> None:
        """Core dispatch logic — keyword matching, forwarding, auto-reply."""
        async with self.state.dispatch_semaphore:
            key = self.formatter.message_key(ev)
            text = ev.message.message or ""

            # Get keywords
            keywords = await self.db.get_keywords()
            if not keywords:
                return
            kw_pattern = "|".join(map(re.escape, keywords))
            kw_re = re.compile(kw_pattern, re.IGNORECASE)

            # Forward matching messages
            if kw_re.search(text) and key not in self.state.FORWARD_DONE:
                self.state.FORWARD_DONE.add(key)
                fwd_txt = await self.formatter.build_forward_text(ev)
                await asyncio.gather(*[
                    self.messenger.safe_send(b.client if hasattr(b, "client") else b, b.target_group_id, fwd_txt, tag=f"FWD⌁{b.phone}")
                    for b in self.state.bots if b.mode in ("forward", "both")
                ], return_exceptions=True)

            # Find source bot
            src_bot = next((b for b in self.state.bots if b.client is ev.client), None)
            if not src_bot:
                return

            # Check direct triggers
            direct_triggers = self.state.direct_triggers
            blocked_phrases = self.state.blocked_phrases

            wants_reply = (
                kw_re.search(text)
                and any(TextUtils.fuzzy_match(text, trg) for trg in direct_triggers)
                and TextUtils.normalize_text(text) not in {TextUtils.normalize_text(p) for p in blocked_phrases}
            )
            if not wants_reply:
                return

            # Reply lock
            lock = self.state.get_reply_lock(key)
            async with lock:
                if key in self.state.REPLY_DONE:
                    return

                # Rate limit check
                sender = await ev.get_sender()
                sender_id = getattr(sender, "id", 0)
                rate_limit_max = int(await self.db.get_setting("rate_limit_max", "4"))

                try:
                    prev = await self.db.count_auto_replies_distinct(sender_id, hours=24)
                    if prev >= rate_limit_max:
                        uname = getattr(sender, "username", "") or ""
                        disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name', '') or '').strip()}".strip()
                        await self.db.add_blocked_user(sender_id, uname, disp_name)
                        self.state.blocked_users[sender_id] = (uname, disp_name)
                        self.state.REPLY_DONE.add(key)
                        return
                except Exception as ex:
                    self.logger.warning(f"Rate limit check failed: {ex}")

                # Log pending
                dedupe_key = TextUtils.make_dedupe_key(key)
                pending_log_id = 0
                try:
                    uname = getattr(sender, "username", "") or ""
                    disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name', '') or '').strip()}".strip()
                    pending_log_id = await self.db.log_auto_reply_pending(
                        sender_id, uname, disp_name, dedupe_key, ev.chat_id, ev.message.id
                    )
                except Exception as ex:
                    self.logger.warning(f"Log pending failed: {ex}")

                self.state.REPLY_DONE.add(key)

                # Forward to fallback
                await self.fallback.forward_any(ev)

                # Send auto-reply
                any_ok = False
                for b in self.state.bots:
                    if b.mode not in ("reply", "both"):
                        continue
                    try:
                        tgt = sender if b.client is ev.client else await b.client.get_entity(sender.id)
                    except Exception:
                        tgt = getattr(sender, "id", None)
                    if not tgt:
                        continue

                    sent_orig = await self.messenger.safe_send(b.client, tgt, text, tag=f"ORIG⌁{b.phone}")
                    if not sent_orig:
                        continue

                    default_reply = await self.db.get_setting("default_auto_reply", "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك")
                    raw_reply = self.state.next_auto_reply(default_reply)
                    formatted_reply = TextUtils.format_auto_reply(raw_reply, sender)
                    sent_auto = await self.messenger.safe_send(b.client, tgt, formatted_reply, tag=f"AUTO⌁{b.phone}")

                    if sent_auto:
                        try:
                            await self.db.update_auto_reply_log(
                                pending_log_id, bot_phone=b.phone, message_id=getattr(sent_auto, "id", None)
                            )
                        except Exception as ex:
                            self.logger.warning(f"Update log failed: {ex}")
                        any_ok = True
                        break

                if not any_ok:
                    warn = "⚠️ لم يتم الرد تلقائيًا — سيتم المتابعة يدويًا:"
                    await self.fallback.forward_any(ev, warn_prefix=warn)

    # ─── Group Management ───

    async def join_sleep(self, base_delay: int = 30) -> None:
        delay = base_delay + random.randint(0, 30)
        for _ in range(delay):
            if self.state.stop_joining_flags.get(self.phone):
                break
            await asyncio.sleep(1)

    async def join_groups_with_account(self, start_index: int = 0) -> Tuple[int, int]:
        """Join all stored groups starting from index. Returns (joined, total)."""
        if self.state.circuit_breaker.is_open(self.phone):
            self.logger.warning(f"{self.phone}: Circuit breaker open")
            return 0, 0

        links = await self.db.get_all_groups()
        joined = 0
        total = len(links)
        self.logger.info(f"{self.phone}: Joining {total} groups from {start_index + 1}")

        for idx, link in enumerate(links[start_index:], start=start_index):
            if self.state.stop_joining_flags.get(self.phone):
                self.logger.info(f"{self.phone}: Join stopped at {idx + 1}")
                break
            try:
                entity = await self.client.get_entity(link)
                await self.client(JoinChannelRequest(entity))
                joined += 1
                self.state.circuit_breaker.record_success(self.phone)
                self.logger.info(f"{self.phone}: Joined [{idx + 1}/{total}] {link}")
            except UserAlreadyParticipantError:
                self.logger.info(f"{self.phone}: Already in [{idx + 1}/{total}] {link}")
            except FloodWaitError as e:
                self.state.circuit_breaker.record_failure(self.phone)
                self.logger.warning(f"{self.phone}: FloodWait {e.seconds}s at [{idx + 1}/{total}]")
                await self.join_sleep(e.seconds + 2)
                if self.state.stop_joining_flags.get(self.phone):
                    break
            except Exception as ex:
                self.state.circuit_breaker.record_failure(self.phone)
                self.logger.error(f"{self.phone}: Failed [{idx + 1}/{total}] {link}: {ex}")

        self.logger.info(f"{self.phone}: Join complete: {joined}/{total}")
        return joined, total

    async def user_groups_status(self) -> Tuple[List[str], List[str]]:
        """Check which stored groups the account is in."""
        links = await self.db.get_all_groups()
        in_groups, not_in = [], []
        me = await self.client.get_me()
        for link in links:
            try:
                entity = await self.client.get_entity(link)
                result = await self.client(GetParticipantRequest(entity, me.id))
                participant = result.participant
                if isinstance(participant, (ChannelParticipant, ChannelParticipantSelf, ChannelParticipantAdmin, ChannelParticipantCreator)):
                    in_groups.append(link)
                else:
                    not_in.append(link)
            except Exception:
                not_in.append(link)
        return in_groups, not_in

    async def unblock_spambot(self) -> None:
        """Send /start to @SpamBot."""
        try:
            async with self.client.conversation("@SpamBot") as conv:
                await conv.send_message("/start")
                await conv.get_response(timeout=10)
        except Exception:
            pass
