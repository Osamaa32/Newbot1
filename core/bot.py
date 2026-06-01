import asyncio
import logging
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple

import telethon
from telethon import events, types, Button
from telethon.errors import (
    FloodWaitError, UserIsBlockedError, MessageTooLongError,
    UserAlreadyParticipantError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
from telethon.tl.types import (
    ChannelParticipant, ChannelParticipantSelf,
    ChannelParticipantAdmin, ChannelParticipantCreator
)
from telethon import TelegramClient

from core.config import AppConfig
from core.db import PostgresDB
from core.utils import FastUtils, BloomFilter, SenderLockManager
from core.services import TriggerMatcher, MessageFormatter, GroupRepo
from core.accounts import (
    FastMessenger, ManagedAccount, AccountManager, AccountWizard
)


class SharedState:
    """ Shared state across all accounts. """

    __slots__ = (
        "bots", "forward_filter", "reply_filter", "trigger_matcher",
        "sender_locks", "direct_triggers", "blocked_phrases", "auto_replies",
        "_auto_index", "pending_ops", "pending_wizards", "COMMAND_USER_ID",
        "admin_client", "stop_joining_flags", "joining_now",
        "blocked_users", "admin_cache"
    )

    def __init__(self):
        self.bots: list = []
        self.forward_filter = BloomFilter(50_000_000)
        self.reply_filter = BloomFilter(50_000_000)
        self.trigger_matcher = TriggerMatcher()
        self.sender_locks = SenderLockManager(max_concurrent=500)
        self.direct_triggers: list[str] = []
        self.blocked_phrases: list[str] = []
        self.auto_replies: list[str] = []
        self._auto_index = 0
        self.pending_ops: Dict[Tuple[int, int], Tuple[str, str]] = {}
        self.pending_wizards: Dict[int, dict] = {}
        self.COMMAND_USER_ID: Optional[int] = None
        self.admin_client: Optional[TelegramClient] = None
        self.stop_joining_flags: Dict[str, bool] = {}
        self.joining_now: Dict[str, asyncio.Task] = {}
        self.blocked_users: Dict[int, Tuple[str, str]] = {}
        self.admin_cache: Dict[Tuple[int, int], bool] = {}

    def next_auto_reply(self) -> str:
        if not self.auto_replies:
            return "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك"
        msg = self.auto_replies[self._auto_index]
        self._auto_index = (self._auto_index + 1) % len(self.auto_replies)
        return msg


class MessagePipeline:
    """ Async pipeline — decouples receiving from processing. """

    __slots__ = ("queue", "semaphore", "handler", "dropped", "logger")

    def __init__(self, handler, max_workers: int = 50, logger=None):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self.semaphore = asyncio.Semaphore(max_workers)
        self.handler = handler
        self.dropped = 0
        self.logger = logger

    def start(self, num_workers: int = 4):
        for i in range(num_workers):
            asyncio.create_task(self._worker(f"worker-{i}"))

    async def _worker(self, name: str):
        while True:
            ev = None
            try:
                ev = await self.queue.get()
                async with self.semaphore:
                    await asyncio.wait_for(self.handler(ev), timeout=30.0)
            except asyncio.CancelledError:
                if ev is not None:
                    self.queue.task_done()
                break
            except asyncio.TimeoutError:
                self.dropped += 1
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"Pipeline {name} error: {e}")
            else:
                # Success — mark done
                if ev is not None:
                    self.queue.task_done()

    async def submit(self, ev: events.NewMessage.Event):
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            self.dropped += 1


