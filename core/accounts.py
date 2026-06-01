import asyncio
import logging
from typing import Optional, Any, Dict, List, Tuple

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, UserIsBlockedError, MessageTooLongError,
    UserAlreadyParticipantError, SessionPasswordNeededError,
    PhoneCodeInvalidError, PhoneCodeExpiredError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
from telethon.tl.types import (
    ChannelParticipant, ChannelParticipantSelf,
    ChannelParticipantAdmin, ChannelParticipantCreator
)

from core.config import AppConfig
from core.db import PostgresDB
from core.utils import FastUtils, BloomFilter, SenderLockManager
from core.services import MessageFormatter


class FastMessenger:
    """ Safe message sender with retry logic. """

    __slots__ = ("logger",)

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    async def safe_send(self, client: TelegramClient, dst: Any, text: str,
                        tag: str = "SEND", buttons=None
                        ) -> Optional[Any]:
        if len(text) > 4096:
            last = None
            for part in FastUtils.split_long(text, 4096):
                last = await self.safe_send(
                    client, dst, part, tag=tag, buttons=buttons
                )
                buttons = None
            return last

        delay = 1
        for _ in range(3):
            try:
                return await client.send_message(
                    dst, text, parse_mode="Markdown", buttons=buttons
                )
            except FloodWaitError as e:
                self.logger.warning(f"{tag} FloodWait {e.seconds}s")
                await asyncio.sleep(min(delay, e.seconds))
                delay *= 2
            except MessageTooLongError:
                for part in FastUtils.split_long(text, 4096):
                    await client.send_message(dst, part, parse_mode="Markdown")
                return None
            except UserIsBlockedError:
                self.logger.warning(f"{tag} blocked by {dst}")
                return None
            except Exception as ex:
                self.logger.error(f"{tag} error: {ex}")
                return None
        return None

    async def safe_send_file(self, client: TelegramClient, dst: Any,
                              file_path: str, caption: str = "",
                              tag: str = "FILE") -> Optional[Any]:
        delay = 1
        for _ in range(3):
            try:
                return await client.send_file(
                    dst, file_path, caption=caption, force_document=True
                )
            except FloodWaitError as e:
                self.logger.warning(f"{tag} FloodWait {e.seconds}s")
                await asyncio.sleep(delay)
                delay *= 2
            except UserIsBlockedError:
                self.logger.warning(f"{tag} blocked")
                return None
            except Exception as ex:
                self.logger.error(f"{tag} error: {ex}")
                return None
        return None


