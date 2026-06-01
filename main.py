import os
import asyncio
import logging

import telethon
from telethon import TelegramClient
from telethon.sessions import StringSession

from core.config import AppConfig
from core.db import PostgresDB
from core.utils import FastUtils, BloomFilter, SenderLockManager
from core.services import TriggerMatcher, MessageFormatter, GroupRepo
from core.accounts import FastMessenger, ManagedAccount, AccountManager, AccountWizard
from core.bot import SharedState, MessagePipeline, BotCommands


class BotManager:
    """ Central manager — runs admin account + all managed accounts. """

    def __init__(self):
        self.cfg = AppConfig()
        self.db = PostgresDB(self.cfg)
        self.state = SharedState()
        self.messenger = FastMessenger(self.cfg.logger)
        self.formatter = MessageFormatter(self.cfg)
        self.group_repo = GroupRepo(self.db)
        self.account_manager = AccountManager(
            self.cfg, self.db, self.state, self.messenger,
            self.formatter, self.group_repo, self.cfg.logger
        )
        self.account_wizard = AccountWizard(
            self.state, self.cfg, self.db, self.messenger, self.cfg.logger
        )
        self.commands = BotCommands(
            self.cfg, self.db, self.state, self.messenger,
            self.formatter, self.group_repo, self.account_manager,
            self.account_wizard, self.cfg.logger
        )
        self.pipeline = MessagePipeline(self._process_message, logger=self.cfg.logger)
        self.admin_client: TelegramClient = None

    async def start(self):
        # Initialize database
        await self.db.init()

        # Load core data
        self.state.direct_triggers = await self.db.load_table("direct_reply_messages")
        self.state.blocked_phrases = await self.db.load_table("blocked_reply_messages")
        self.state.auto_replies = await self.db.load_table("auto_reply_responses")
        self.state.blocked_users = await self.db.blocked_users_map()
        self.state.trigger_matcher.rebuild(
            self.state.direct_triggers, self.state.blocked_phrases
        )

        self.cfg.logger.info(
            f"Loaded: {len(self.state.direct_triggers)} triggers, "
            f"{len(self.state.blocked_phrases)} blocked, "
            f"{len(self.state.auto_replies)} auto-replies, "
            f"{len(self.state.blocked_users)} blocked users"
        )

        # Start pipeline
        self.pipeline.start(num_workers=4)

        # Connect admin account (from env — only needed for command interface)
        admin_connected = await self._connect_admin()
        if not admin_connected:
            self.cfg.logger.error("No admin account configured! Commands won't work.")
            # Continue anyway — managed accounts may still work

        # Load and start all managed accounts from DB
        await self.account_manager.load_and_start_all()

        # Register message handlers on all accounts
        for account in self.account_manager.get_all_accounts():
            self._register_handlers(account)

        self.cfg.logger.info(
            f"Bot running with {len(self.account_manager.get_all_accounts())} account(s)"
        )

        # Keep running
        if self.admin_client:
            await self.admin_client.run_until_disconnected()
        else:
            # Keep alive if no admin
            while True:
                await asyncio.sleep(3600)

    async def _connect_admin(self) -> bool:
        """ Connect admin account from environment variables. """
        api_id = os.getenv("ADMIN_API_ID") or os.getenv("TELEGRAM_API_ID_1")
        api_hash = os.getenv("ADMIN_API_HASH") or os.getenv("TELEGRAM_API_HASH_1")
        phone = os.getenv("ADMIN_PHONE") or os.getenv("TELEGRAM_PHONE_1")
        session_str = os.getenv("ADMIN_SESSION")

        if not all((api_id, api_hash, phone)):
            # Check if we have any account at all in DB
            accounts = await self.db.get_all_accounts()
            if accounts:
                # Use first account as admin
                acc = accounts[0]
                session = StringSession(acc.get("session_string") or "")
                self.admin_client = TelegramClient(session, acc["api_id"], acc["api_hash"])
                try:
                    await self.admin_client.connect()
                    if await self.admin_client.is_user_authorized():
                        me = await self.admin_client.get_me()
                        self.state.COMMAND_USER_ID = me.id
                        self.state.admin_client = self.admin_client
                        self.cfg.logger.info(f"Using DB account {acc['phone']} as admin")
                        self._register_command_handlers(self.admin_client)
                        return True
                except Exception:
                    pass
            return False

        session = StringSession(session_str) if session_str else StringSession()
        self.admin_client = TelegramClient(session, int(api_id), api_hash)

        try:
            await self.admin_client.start(phone)
            me = await self.admin_client.get_me()
            self.state.COMMAND_USER_ID = me.id
            self.state.admin_client = self.admin_client

            # Save session for next time
            new_session = self.admin_client.session.save()
            if new_session:
                os.environ["ADMIN_SESSION"] = new_session

            self.cfg.logger.info(f"Admin ({me.id}) started — {phone}")
            self._register_command_handlers(self.admin_client)
            return True

        except telethon.errors.AuthKeyDuplicatedError:
            self.cfg.logger.error("Admin AuthKeyDuplicated. Delete session and restart.")
            return False
        except Exception as e:
            self.cfg.logger.error(f"Admin failed: {e}")
            return False

    def _register_command_handlers(self, client: TelegramClient):
        """ Register command handlers on admin client only. """
        pat = r'(?i)^/(?:' + "|".join(self.cfg.COMMANDS) + r')\b'

        @client.on(events.NewMessage(incoming=True, pattern=pat, chats=[self.cfg.COMMAND_GROUP_ID]))
        @client.on(events.NewMessage(outgoing=True, pattern=pat, chats=[self.cfg.COMMAND_GROUP_ID]))
        async def on_command(ev):
            await self.commands.route(client, ev)

        # Also handle messages in command group for pending ops & wizards
        @client.on(events.NewMessage(incoming=True, chats=[self.cfg.COMMAND_GROUP_ID]))
        @client.on(events.NewMessage(outgoing=True, chats=[self.cfg.COMMAND_GROUP_ID]))
        async def on_admin_message(ev):
            await self._on_admin_message(ev)

    def _register_handlers(self, account: ManagedAccount):
        """ Register message handlers on managed accounts. """
        client = account.client

        @client.on(events.NewMessage(incoming=True))
        @client.on(events.NewMessage(outgoing=True))
        async def on_message(ev):
            await self._on_message(ev)

    async def _on_message(self, ev):
        """ Quick filter + pipeline submit for managed accounts. """
        text = ev.message.message or ""

        if not text or text.startswith("✉") or ev.out:
            return
        if ev.is_private:
            return
        if ev.chat_id in self.cfg.EXCLUDED_GROUPS:
            return
        if FastUtils.should_ignore(text):
            return

        await self.pipeline.submit(ev)

    async def _on_admin_message(self, ev):
        """ Handle admin messages — wizards, pending ops. """
        text = ev.message.message or ""
        sender_id = ev.message.sender_id
        chat_id = ev.chat_id
        key = (chat_id, sender_id)

        # Check wizard
        if sender_id in self.state.pending_wizards:
            completed = await self.account_wizard.handle_step(
                self.admin_client, chat_id, sender_id, text
            )
            if not completed:
                return

        # Check pending ops
        if key in self.state.pending_ops:
            # Route through BotCommands pending handler
            await self.commands._handle_pending_op(self.admin_client, ev, key)

    async def _process_message(self, ev):
        """ Pipeline processing — forwarded to managed account logic. """
        text = ev.message.message or ""
        key = self.formatter.message_key(ev)
        key_str = str(key)

        if key_str in self.state.forward_filter:
            return

        # Find which account this event belongs to
        client = ev.client
        account = None
        for acc in self.account_manager.get_all_accounts():
            if acc.client is client:
                account = acc
                break

        if not account:
            return

        sender = await ev.get_sender()
        if not sender:
            return
        if getattr(sender, "bot", False):
            return

        # Admin check
        if await self._is_admin(ev.chat_id, sender.id, client):
            return

        # Blocked check
        if sender.id in self.state.blocked_users:
            return

        # Forward
        if self.cfg.KW_RE.search(text):
            await self._fast_forward(ev, key_str)

        # Reply
        if not self.state.trigger_matcher.should_trigger(text):
            return

        await self._fast_reply(ev, sender, key, key_str)

    async def _is_admin(self, chat_id, user_id, client) -> bool:
        cache_key = (chat_id, user_id)
        if cache_key in self.state.admin_cache:
            return self.state.admin_cache[cache_key]
        try:
            from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
            participant = await client.get_participant(chat_id, user_id)
            is_admin = isinstance(participant, (ChannelParticipantAdmin, ChannelParticipantCreator))
            self.state.admin_cache[cache_key] = is_admin
            return is_admin
        except Exception:
            self.state.admin_cache[cache_key] = False
            return False

    async def _fast_forward(self, ev, key_str):
        self.state.forward_filter.add(key_str)
        fwd_text = await self.formatter.build_forward_text(ev)

        tasks = [
            self.messenger.safe_send(
                acc.client, acc.target_group_id, fwd_text,
                tag=f"FWD⌁{acc.phone}"
            )
            for acc in self.account_manager.get_all_accounts()
            if acc.mode in ("forward", "both") and acc.is_connected
        ]

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fast_reply(self, ev, sender, key, key_str):
        async with self.state.sender_locks.get_lock(sender.id):
            if key_str in self.state.reply_filter:
                return

            self.state.reply_filter.add(key_str)

            # Rate limit
            count = await self.db.count_auto_replies_distinct(
                sender.id, self.cfg.RATE_LIMIT_WINDOW_HOURS
            )
            if count >= self.cfg.RATE_LIMIT_THRESHOLD:
                await self._auto_block(sender)
                return

            dedupe_key = FastUtils.make_dedupe_key(key)
            log_id = await self._log_reply(sender, dedupe_key, ev)

            # Fallback forward
            await self._fallback_forward(ev)

            # Send reply from any account
            any_ok = False
            for acc in self.account_manager.get_all_accounts():
                if acc.mode not in ("reply", "both"):
                    continue

                try:
                    tgt = sender if acc.client is ev.client else await acc.client.get_entity(sender.id)
                    if not tgt:
                        continue

                    sent_orig = await self.messenger.safe_send(
                        acc.client, tgt, ev.message.message or "",
                        tag=f"ORIG⌁{acc.phone}"
                    )
                    if not sent_orig:
                        continue

                    auto_text = self.state.next_auto_reply()
                    sent_auto = await self.messenger.safe_send(
                        acc.client, tgt, auto_text, tag=f"AUTO⌁{acc.phone}"
                    )

                    if sent_auto and log_id:
                        try:
                            await self.db.update_auto_reply_log(
                                log_id, bot_phone=acc.phone,
                                message_id=getattr(sent_auto, "id", None)
                            )
                        except Exception:
                            pass

                    any_ok = True
                    break

                except Exception as e:
                    self.logger.debug(f"Reply failed on {acc.phone}: {e}")
                    continue

            if not any_ok:
                await self._fallback_forward(ev, "⚠️ لم يتم الرد:")

    async def _auto_block(self, sender):
        uname = getattr(sender, "username", "") or ""
        dname = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip()
        await self.db.add_blocked_user(sender.id, uname, dname)
        self.state.blocked_users[sender.id] = (uname, dname)
        self.cfg.logger.info(f"Auto-blocked user {sender.id} @{uname}")

    async def _log_reply(self, sender, dedupe_key, ev):
        try:
            uname = getattr(sender, "username", "") or ""
            dname = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip()
            return await self.db.log_auto_reply(
                sender.id, uname, dname, dedupe_key,
                ev.chat_id, ev.message.id
            )
        except Exception as e:
            self.cfg.logger.warning(f"Log failed: {e}")
            return 0

    async def _fallback_forward(self, ev, prefix=""):
        try:
            fwd = await self.formatter.build_forward_text(ev)
            if prefix:
                fwd = f"{prefix}\n\n{fwd}"

            for acc in self.account_manager.get_all_accounts():
                if not acc.is_connected:
                    continue
                try:
                    await acc.client.forward_messages(
                        self.cfg.FALLBACK_GROUP_ID, ev.message
                    )
                    return
                except Exception:
                    pass

            for acc in self.account_manager.get_all_accounts():
                if not acc.is_connected:
                    continue
                if await self.messenger.safe_send(
                    acc.client, self.cfg.FALLBACK_GROUP_ID, fwd, tag="FALLBACK"
                ):
                    return
        except Exception as e:
            self.cfg.logger.error(f"Fallback failed: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(BotManager().start())
    except KeyboardInterrupt:
        print("Shutdown requested.")
    except Exception:
        import traceback
        traceback.print_exc()