class BotCommands:
    """ All bot command handlers. """

    def __init__(self, cfg: AppConfig, db: PostgresDB, state: SharedState,
                 messenger: FastMessenger, formatter: MessageFormatter,
                 group_repo: GroupRepo, account_manager: AccountManager,
                 account_wizard: AccountWizard, logger: logging.Logger):
        self.cfg = cfg
        self.db = db
        self.state = state
        self.messenger = messenger
        self.formatter = formatter
        self.group_repo = group_repo
        self.account_manager = account_manager
        self.account_wizard = account_wizard
        self.logger = logger

    async def route(self, client: TelegramClient, ev: events.NewMessage.Event):
        chat_id = ev.chat_id
        sender_id = ev.message.sender_id
        raw = ev.message.message.strip()
        parts = raw.split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1] if len(parts) > 1 else ""

        # Check wizard first
        if sender_id in self.state.pending_wizards:
            completed = await self.account_wizard.handle_step(
                client, chat_id, sender_id, raw
            )
            if not completed:
                return
            # Wizard done, fall through only if not a command
            if cmd in self.cfg.COMMANDS:
                pass  # Allow command after wizard
            else:
                return

        # Check pending ops
        key = (chat_id, sender_id)
        if key in self.state.pending_ops and cmd not in self.cfg.COMMANDS:
            await self._handle_pending_op(client, ev, key)
            return

        # Auth check
        if self.state.COMMAND_USER_ID and sender_id != self.state.COMMAND_USER_ID:
            return

        if cmd not in self.cfg.COMMANDS:
            return

        try:
            await self._dispatch(client, chat_id, cmd, arg, ev)
        except Exception as e:
            self.logger.error(f"Command /{cmd} error: {e}")
            await self.messenger.safe_send(client, chat_id, f"❌ خطأ: {e}", tag="CMD")

    async def _dispatch(self, client, chat_id, cmd, arg, ev):
        handlers = {
            "help": self._cmd_help,
            "stats": self._cmd_stats,
            "add": self._cmd_add,
            "del": self._cmd_del,
            "list": self._cmd_list,
            "find": self._cmd_find,
            "blkadd": self._cmd_blkadd,
            "blkdel": self._cmd_blkdel,
            "blklist": self._cmd_blklist,
            "blkfind": self._cmd_blkfind,
            "autoadd": self._cmd_autoadd,
            "autodel": self._cmd_autodel,
            "autolist": self._cmd_autolist,
            "autofind": self._cmd_autofind,
            "groupadd": self._cmd_groupadd,
            "groupdel": self._cmd_groupdel,
            "groupupdate": self._cmd_groupupdate,
            "grouplist": self._cmd_grouplist,
            "groupcount": self._cmd_groupcount,
            "joingroups": self._cmd_joingroups,
            "stopjoin": self._cmd_stopjoin,
            "usergroups": self._cmd_usergroups,
            "usergroups_notin": self._cmd_usergroups_notin,
            "dbbackup": self._cmd_dbbackup,
            "dbrestore": self._cmd_dbrestore,
            "blkuser_add": self._cmd_blkuser_add,
            "blkuser_del": self._cmd_blkuser_del,
            "blkuser_list": self._cmd_blkuser_list,
            "blkuser_find": self._cmd_blkuser_find,
            "autoreplies_count": self._cmd_autoreplies_count,
            "autoreplies_list": self._cmd_autoreplies_list,
            "autoreplies_clear": self._cmd_autoreplies_clear,
            "unblock": self._cmd_unblock,
            # Account management commands
            "addaccount": self._cmd_addaccount,
            "accounts": self._cmd_accounts,
            "delaccount": self._cmd_delaccount,
            "startaccount": self._cmd_startaccount,
            "stopaccount": self._cmd_stopaccount,
            "reconnect": self._cmd_reconnect,
        }

        handler = handlers.get(cmd)
        if handler:
            await handler(client, chat_id, arg, ev)

    # ===== Account Management Commands =====

    async def _cmd_addaccount(self, client, chat_id, arg, ev):
        await self.account_wizard.start(client, chat_id, ev.message.sender_id)

    async def _cmd_accounts(self, client, chat_id, arg, ev):
        msg = await self.account_manager.list_accounts()
        await self.messenger.safe_send(client, chat_id, msg, tag="CMD")

    async def _cmd_delaccount(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(
                client, chat_id, "استخدم: /delaccount <phone>", tag="CMD"
            )
            return
        await self.account_manager.delete_account(arg)

    async def _cmd_startaccount(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(
                client, chat_id, "استخدم: /startaccount <phone>", tag="CMD"
            )
            return
        await self.account_manager.start_account(arg)

    async def _cmd_stopaccount(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(
                client, chat_id, "استخدم: /stopaccount <phone>", tag="CMD"
            )
            return
        await self.account_manager.stop_account(arg)

    async def _cmd_reconnect(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(
                client, chat_id, "استخدم: /reconnect <phone>", tag="CMD"
            )
            return
        await self.account_manager.reconnect_account(arg)

    # ===== Original Commands =====

    async def _cmd_help(self, client, chat_id, arg, ev):
        text = (
            "✨ **أوامر البوت** ✨\n\n"
            "👤 **إدارة الحسابات:**\n"
            "`/addaccount` — إضافة حساب جديد\n"
            "`/accounts` — عرض كل الحسابات\n"
            "`/startaccount <phone>` — تشغيل حساب\n"
            "`/stopaccount <phone>` — إيقاف حساب\n"
            "`/delaccount <phone>` — حذف حساب\n"
            "`/reconnect <phone>` — إعادة توصيل\n\n"
            "📊 `/stats` — ملخص التخزين\n\n"
            "**🟢 محفّزات الرد:**\n`/add /del /list /find`\n\n"
            "**⛔️ حظر نصي:**\n`/blkadd /blkdel /blklist /blkfind`\n\n"
            "**🔁 ردود تلقائية:**\n`/autoadd /autodel /autolist /autofind`\n\n"
            "**🔗 الجروبات:**\n"
            "`/groupadd /groupdel /groupupdate /grouplist /groupcount`\n\n"
            "**👥 الحسابات:**\n"
            "`/usergroups /usergroups_notin /joingroups /stopjoin`\n\n"
            "**🗄 نسخ احتياطي:**\n`/dbbackup /dbrestore`\n\n"
            "**🚫 المحظورين:**\n"
            "`/blkuser_add /blkuser_del /blkuser_list /blkuser_find`\n\n"
            "**📒 السجل:**\n"
            "`/autoreplies_count /autoreplies_list /autoreplies_clear`\n\n"
            "`/help /unblock`"
        )
        kb = [[Button.text("/stats")], [Button.text("/accounts")], [Button.text("/help")]]
        await self.messenger.safe_send(client, chat_id, text, tag="CMD", buttons=kb)

    async def _cmd_stats(self, client, chat_id, arg, ev):
        stats = await self.db.get_stats()
        accounts_msg = await self.account_manager.list_accounts()
        msg = (
            "📊 **ملخص التخزين:**\n\n"
            f"• 🟢 رد مباشر: **{stats['direct']}**\n"
            f"• ⛔️ حظر نصي: **{stats['blocked_text']}**\n"
            f"• 🚫 محظورين: **{stats['blocked_users']}**\n"
            f"• 🔗 جروبات: **{stats['groups']}**\n"
            f"• 📱 حسابات نشطة: **{stats['accounts']}**\n\n"
            f"{accounts_msg}"
        )
        await self.messenger.safe_send(client, chat_id, msg, tag="CMD")

    async def _cmd_list(self, client, chat_id, arg, ev):
        await self._send_list(client, chat_id, arg, self.state.direct_triggers)

    async def _cmd_blklist(self, client, chat_id, arg, ev):
        await self._send_list(client, chat_id, arg, self.state.blocked_phrases)

    async def _cmd_autolist(self, client, chat_id, arg, ev):
        await self._send_list(client, chat_id, arg, self.state.auto_replies)

    async def _send_list(self, client, chat_id, arg, store):
        raw = arg.strip().lower() in {"raw", "بدون", "no"}
        if not store:
            await self.messenger.safe_send(client, chat_id, "— لا يوجد —", tag="CMD")
            return
        msg = "\n".join(f"`{s}`" for s in store) if raw else "\n".join(f"{i + 1}. `{s}`" for i, s in enumerate(store))
        await self.messenger.safe_send(client, chat_id, msg, tag="CMD")

    async def _cmd_add(self, client, chat_id, arg, ev):
        await self._modify_store(client, chat_id, arg, ev, "add", "direct_reply_messages", self.state.direct_triggers)

    async def _cmd_del(self, client, chat_id, arg, ev):
        await self._modify_store(client, chat_id, arg, ev, "del", "direct_reply_messages", self.state.direct_triggers)

    async def _cmd_blkadd(self, client, chat_id, arg, ev):
        await self._modify_store(client, chat_id, arg, ev, "blkadd", "blocked_reply_messages", self.state.blocked_phrases)

    async def _cmd_blkdel(self, client, chat_id, arg, ev):
        await self._modify_store(client, chat_id, arg, ev, "blkdel", "blocked_reply_messages", self.state.blocked_phrases)

    async def _cmd_autoadd(self, client, chat_id, arg, ev):
        await self._modify_store(client, chat_id, arg, ev, "autoadd", "auto_reply_responses", self.state.auto_replies)

    async def _cmd_autodel(self, client, chat_id, arg, ev):
        await self._modify_store(client, chat_id, arg, ev, "autodel", "auto_reply_responses", self.state.auto_replies)

    async def _modify_store(self, client, chat_id, arg, ev, op, table, store):
        if not arg:
            names = {"add": "الإضافة", "del": "الحذف", "blkadd": "الإضافة",
                     "blkdel": "الحذف", "autoadd": "الإضافة", "autodel": "الحذف"}
            await self.messenger.safe_send(
                client, chat_id, f"✍️ أرسل العناصر لـ **{names.get(op, op)}**:", tag="CMD"
            )
            self.state.pending_ops[(chat_id, ev.message.sender_id)] = (op, table)
            return

        lines = [l.strip() for l in arg.splitlines() if l.strip()]
        results = []
        if "add" in op:
            for line in lines:
                if line not in store:
                    await self.db.insert_table(table, line)
                    store.append(line)
                    results.append(f"✓ {line}")
                else:
                    results.append(f"⚠️ موجود: {line}")
        else:
            for line in lines:
                if line in store:
                    await self.db.delete_table(table, line)
                    store.remove(line)
                    results.append(f"✓ {line}")
                else:
                    results.append(f"⚠️ غير موجود: {line}")

        await self.messenger.safe_send(client, chat_id, "\n".join(results), tag="CMD")
        if table in ("direct_reply_messages", "blocked_reply_messages"):
            self.state.trigger_matcher.rebuild(self.state.direct_triggers, self.state.blocked_phrases)

    async def _cmd_find(self, client, chat_id, arg, ev):
        await self._find_store(client, chat_id, arg, self.state.direct_triggers, 80)

    async def _cmd_blkfind(self, client, chat_id, arg, ev):
        await self._find_store(client, chat_id, arg, self.state.blocked_phrases, 100)

    async def _cmd_autofind(self, client, chat_id, arg, ev):
        await self._find_store(client, chat_id, arg, self.state.auto_replies, 80)

    async def _find_store(self, client, chat_id, arg, store, threshold):
        if not arg:
            await self.messenger.safe_send(client, chat_id, "اكتب نمط البحث.", tag="CMD")
            return
        from rapidfuzz import fuzz
        patterns = [l.strip() for l in arg.splitlines() if l.strip()]
        lines = []
        for pat in patterns:
            norm = FastUtils.normalize_text(pat)
            matches = [m for m in store if fuzz.ratio(norm, FastUtils.normalize_text(m)) >= threshold]
            if matches:
                lines.append(f"🔎 **{pat}:**")
                for m in matches:
                    lines.append(f"```\n{m}\n```")
            else:
                lines.append(f"🔎 **{pat}:** — لا توجد —")
        await self.messenger.safe_send(client, chat_id, "\n".join(lines), tag="CMD")

    async def _cmd_groupadd(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(client, chat_id, "✍️ أرسل الروابط:", tag="CMD")
            self.state.pending_ops[(chat_id, ev.message.sender_id)] = ("groupadd", "join_groups")
            return
        lines = [l.strip() for l in arg.splitlines() if l.strip()]
        existing = set(await self.group_repo.all())
        results = []
        for link in lines:
            if link not in existing:
                await self.group_repo.add(link)
                existing.add(link)
                results.append(f"✓ {link}")
            else:
                results.append(f"⚠️ موجود: {link}")
        await self.messenger.safe_send(client, chat_id, "\n".join(results), tag="CMD")

    async def _cmd_groupdel(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(client, chat_id, "✍️ أرسل الروابط للحذف:", tag="CMD")
            self.state.pending_ops[(chat_id, ev.message.sender_id)] = ("groupdel", "join_groups")
            return
        lines = [l.strip() for l in arg.splitlines() if l.strip()]
        existing = set(await self.group_repo.all())
        results = []
        for link in lines:
            if link in existing:
                await self.group_repo.delete(link)
                results.append(f"✓ {link}")
            else:
                results.append(f"⚠️ غير موجود: {link}")
        await self.messenger.safe_send(client, chat_id, "\n".join(results), tag="CMD")

    async def _cmd_groupupdate(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(client, chat_id, "✍️ أرسل سطرين: القديم ثم الجديد.", tag="CMD")
            self.state.pending_ops[(chat_id, ev.message.sender_id)] = ("groupupdate", "join_groups")
            return
        lines = [l.strip() for l in arg.splitlines() if l.strip()]
        if len(lines) == 2:
            await self.group_repo.update(lines[0], lines[1])
            await self.messenger.safe_send(client, chat_id, f"✓ {lines[0]} → {lines[1]}", tag="CMD")
        else:
            await self.messenger.safe_send(client, chat_id, "⚠️ أرسل سطرين.", tag="CMD")

    async def _cmd_grouplist(self, client, chat_id, arg, ev):
        links = await self.group_repo.all()
        raw = arg.strip().lower() in {"raw", "بدون", "no"}
        if not links:
            msg = "لا يوجد روابط."
        elif raw:
            msg = "\n".join(links)
        else:
            msg = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(links))
        kb = [[Button.text("/grouplist"), Button.text("/grouplist raw")]]
        await self.messenger.safe_send(client, chat_id, msg, tag="CMD", buttons=kb)

    async def _cmd_groupcount(self, client, chat_id, arg, ev):
        links = await self.group_repo.all()
        await self.messenger.safe_send(client, chat_id, f"📊 العدد: {len(links)}", tag="CMD")

    async def _cmd_joingroups(self, client, chat_id, arg, ev):
        if not arg:
            accs = "\n".join(f"- {a.phone}" for a in self.account_manager.get_all_accounts())
            await self.messenger.safe_send(
                client, chat_id, f"**الحسابات:**\n{accs}\n\n✍️ أرسل: `<رقم> [بداية]`", tag="CMD"
            )
            self.state.pending_ops[(chat_id, ev.message.sender_id)] = ("joingroups", "join_groups")
            return
        parts = arg.strip().split()
        phone = parts[0]
        start = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
        account = self.account_manager.get_account(phone)
        if not account:
            await self.messenger.safe_send(client, chat_id, f"⚠️ لا يوجد: {phone}", tag="CMD")
            return
        self.state.stop_joining_flags[phone] = False
        task = asyncio.create_task(account.join_groups(self.group_repo, self.state, start))
        self.state.joining_now[phone] = task
        await self.messenger.safe_send(client, chat_id, f"⏳ {phone} من {start + 1}.", tag="CMD")

    async def _cmd_stopjoin(self, client, chat_id, arg, ev):
        if not self.state.joining_now:
            await self.messenger.safe_send(client, chat_id, "لا توجد عمليات.", tag="CMD")
            return
        accs = "\n".join(f"- {p}" for p in self.state.joining_now)
        await self.messenger.safe_send(
            client, chat_id, f"**نشطة:**\n{accs}\n\n✍️ أرسل رقم أو `all`.", tag="CMD"
        )
        self.state.pending_ops[(chat_id, ev.message.sender_id)] = ("stopjoin", "join_groups")

    async def _cmd_usergroups(self, client, chat_id, arg, ev):
        if not arg:
            accs = "\n".join(f"- {a.phone}" for a in self.account_manager.get_all_accounts())
            await self.messenger.safe_send(
                client, chat_id, f"**الحسابات:**\n{accs}\n\n✍️ أرسل الرقم.", tag="CMD"
            )
            self.state.pending_ops[(chat_id, ev.message.sender_id)] = ("usergroups", "join_groups")
            return
        phone = arg.strip()
        account = self.account_manager.get_account(phone)
        if not account:
            await self.messenger.safe_send(client, chat_id, f"⚠️ لا يوجد: {phone}", tag="CMD")
            return
        in_g, not_in = await account.user_groups_status(self.group_repo)
        msg = f"🔢 **{phone}**\n✅ عضو: {len(in_g)}\n❌ خارج: {len(not_in)}"
        await self.messenger.safe_send(client, chat_id, msg, tag="CMD")

    async def _cmd_usergroups_notin(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(client, chat_id, "اكتب: /usergroups_notin <phone>", tag="CMD")
            return
        phone = arg.strip()
        account = self.account_manager.get_account(phone)
        if not account:
            await self.messenger.safe_send(client, chat_id, f"⚠️ لا يوجد: {phone}", tag="CMD")
            return
        _, not_in = await account.user_groups_status(self.group_repo)
        if not_in:
            await self.messenger.safe_send(client, chat_id, "❗️ خارجها:\n" + "\n".join(not_in[:100]), tag="CMD")
        else:
            await self.messenger.safe_send(client, chat_id, "✅ عضو في الكل!", tag="CMD")

    async def _cmd_dbbackup(self, client, chat_id, arg, ev):
        import datetime
        ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        out_path = f"backups/db_{ts}.json.gz"
        await self.messenger.safe_send(client, chat_id, "⏳ نسخ احتياطي...", tag="CMD")
        try:
            await self.db.export_json_gz(out_path)
            await self.messenger.safe_send_file(client, chat_id, out_path, caption=f"✅ تم: `{out_path}`", tag="DBFILE")
        except Exception as e:
            await self.messenger.safe_send(client, chat_id, f"❌ فشل: `{e}`", tag="CMD")

    async def _cmd_dbrestore(self, client, chat_id, arg, ev):
        await self.messenger.safe_send(client, chat_id, "⏳ استرجاع...", tag="CMD")
        try:
            in_path = None
            if ev.is_reply:
                rep = await ev.get_reply_message()
                if rep and rep.document:
                    Path("backups").mkdir(parents=True, exist_ok=True)
                    in_path = await client.download_media(rep, file="backups/")
            if not in_path:
                files = sorted(Path("backups").glob("db_*.json.gz"), reverse=True)
                if not files:
                    await self.messenger.safe_send(client, chat_id, "❌ لا توجد نسخ.", tag="CMD")
                    return
                in_path = str(files[0])
            await self.db.import_json_gz(in_path)
            # Reload state
            self.state.direct_triggers = await self.db.load_table("direct_reply_messages")
            self.state.blocked_phrases = await self.db.load_table("blocked_reply_messages")
            self.state.auto_replies = await self.db.load_table("auto_reply_responses")
            self.state.blocked_users = await self.db.blocked_users_map()
            self.state.trigger_matcher.rebuild(self.state.direct_triggers, self.state.blocked_phrases)
            await self.messenger.safe_send(client, chat_id, "✅ تم الاسترجاع.", tag="CMD")
        except Exception as e:
            await self.messenger.safe_send(client, chat_id, f"❌ فشل: `{e}`", tag="CMD")

    async def _cmd_blkuser_add(self, client, chat_id, arg, ev):
        uid, uname, dname = None, "", ""
        if ev.is_reply:
            rep = await ev.get_reply_message()
            snd = await rep.get_sender()
            uid = getattr(snd, "id", None)
            uname = getattr(snd, "username", "") or ""
            dname = f"{getattr(snd, 'first_name', '') or ''} {getattr(snd, 'last_name', '') or ''}".strip()
        elif arg:
            parts = arg.split()
            try:
                uid = int(parts[0])
                uname = parts[1] if len(parts) > 1 else ""
                dname = " ".join(parts[2:]) if len(parts) > 2 else ""
            except Exception:
                pass
        if not uid:
            await self.messenger.safe_send(client, chat_id, "❌ حدد مستخدم.", tag="CMD")
            return
        await self.db.add_blocked_user(uid, uname, dname)
        self.state.blocked_users[uid] = (uname, dname)
        await self.messenger.safe_send(client, chat_id, f"✅ أُضيف: {uid} @{uname or '—'}", tag="CMD")

    async def _cmd_blkuser_del(self, client, chat_id, arg, ev):
        uid = None
        if ev.is_reply:
            rep = await ev.get_reply_message()
            snd = await rep.get_sender()
            uid = getattr(snd, "id", None)
        elif arg:
            try:
                uid = int(arg.strip())
            except Exception:
                pass
        if not uid:
            await self.messenger.safe_send(client, chat_id, "❌ حدد مستخدم.", tag="CMD")
            return
        await self.db.del_blocked_user(uid)
        self.state.blocked_users.pop(uid, None)
        await self.messenger.safe_send(client, chat_id, f"✅ أُزيل: {uid}", tag="CMD")

    async def _cmd_blkuser_list(self, client, chat_id, arg, ev):
        raw = arg.strip().lower() in {"raw", "بدون", "no"}
        rows = await self.db.list_blocked_users(200)
        if not rows:
            await self.messenger.safe_send(client, chat_id, "— لا يوجد —", tag="CMD")
            return
        if raw:
            msg = "\n".join(f"{r['user_id']} @{r['username'] or '—'} | {r['display_name'] or '—'}" for r in rows)
        else:
            msg = "\n".join(
                f"🔹 `{r['user_id']}`{' | @' + r['username'] if r['username'] else ''}\n"
                f"📝 `{r['display_name'] or '—'}` | 🕒 `{r['created_at']}`"
                for r in rows
            )
        await self.messenger.safe_send(client, chat_id, msg, tag="CMD")

    async def _cmd_blkuser_find(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(client, chat_id, "اكتب: /blkuser_find <pattern>", tag="CMD")
            return
        rows = await self.db.find_blocked_users(arg, 200)
        if not rows:
            await self.messenger.safe_send(client, chat_id, "— لا توجد —", tag="CMD")
            return
        msg = "\n".join(f"- {r['user_id']} @{r['username'] or '—'} | {r['display_name'] or '—'}" for r in rows)
        await self.messenger.safe_send(client, chat_id, msg, tag="CMD")

    async def _cmd_autoreplies_count(self, client, chat_id, arg, ev):
        if not arg:
            await self.messenger.safe_send(client, chat_id, "استخدم: /autoreplies_count <user_id>", tag="CMD")
            return
        try:
            uid = int(arg.strip())
        except Exception:
            await self.messenger.safe_send(client, chat_id, "صيغة غير صحيحة.", tag="CMD")
            return
        count = await self.db.count_auto_replies(uid)
        await self.messenger.safe_send(client, chat_id, f"🔢 {uid}: {count} رد", tag="CMD")

    async def _cmd_autoreplies_list(self, client, chat_id, arg, ev):
        limit = int(arg.strip()) if arg.strip().isdigit() else 50
        uid = None
        if ev.is_reply:
            rep = await ev.get_reply_message()
            snd = await rep.get_sender()
            uid = getattr(snd, "id", None)
        rows = await (self.db.list_auto_replies_for_user(uid, limit) if uid else self.db.list_auto_replies(limit))
        if not rows:
            await self.messenger.safe_send(client, chat_id, "— لا يوجد —", tag="CMD")
            return
        lines = [
            f"🔹 #{r['id']} 👤 `{r['user_id']}` 🤖 `{r['bot_phone'] or '—'}` 🕒 `{r['created_at']}`"
            for r in rows
        ]
        await self.messenger.safe_send(client, chat_id, "📒 السجل:\n" + "\n".join(lines), tag="CMD")

    async def _cmd_autoreplies_clear(self, client, chat_id, arg, ev):
        if arg.strip().lower() == "all":
            count = await self.db.clear_auto_replies()
            await self.messenger.safe_send(client, chat_id, f"🧹 مُسح {count} سجل.", tag="CMD")
            return
        uid = None
        if ev.is_reply:
            rep = await ev.get_reply_message()
            snd = await rep.get_sender()
            uid = getattr(snd, "id", None)
        elif arg:
            try:
                uid = int(arg.strip())
            except Exception:
                pass
        if not uid:
            await self.messenger.safe_send(client, chat_id, "حدد مستخدم أو all.", tag="CMD")
            return
        count = await self.db.clear_auto_replies(uid)
        await self.messenger.safe_send(client, chat_id, f"🧹 مُسح {count} سجل للمستخدم {uid}.", tag="CMD")

    async def _cmd_unblock(self, client, chat_id, arg, ev):
        for account in self.account_manager.get_all_accounts():
            asyncio.create_task(self._start_spambot(account.client))
        await self.messenger.safe_send(client, chat_id, "✓ أُرسل /start لـ @SpamBot.", tag="CMD")

    async def _start_spambot(self, client: TelegramClient):
        try:
            async with client.conversation("@SpamBot") as conv:
                await conv.send_message("/start")
                await conv.get_response(timeout=10)
        except Exception:
            pass

    async def _handle_pending_op(self, client, ev, key):
        op, table = self.state.pending_ops.pop(key)
        chat_id = ev.chat_id
        text = ev.message.message or ""
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        if op == "stopjoin":
            choice = lines[0].lower() if lines else ""
            if choice == "all":
                for phone in list(self.state.joining_now):
                    self.state.stop_joining_flags[phone] = True
                await self.messenger.safe_send(client, chat_id, "⏹ أُوقف الكل.", tag="CMD")
            elif choice in self.state.joining_now:
                self.state.stop_joining_flags[choice] = True
                await self.messenger.safe_send(client, chat_id, f"⏹ أُوقف {choice}.", tag="CMD")
            else:
                await self.messenger.safe_send(client, chat_id, f"⚠️ لا عملية: {choice}", tag="CMD")
            return

        if op == "groupadd":
            existing = set(await self.group_repo.all())
            results = []
            for link in lines:
                if link not in existing:
                    await self.group_repo.add(link)
                    existing.add(link)
                    results.append(f"✓ {link}")
                else:
                    results.append(f"⚠️ موجود: {link}")
            await self.messenger.safe_send(client, chat_id, "\n".join(results), tag="CMD")
            return

        if op == "groupdel":
            existing = set(await self.group_repo.all())
            results = []
            for link in lines:
                if link in existing:
                    await self.group_repo.delete(link)
                    results.append(f"✓ {link}")
                else:
                    results.append(f"⚠️ غير موجود: {link}")
            await self.messenger.safe_send(client, chat_id, "\n".join(results), tag="CMD")
            return

        if op == "groupupdate":
            if len(lines) == 2:
                await self.group_repo.update(lines[0], lines[1])
                await self.messenger.safe_send(client, chat_id, f"✓ {lines[0]} → {lines[1]}", tag="CMD")
            else:
                await self.messenger.safe_send(client, chat_id, "⚠️ أرسل سطرين.", tag="CMD")
            return

        if op == "joingroups":
            parts = lines[0].split() if lines else []
            phone = parts[0] if parts else ""
            start = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
            account = self.account_manager.get_account(phone)
            if not account:
                await self.messenger.safe_send(client, chat_id, f"⚠️ لا يوجد: {phone}", tag="CMD")
                return
            self.state.stop_joining_flags[phone] = False
            task = asyncio.create_task(account.join_groups(self.group_repo, self.state, start))
            self.state.joining_now[phone] = task
            await self.messenger.safe_send(client, chat_id, f"⏳ {phone} من {start + 1}.", tag="CMD")
            return

        if op == "usergroups":
            phone = lines[0] if lines else ""
            account = self.account_manager.get_account(phone)
            if not account:
                await self.messenger.safe_send(client, chat_id, f"⚠️ لا يوجد: {phone}", tag="CMD")
                return
            in_g, not_in = await account.user_groups_status(self.group_repo)
            msg = f"🔢 **{phone}**\n✅ عضو: {len(in_g)}\n❌ خارج: {len(not_in)}"
            await self.messenger.safe_send(client, chat_id, msg, tag="CMD")
            return

        # Text store operations
        stores = {
            "direct_reply_messages": self.state.direct_triggers,
            "blocked_reply_messages": self.state.blocked_phrases,
            "auto_reply_responses": self.state.auto_replies
        }
        store = stores.get(table)
        if not store:
            return

        op_type = op.split("_")[0]

        if op_type in ("add", "blkadd", "autoadd"):
            results = []
            for line in lines:
                if line not in store:
                    await self.db.insert_table(table, line)
                    store.append(line)
                    results.append(f"✓ {line}")
                else:
                    results.append(f"⚠️ موجود: {line}")
            await self.messenger.safe_send(client, chat_id, "\n".join(results), tag="CMD")

        elif op_type in ("del", "blkdel", "autodel"):
            results = []
            for line in lines:
                if line in store:
                    await self.db.delete_table(table, line)
                    store.remove(line)
                    results.append(f"✓ {line}")
                else:
                    results.append(f"⚠️ غير موجود: {line}")
            await self.messenger.safe_send(client, chat_id, "\n".join(results), tag="CMD")

        elif op_type in ("find", "blkfind", "autofind"):
            from rapidfuzz import fuzz
            thresh = 100 if "blk" in op else 80
            patterns = lines if lines else [text]
            msg_lines = []
            for pat in patterns:
                norm_pat = FastUtils.normalize_text(pat)
                matches = [m for m in store if fuzz.ratio(norm_pat, FastUtils.normalize_text(m)) >= thresh]
                if matches:
                    msg_lines.append(f"🔎 **{pat}:**")
                    for m in matches:
                        msg_lines.append(f"```\n{m}\n```")
                else:
                    msg_lines.append(f"🔎 **{pat}:** — لا توجد —")
            await self.messenger.safe_send(client, chat_id, "\n".join(msg_lines), tag="CMD")

        if table in ("direct_reply_messages", "blocked_reply_messages"):
            self.state.trigger_matcher.rebuild(self.state.direct_triggers, self.state.blocked_phrases)