class ManagedAccount:
    """ A single managed Telegram account using StringSession (no files). """

    __slots__ = (
        "cfg", "db", "phone", "api_id", "api_hash", "target_group_id",
        "mode", "session_string", "client", "logger", "is_connected",
        "telegram_id", "display_name", "_handlers", "_tasks"
    )

    def __init__(self, cfg: AppConfig, db: PostgresDB,
                 phone: str, api_id: int, api_hash: str,
                 target_group_id: int, mode: str = "both",
                 session_string: Optional[str] = None,
                 logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.db = db
        self.phone = phone
        self.api_id = api_id
        self.api_hash = api_hash
        self.target_group_id = target_group_id
        self.mode = mode.lower()
        self.session_string = session_string or ""
        self.logger = logger or cfg.logger
        self.is_connected = False
        self.telegram_id: Optional[int] = None
        self.display_name: Optional[str] = None
        self._handlers = []
        self._tasks: List[asyncio.Task] = []

        # Create client with StringSession (in-memory, no file)
        session = StringSession(self.session_string) if self.session_string else StringSession()
        self.client = TelegramClient(session, api_id, api_hash)

    async def connect(self) -> bool:
        """ Connect the account and save session string if new. """
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                self.logger.warning(f"Account {self.phone} not authorized")
                return False

            me = await self.client.get_me()
            self.telegram_id = me.id
            self.display_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or self.phone

            # Save session string if it's new
            new_session = self.client.session.save()
            if new_session and new_session != self.session_string:
                self.session_string = new_session
                await self.db.update_account_session(self.phone, new_session)
                self.logger.info(f"Session saved for {self.phone}")

            await self.db.update_account_status(
                self.phone, is_connected=True,
                telegram_id=self.telegram_id,
                display_name=self.display_name
            )
            self.is_connected = True
            self.logger.info(f"Account {self.phone} ({self.display_name}) connected")
            return True

        except Exception as e:
            self.logger.error(f"Failed to connect {self.phone}: {e}")
            await self.db.update_account_status(self.phone, is_connected=False)
            self.is_connected = False
            return False

    async def disconnect(self):
        try:
            for task in self._tasks:
                task.cancel()
            self._tasks.clear()
            await self.client.disconnect()
            await self.db.update_account_status(self.phone, is_connected=False)
            self.is_connected = False
        except Exception:
            pass

    async def restart(self) -> bool:
        await self.disconnect()
        # Rebuild client with same session
        session = StringSession(self.session_string)
        self.client = TelegramClient(session, self.api_id, self.api_hash)
        return await self.connect()

    def add_handler(self, callback, event_builder):
        self.client.add_event_handler(callback, event_builder)
        self._handlers.append((callback, event_builder))

    def create_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def join_groups(self, group_repo, state, start_index: int = 0):
        links = await group_repo.all()
        joined = 0

        for idx, link in enumerate(links[start_index:], start=start_index):
            if state.stop_joining_flags.get(self.phone):
                break

            try:
                entity = await self.client.get_entity(link)
                await self.client(JoinChannelRequest(entity))
                joined += 1
                self.logger.info(f"[{self.phone}] Joined {link} ({idx+1}/{len(links)})")
                await asyncio.sleep(250)
            except UserAlreadyParticipantError:
                self.logger.info(f"[{self.phone}] Already in {link}")
            except FloodWaitError as e:
                self.logger.warning(f"[{self.phone}] FloodWait {e.seconds}s")
                await asyncio.sleep(e.seconds + 2)
                if state.stop_joining_flags.get(self.phone):
                    break
            except Exception as ex:
                self.logger.error(f"[{self.phone}] Failed {link}: {ex}")

        self.logger.info(f"[{self.phone}] Joined {joined}/{len(links)}")

    async def user_groups_status(self, group_repo) -> Tuple[List[str], List[str]]:
        links = await group_repo.all()
        in_groups, not_in = [], []
        me = await self.client.get_me()

        for link in links:
            try:
                entity = await self.client.get_entity(link)
                result = await self.client(GetParticipantRequest(entity, me.id))
                participant = result.participant
                if isinstance(participant, (ChannelParticipant, ChannelParticipantSelf,
                                            ChannelParticipantAdmin, ChannelParticipantCreator)):
                    in_groups.append(link)
                else:
                    not_in.append(link)
            except Exception:
                not_in.append(link)

        return in_groups, not_in


class AccountWizard:
    """ Interactive wizard for adding a new Telegram account via commands. """

    # Wizard steps
    STEP_API_ID = "api_id"
    STEP_API_HASH = "api_hash"
    STEP_PHONE = "phone"
    STEP_TARGET_GROUP = "target_group"
    STEP_MODE = "mode"
    STEP_CODE = "code"
    STEP_2FA = "2fa"

    __slots__ = ("state", "cfg", "db", "messenger", "logger")

    def __init__(self, state, cfg: AppConfig, db: PostgresDB,
                 messenger: FastMessenger, logger: logging.Logger):
        self.state = state
        self.cfg = cfg
        self.db = db
        self.messenger = messenger
        self.logger = logger

    async def start(self, client: TelegramClient, chat_id: int, user_id: int):
        """ Start the account addition wizard. """
        self.state.pending_wizards[user_id] = {
            "step": self.STEP_API_ID,
            "data": {},
            "client": None,
            "phone_code_hash": None,
        }
        await self.messenger.safe_send(
            client, chat_id,
            "🆕 **إضافة حساب جديد**\n\n"
            "الخطوة 1/5: أرسل **API ID**:\n"
            "(تحصله من [my.telegram.org](https://my.telegram.org))",
            tag="WIZARD"
        )

    async def handle_step(self, client: TelegramClient, chat_id: int,
                         user_id: int, text: str) -> bool:
        """ Handle wizard step. Returns True if wizard completed. """
        wizard = self.state.pending_wizards.get(user_id)
        if not wizard:
            return False

        step = wizard["step"]
        data = wizard["data"]

        try:
            if step == self.STEP_API_ID:
                api_id = int(text.strip())
                data["api_id"] = api_id
                wizard["step"] = self.STEP_API_HASH
                await self.messenger.safe_send(
                    client, chat_id,
                    "✅ API ID حُفظ.\n\n"
                    "الخطوة 2/5: أرسل **API HASH**:\n"
                    "(من نفس موقع my.telegram.org)",
                    tag="WIZARD"
                )
                return False

            elif step == self.STEP_API_HASH:
                api_hash = text.strip()
                if len(api_hash) < 20:
                    await self.messenger.safe_send(
                        client, chat_id, "⚠️ API Hash قصير جداً. أعد الإرسال:",
                        tag="WIZARD"
                    )
                    return False
                data["api_hash"] = api_hash
                wizard["step"] = self.STEP_PHONE
                await self.messenger.safe_send(
                    client, chat_id,
                    "✅ API Hash حُفظ.\n\n"
                    "الخطوة 3/5: أرسل **رقم الهاتف**:\n"
                    "(مثال: +966500000001)",
                    tag="WIZARD"
                )
                return False

            elif step == self.STEP_PHONE:
                phone = text.strip().replace(" ", "")
                if not phone.startswith("+") or len(phone) < 8:
                    await self.messenger.safe_send(
                        client, chat_id, "⚠️ الرقم يجب يبدأ بـ +. أعد الإرسال:",
                        tag="WIZARD"
                    )
                    return False

                data["phone"] = phone
                wizard["step"] = self.STEP_TARGET_GROUP
                await self.messenger.safe_send(
                    client, chat_id,
                    "✅ الرقم حُفظ.\n\n"
                    "الخطوة 4/5: أرسل **Target Group ID**:\n"
                    "(مثال: -1001234567890)",
                    tag="WIZARD"
                )
                return False

            elif step == self.STEP_TARGET_GROUP:
                try:
                    target_group = int(text.strip())
                except ValueError:
                    await self.messenger.safe_send(
                        client, chat_id, "⚠️ يجب أن يكون رقماً. أعد الإرسال:",
                        tag="WIZARD"
                    )
                    return False

                data["target_group_id"] = target_group
                wizard["step"] = self.STEP_MODE
                await self.messenger.safe_send(
                    client, chat_id,
                    "✅ Target Group حُفظ.\n\n"
                    "الخطوة 5/5: أرسل **الوضع**:\n"
                    "`forward` — فقط توجيه\n"
                    "`reply` — فقط رد\n"
                    "`both` — كلاهما (افتراضي)",
                    tag="WIZARD"
                )
                return False

            elif step == self.STEP_MODE:
                mode = text.strip().lower()
                if mode not in ("forward", "reply", "both"):
                    mode = "both"
                data["mode"] = mode

                # Now we have all info — send code request
                await self.messenger.safe_send(
                    client, chat_id,
                    f"⏳ جاري طلب كود التحقق لـ {data['phone']}...",
                    tag="WIZARD"
                )

                try:
                    # Create temporary client
                    temp_session = StringSession()
                    temp_client = TelegramClient(
                        temp_session, data["api_id"], data["api_hash"]
                    )
                    await temp_client.connect()

                    result = await temp_client.send_code_request(data["phone"])
                    wizard["temp_client"] = temp_client
                    wizard["phone_code_hash"] = result.phone_code_hash
                    wizard["step"] = self.STEP_CODE

                    await self.messenger.safe_send(
                        client, chat_id,
                        "📩 أُرسل كود التحقق!\n\n"
                        f"الخطوة الأخيرة: أرسل الكود المكون من 5 أرقام "
                        f"اللي وصل لـ **{data['phone']}**:",
                        tag="WIZARD"
                    )
                except Exception as e:
                    self.logger.error(f"Code request failed: {e}")
                    await self.messenger.safe_send(
                        client, chat_id, f"❌ فشل طلب الكود: `{e}`", tag="WIZARD"
                    )
                    self.state.pending_wizards.pop(user_id, None)
                    return True  # End wizard

                return False

            elif step == self.STEP_CODE:
                code = text.strip().replace(" ", "")
                temp_client = wizard.get("temp_client")
                phone_code_hash = wizard.get("phone_code_hash")

                if not temp_client or not phone_code_hash:
                    await self.messenger.safe_send(
                        client, chat_id, "❌ انتهت الجلسة. ابدأ من جديد بـ /addaccount",
                        tag="WIZARD"
                    )
                    self.state.pending_wizards.pop(user_id, None)
                    return True

                try:
                    await temp_client.sign_in(
                        phone=data["phone"],
                        code=code,
                        phone_code_hash=phone_code_hash
                    )

                    # Get session string
                    session_string = temp_client.session.save()
                    await temp_client.disconnect()

                    # Save to database
                    await self.db.add_account(
                        phone=data["phone"],
                        api_id=data["api_id"],
                        api_hash=data["api_hash"],
                        target_group_id=data["target_group_id"],
                        mode=data.get("mode", "both"),
                        session_string=session_string
                    )

                    # Account will be auto-started by AccountManager
                    await self.messenger.safe_send(
                        client, chat_id,
                        f"✅ **تم إضافة الحساب بنجاح!**\n\n"
                        f"📱 **الرقم:** `{data['phone']}`\n"
                        f"🆔 **API ID:** `{data['api_id']}`\n"
                        f"📦 **الوضع:** `{data.get('mode', 'both')}`\n"
                        f"🎯 **المجموعة:** `{data['target_group_id']}`\n\n"
                        f"⏳ الحساب سيُشغل تلقائياً...\n"
                        f"تحقق من الحالة بـ `/accounts`",
                        tag="WIZARD"
                    )

                except SessionPasswordNeededError:
                    wizard["step"] = self.STEP_2FA
                    await self.messenger.safe_send(
                        client, chat_id,
                        "🔐 الحساب محمي بـ **2FA**.\n"
                        "أرسل كلمة المرور:",
                        tag="WIZARD"
                    )
                    return False

                except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                    await self.messenger.safe_send(
                        client, chat_id,
                        f"❌ كود خاطئ أو منتهي: `{e}`.\n"
                        f"ابدأ من جديد بـ /addaccount",
                        tag="WIZARD"
                    )

                except Exception as e:
                    self.logger.error(f"Sign in failed: {e}")
                    await self.messenger.safe_send(
                        client, chat_id,
                        f"❌ فشل تسجيل الدخول: `{e}`", tag="WIZARD"
                    )

                finally:
                    # Cleanup
                    try:
                        await temp_client.disconnect()
                    except Exception:
                        pass
                    self.state.pending_wizards.pop(user_id, None)

                return True

            elif step == self.STEP_2FA:
                password = text.strip()
                temp_client = wizard.get("temp_client")

                try:
                    await temp_client.sign_in(password=password)
                    session_string = temp_client.session.save()
                    await temp_client.disconnect()

                    await self.db.add_account(
                        phone=data["phone"],
                        api_id=data["api_id"],
                        api_hash=data["api_hash"],
                        target_group_id=data["target_group_id"],
                        mode=data.get("mode", "both"),
                        session_string=session_string
                    )

                    await self.messenger.safe_send(
                        client, chat_id,
                        f"✅ **تم إضافة الحساب بنجاح (مع 2FA)!**\n\n"
                        f"📱 **الرقم:** `{data['phone']}`\n"
                        f"⏳ سيُشغل تلقائياً...",
                        tag="WIZARD"
                    )

                except Exception as e:
                    self.logger.error(f"2FA sign in failed: {e}")
                    await self.messenger.safe_send(
                        client, chat_id,
                        f"❌ فشل 2FA: `{e}`\n"
                        f"ابدأ من جديد بـ /addaccount",
                        tag="WIZARD"
                    )

                finally:
                    try:
                        await temp_client.disconnect()
                    except Exception:
                        pass
                    self.state.pending_wizards.pop(user_id, None)

                return True

        except Exception as e:
            self.logger.error(f"Wizard error: {e}")
            await self.messenger.safe_send(
                client, chat_id, f"❌ خطأ: `{e}`", tag="WIZARD"
            )
            self.state.pending_wizards.pop(user_id, None)
            return True

        return False


class AccountManager:
    """ Manages all accounts lifecycle — load, start, stop, monitor. """

    __slots__ = (
        "cfg", "db", "state", "messenger", "formatter",
        "group_repo", "logger", "accounts", "_monitor_task"
    )

    def __init__(self, cfg: AppConfig, db: PostgresDB, state,
                 messenger: FastMessenger, formatter: MessageFormatter,
                 group_repo, logger: logging.Logger):
        self.cfg = cfg
        self.db = db
        self.state = state
        self.messenger = messenger
        self.formatter = formatter
        self.group_repo = group_repo
        self.logger = logger
        self.accounts: Dict[str, ManagedAccount] = {}
        self._monitor_task: Optional[asyncio.Task] = None

    async def load_and_start_all(self):
        """ Load all active accounts from DB and connect them. """
        rows = await self.db.get_active_accounts()
        self.logger.info(f"Loading {len(rows)} active accounts from DB")

        for row in rows:
            await self._start_account_from_row(row)

        # Start health monitor
        self._monitor_task = asyncio.create_task(self._health_monitor())

    async def _start_account_from_row(self, row: dict) -> bool:
        """ Create and connect a ManagedAccount from DB row. """
        phone = row["phone"]

        if phone in self.accounts:
            self.logger.warning(f"Account {phone} already loaded")
            return False

        account = ManagedAccount(
            cfg=self.cfg,
            db=self.db,
            phone=phone,
            api_id=row["api_id"],
            api_hash=row["api_hash"],
            target_group_id=row["target_group_id"],
            mode=row.get("mode", "both"),
            session_string=row.get("session_string"),
            logger=self.logger
        )

        success = await account.connect()
        if success:
            self.accounts[phone] = account
            self.logger.info(f"Account {phone} started successfully")
            return True
        else:
            self.logger.error(f"Failed to start account {phone}")
            return False

    async def start_account(self, phone: str) -> bool:
        """ Start a specific account by phone. """
        phone = phone.replace(" ", "")

        if phone in self.accounts:
            await self.messenger.safe_send(
                self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
                f"ℹ️ الحساب {phone} مشغول بالفعل.", tag="ACCT"
            )
            return False

        row = await self.db.get_account(phone)
        if not row:
            await self.messenger.safe_send(
                self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
                f"⚠️ الحساب {phone} غير موجود في قاعدة البيانات.", tag="ACCT"
            )
            return False

        await self.db.set_account_active(phone, True)
        success = await self._start_account_from_row(row)

        if success:
            await self.messenger.safe_send(
                self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
                f"✅ الحساب {phone} شُغل.", tag="ACCT"
            )
        else:
            await self.messenger.safe_send(
                self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
                f"❌ فشل تشغيل {phone}.", tag="ACCT"
            )

        return success

    async def stop_account(self, phone: str):
        """ Stop a specific account. """
        phone = phone.replace(" ", "")
        account = self.accounts.pop(phone, None)

        if account:
            await account.disconnect()
            await self.db.set_account_active(phone, False)
            await self.messenger.safe_send(
                self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
                f"⏹ الحساب {phone} أُوقف.", tag="ACCT"
            )
        else:
            await self.messenger.safe_send(
                self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
                f"⚠️ الحساب {phone} غير مشغل.", tag="ACCT"
            )

    async def delete_account(self, phone: str):
        """ Delete an account completely. """
        phone = phone.replace(" ", "")
        account = self.accounts.pop(phone, None)

        if account:
            await account.disconnect()

        await self.db.delete_account(phone)
        await self.messenger.safe_send(
            self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
            f"🗑 الحساب {phone} حُذف.", tag="ACCT"
        )

    async def reconnect_account(self, phone: str) -> bool:
        """ Restart a specific account. """
        phone = phone.replace(" ", "")
        account = self.accounts.get(phone)

        if account:
            success = await account.restart()
            status = "✅ أُعيد الاتصال" if success else "❌ فشل"
            await self.messenger.safe_send(
                self.state.admin_client, self.cfg.COMMAND_GROUP_ID,
                f"{status} — {phone}", tag="ACCT"
            )
            return success
        else:
            return await self.start_account(phone)

    async def list_accounts(self) -> str:
        """ Get formatted list of all accounts. """
        db_accounts = await self.db.get_all_accounts()
        if not db_accounts:
            return "— لا يوجد حسابات —"

        lines = ["📱 **الحسابات:**\n"]
        for a in db_accounts:
            phone = a["phone"]
            is_live = phone in self.accounts and self.accounts[phone].is_connected
            status = "🟢 متصل" if is_live else "🔴 غير متصل"
            active = "✅ نشط" if a.get("is_active") else "⏹ معطل"
            mode = a.get("mode", "both")
            name = a.get("display_name") or "—"

            lines.append(
                f"**{phone}** — {name}\n"
                f"{status} | {active} | 🎛 `{mode}`\n"
                f"🎯 `{a.get('target_group_id', '—')}`\n"
                f"—"
            )

        return "\n".join(lines)

    async def _health_monitor(self):
        """ Periodically check account health and reconnect if needed. """
        while True:
            await asyncio.sleep(60)  # Check every minute

            for phone, account in list(self.accounts.items()):
                try:
                    if not account.client.is_connected():
                        self.logger.warning(f"{phone} disconnected, reconnecting...")
                        success = await account.restart()
                        if success:
                            self.logger.info(f"{phone} reconnected")
                        else:
                            self.logger.error(f"{phone} failed to reconnect")
                except Exception as e:
                    self.logger.error(f"Health check error for {phone}: {e}")

    async def stop_all(self):
        """ Gracefully stop all accounts. """
        if self._monitor_task:
            self._monitor_task.cancel()

        for phone, account in list(self.accounts.items()):
            await account.disconnect()

        self.accounts.clear()
        self.logger.info("All accounts stopped")

    def get_account(self, phone: str) -> Optional[ManagedAccount]:
        return self.accounts.get(phone.replace(" ", ""))

    def get_all_accounts(self) -> List[ManagedAccount]:
        return list(self.accounts.values())
