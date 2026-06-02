

# ==================== WORKER BOT (Telethon Account) ====================

class WorkerBot:
    def __init__(self, cfg: Config, db: DB, state: State, messenger: Messenger,
                 group_repo: GroupRepo, formatter: MessageFormatter,
                 fallback: FallbackRouter, client: TelegramClient,
                 target_group_id: int, phone: str, mode: str,
                 logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.db = db
        self.state = state
        self.messenger = messenger
        self.group_repo = group_repo
        self.formatter = formatter
        self.fallback = fallback
        self.client = client
        self.target_group_id = target_group_id
        self.phone = phone
        self.mode = mode.lower()
        self.logger = logger or cfg.logger

        client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        client.add_event_handler(self.on_message, events.NewMessage(outgoing=True))

    async def join_sleep(self, phone: str, base_delay: int = 30) -> None:
        delay = base_delay + random.randint(0, 30)
        for _ in range(delay):
            if self.state.stop_joining_flags.get(phone):
                break
            await asyncio.sleep(1)

    async def join_groups_with_account(self, start_index: int = 0) -> None:
        if self.state.circuit_breaker.is_open(self.phone):
            return
        links = await self.group_repo.all()
        joined = 0
        total = len(links)
        for idx, link in enumerate(links[start_index:], start=start_index):
            if self.state.stop_joining_flags.get(self.phone):
                return
            try:
                entity = await self.client.get_entity(link)
                await self.client(JoinChannelRequest(entity))
                joined += 1
                self.state.circuit_breaker.record_success(self.phone)
                await self.join_sleep(self.phone, 30)
            except UserAlreadyParticipantError:
                pass
            except FloodWaitError as e:
                self.state.circuit_breaker.record_failure(self.phone)
                await self.join_sleep(self.phone, e.seconds + 2)
                if self.state.stop_joining_flags.get(self.phone):
                    return
            except Exception:
                self.state.circuit_breaker.record_failure(self.phone)

    async def user_groups_status(self) -> Tuple[List[str], List[str]]:
        links = await self.group_repo.all()
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

    async def unified_dispatch(self, ev: events.NewMessage.Event) -> None:
        async with self.state.dispatch_semaphore:
            key = self.formatter.message_key(ev)
            text = ev.message.message or ""

            kw_re = self.cfg.build_kw_regex()
            if kw_re.search(text) and key not in self.state.FORWARD_DONE:
                self.state.FORWARD_DONE.add(key)
                fwd_txt = await self.formatter.build_forward_text(ev)
                await asyncio.gather(*[
                    self.messenger.safe_send(b.client, b.target_group_id, fwd_txt, tag=f"FWD|{b.phone}")
                    for b in self.state.bots if b.mode in ("forward", "both")
                ], return_exceptions=True)

            src_bot = next((b for b in self.state.bots if b.client is ev.client), None)
            if not src_bot:
                return

            sender = await ev.get_sender()
            sender_id = getattr(sender, "id", None)
            if sender_id and sender_id in self.state.blocked_users:
                return

            wants_reply = (
                kw_re.search(text)
                and any(TextUtils.fuzzy_match(text, trg) for trg in self.state.direct_triggers)
                and TextUtils.normalize_text(text) not in {TextUtils.normalize_text(p) for p in self.state.blocked_phrases}
            )
            if not wants_reply:
                return

            lock = self.state.get_reply_lock(key)
            async with lock:
                if key in self.state.REPLY_DONE:
                    return

                dedupe_key = TextUtils.make_dedupe_key(key)
                pending_log_id = 0
                try:
                    uname = getattr(sender, "username", "") or ""
                    disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name','') or '').strip()}".strip()
                    pending_log_id = await self.db.log_auto_reply_pending(
                        sender_id or 0, uname, disp_name, dedupe_key, ev.chat_id, ev.message.id
                    )
                except Exception as ex:
                    self.logger.warning(f"log_auto_reply_pending failed: {ex}")

                rate_limit_max = int(await self.db.get_setting("rate_limit_max", "4"))
                if sender_id:
                    try:
                        prev = await self.db.count_auto_replies_distinct(sender_id, hours=24)
                        if prev >= rate_limit_max:
                            uname = getattr(sender, "username", "") or ""
                            disp_name = f"{(getattr(sender, 'first_name', '') or '').strip()} {(getattr(sender, 'last_name','') or '').strip()}".strip()
                            await self.db.add_blocked_user(sender_id, uname, disp_name)
                            self.state.blocked_users[sender_id] = (uname, disp_name)
                            self.state.REPLY_DONE.add(key)
                            return
                    except Exception as ex:
                        self.logger.warning(f"auto-replies threshold check failed: {ex}")

                self.state.REPLY_DONE.add(key)
                await self.fallback.forward_any(ev)

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

                    sent_orig = await self.messenger.safe_send(b.client, tgt, text, tag=f"ORIG_REPLY|{b.phone}")
                    if not sent_orig:
                        continue

                    default_reply = await self.db.get_setting("default_auto_reply", "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك")
                    raw_reply = self.state.next_auto_reply(default_reply)
                    formatted_reply = TextUtils.format_auto_reply(raw_reply, sender)
                    sent_auto = await self.messenger.safe_send(b.client, tgt, formatted_reply, tag=f"AUTO_REPLY|{b.phone}")

                    if sent_auto:
                        try:
                            await self.db.update_auto_reply_log(pending_log_id, bot_phone=b.phone, message_id=getattr(sent_auto, "id", None))
                        except Exception as ex:
                            self.logger.warning(f"update_auto_reply_log failed: {ex}")
                        any_ok = True
                        break

                if not any_ok:
                    warn = "⚠️ لم يتم الرد تلقائيًا – سيتم المتابعة يدويًا:"
                    await self.fallback.forward_any(ev, warn_prefix=warn)

    async def on_message(self, ev: events.NewMessage.Event) -> None:
        chat_id, sender_id = ev.chat_id, ev.message.sender_id
        text = ev.message.message or ""

        # OTP handling
        for phone, otp_state in list(self.state.otp_states.items()):
            if otp_state.get("step") == "waiting_code":
                code = text.strip()
                if re.match(r"^\d{5,6}$", code):
                    asyncio.create_task(self._handle_otp_code(phone, code))
                    return
            elif otp_state.get("step") == "waiting_2fa":
                asyncio.create_task(self._handle_2fa(phone, text.strip()))
                return

        filters = self.cfg.FILTERS
        if text.startswith("✉") or ev.out:
            return
        if filters.get("private", (True, None))[0] and ev.is_private:
            return
        if filters.get("outgoing", (True, None))[0] and ev.out:
            return
        if chat_id in self.cfg.EXCLUDED_GROUPS:
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
        if filters.get("bots", (True, None))[0] and getattr(sender, "bot", False):
            return
        if filters.get("admins", (True, None))[0]:
            try:
                part = await ev.client.get_participant(chat_id, sender_id)
                if isinstance(part, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                    return
            except:
                pass

        if self.mode == "self":
            kw_re = self.cfg.build_kw_regex()
            if kw_re.search(text):
                fwd = await self.formatter.build_forward_text(ev)
                await self.messenger.safe_send(self.client, self.target_group_id, fwd, tag="SELFFWD")
            await self.unified_dispatch(ev)
        else:
            await self.unified_dispatch(ev)

    async def _handle_otp_code(self, phone: str, code: str) -> None:
        otp_state = self.state.otp_states.get(phone)
        if not otp_state:
            return
        client = otp_state["client"]
        try:
            await client.sign_in(phone, code)
            await self._finalize_account_setup(phone, client, otp_state)
        except SessionPasswordNeededError:
            self.state.otp_states[phone]["step"] = "waiting_2fa"
        except PhoneCodeInvalidError:
            self.state.otp_states.pop(phone, None)
            await client.disconnect()
        except PhoneCodeExpiredError:
            self.state.otp_states.pop(phone, None)
            await client.disconnect()
        except Exception:
            self.state.otp_states.pop(phone, None)
            await client.disconnect()

    async def _handle_2fa(self, phone: str, password: str) -> None:
        otp_state = self.state.otp_states.get(phone)
        if not otp_state:
            return
        client = otp_state["client"]
        try:
            await client.sign_in(password=password)
            await self._finalize_account_setup(phone, client, otp_state)
        except Exception:
            self.state.otp_states.pop(phone, None)
            await client.disconnect()

    async def _finalize_account_setup(self, phone: str, client: TelegramClient, otp_state: dict) -> None:
        try:
            me = await client.get_me()
            session_string = StringSession.save(client.session)
            await self.db.update_account_session(phone, session_string)
            await self.db.update_account_status(phone, AccountStatus.ACTIVE)

            bot = WorkerBot(
                cfg=self.cfg, db=self.db, state=self.state, messenger=self.messenger,
                group_repo=self.group_repo, formatter=self.formatter, fallback=self.fallback,
                client=client, target_group_id=otp_state["target_group"], phone=phone,
                mode=otp_state["mode"], logger=self.cfg.logger,
            )
            self.state.bots.append(bot)
            self.state.otp_states.pop(phone, None)
            self.logger.info(f"Account {phone} connected successfully")
        except Exception as e:
            self.logger.error(f"Failed to finalize {phone}: {e}")
            self.state.otp_states.pop(phone, None)
            await client.disconnect()

# ==================== CONTROLLER BOT (python-telegram-bot) ====================
# All commands and interactive menus are handled here

class ControllerBot:
    """The main BotFather bot that controls everything via inline keyboards and commands."""

    def __init__(self, settings: Settings, db: DB, state: State, group_repo: GroupRepo,
                 backup: DbBackupManager, cfg: Config, logger: logging.Logger):
        self.settings = settings
        self.db = db
        self.state = state
        self.group_repo = group_repo
        self.backup = backup
        self.cfg = cfg
        self.logger = logger
        self.application: Optional[Application] = None

    # ---------- AUTH ----------

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command - asks for password if not authenticated."""
        user_id = update.effective_user.id
        username = update.effective_user.username

        # Check if already owner
        is_owner = await self.db.is_owner(user_id)
        if is_owner:
            await self.show_main_menu(update, context)
            return ConversationHandler.END

        # First time - ask for password
        await update.message.reply_text(
            "🔐 **مرحباً بك في Ultimate Bot Controller!**\n\n"
            "هذا البوت للتحكم الكامل في الحسابات والإعدادات.\n"
            "يرجى إدخال كلمة المرور للمتابعة:",
            parse_mode="Markdown"
        )
        return AUTH_PASSWORD

    async def check_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check admin password."""
        user_id = update.effective_user.id
        username = update.effective_user.username
        password = update.message.text.strip()

        if password == self.settings.admin_password:
            await self.db.add_owner(user_id, username)
            await self.db.set_setting("owner_id", str(user_id), user_id)
            await update.message.reply_text(
                "✅ **تم المصادقة بنجاح!**\n\n"
                "أهلاً بك أيها المالك. استخدم القائمة التالية للتحكم.",
                parse_mode="Markdown"
            )
            await self.show_main_menu(update, context)
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                "❌ **كلمة المرور خاطئة!**\n\n"
                "حاول مرة أخرى أو تواصل مع المسؤول."
            )
            return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel any conversation."""
        await update.message.reply_text("❌ تم الإلغاء.")
        return ConversationHandler.END

    # ---------- MENU BUILDERS ----------

    def main_menu_keyboard(self):
        """Main menu inline keyboard."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 إدارة الحسابات", callback_data="menu_accounts")],
            [InlineKeyboardButton("💬 إدارة الردود", callback_data="menu_replies")],
            [InlineKeyboardButton("🔑 الكلمات المفتاحية", callback_data="menu_keywords")],
            [InlineKeyboardButton("🛡 الفلاتر والإعدادات", callback_data="menu_filters")],
            [InlineKeyboardButton("🔗 إدارة الجروبات", callback_data="menu_groups")],
            [InlineKeyboardButton("🚫 المحظورون", callback_data="menu_blocked")],
            [InlineKeyboardButton("📊 الإحصائيات والصحة", callback_data="menu_stats")],
            [InlineKeyboardButton("🗄 النسخ الاحتياطي", callback_data="menu_backup")],
            [InlineKeyboardButton("⚙️ الإعدادات المتقدمة", callback_data="menu_settings")],
            [InlineKeyboardButton("❓ مساعدة / Help", callback_data="menu_help")],
        ])

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
        """Show the main control menu."""
        text = (
            "🏠 **القائمة الرئيسية — Ultimate Bot v6.0**\n\n"
            "اختر أحد الأقسام للتحكم:\n\n"
            "📱 **الحسابات** — إضافة، تشغيل، إيقاف، حذف\n"
            "💬 **الردود** — الرد المباشر، المحظور، التلقائي\n"
            "🔑 **الكلمات المفتاحية** — إضافة، حذف، عرض\n"
            "🛡 **الفلاتر** — تجاهل الإشارات، الروابط، الأرقام...\n"
            "🔗 **الجروبات** — إضافة روابط، انضمام، حالة\n"
            "🚫 **المحظورون** — مستخدمين محظورين من الرد\n"
            "📊 **الإحصائيات** — صحة النظام والأرقام\n"
            "🗄 **النسخ الاحتياطي** — حفظ واسترجاع\n"
            "⚙️ **متقدمة** — إعدادات النظام الكاملة\n\n"
            "⬇️ اختر من الأسفل:"
        )
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=self.main_menu_keyboard())
        else:
            if update.message:
                await update.message.reply_text(text, parse_mode="Markdown", reply_markup=self.main_menu_keyboard())
            elif update.callback_query:
                await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=self.main_menu_keyboard())

    # ---------- CALLBACK ROUTER ----------

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Route all callback queries to their handlers."""
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = update.effective_user.id

        # Auth check
        if not await self.db.is_owner(user_id):
            await query.edit_message_text("⚠️ غير مصرح. ابدأ بالأمر /start")
            return

        # Main menus
        if data == "menu_main":
            await self.show_main_menu(update, context, edit=True)
        elif data == "menu_accounts":
            await self.accounts_menu(update, context)
        elif data == "menu_replies":
            await self.replies_menu(update, context)
        elif data == "menu_keywords":
            await self.keywords_menu(update, context)
        elif data == "menu_filters":
            await self.filters_menu(update, context)
        elif data == "menu_groups":
            await self.groups_menu(update, context)
        elif data == "menu_blocked":
            await self.blocked_menu(update, context)
        elif data == "menu_stats":
            await self.stats_menu(update, context)
        elif data == "menu_backup":
            await self.backup_menu(update, context)
        elif data == "menu_settings":
            await self.settings_menu(update, context)
        elif data == "menu_help":
            await self.help_command(update, context)

        # Accounts
        elif data == "account_list":
            await self.account_list(update, context)
        elif data == "account_add":
            await query.edit_message_text(
                "📱 **إضافة حساب جديد**\n\n"
                "الخطوة 1/5: أرسل رقم الهاتف مع كود الدولة\n"
                "مثال: `+966500000000`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]])
            )
            return ACC_PHONE
        elif data == "account_start_all":
            await self.account_start_all(update, context)
        elif data == "account_stop_all":
            await self.account_stop_all(update, context)
        elif data.startswith("account_start|"):
            phone = data.split("|", 1)[1]
            await self.account_start_one(update, context, phone)
        elif data.startswith("account_stop|"):
            phone = data.split("|", 1)[1]
            await self.account_stop_one(update, context, phone)
        elif data.startswith("account_remove|"):
            phone = data.split("|", 1)[1]
            await self.account_remove_one(update, context, phone)
        elif data.startswith("account_status|"):
            phone = data.split("|", 1)[1]
            await self.account_status_detail(update, context, phone)
        elif data.startswith("account_join|"):
            phone = data.split("|", 1)[1]
            await self.account_join_groups(update, context, phone)
        elif data == "account_stopjoin":
            await self.account_stopjoin(update, context)

        # Replies
        elif data == "reply_direct_list":
            await self.reply_direct_list(update, context)
        elif data == "reply_blocked_list":
            await self.reply_blocked_list(update, context)
        elif data == "reply_auto_list":
            await self.reply_auto_list(update, context)
        elif data == "reply_default_show":
            await self.reply_default_show(update, context)
        elif data == "reply_default_edit":
            await query.edit_message_text(
                "✍️ أرسل الرد الافتراضي الجديد:\n\n"
                "المتغيرات المتاحة:\n"
                "`{first_name}` — الاسم\n"
                "`{last_name}` — العائلة\n"
                "`{username}` — المعرف\n"
                "`{user_id}` — الرقم التعريفي\n\n"
                "أو اضغط رجوع للإلغاء.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_replies")]])
            )
            return EDIT_DEFAULT_REPLY

        # Keywords
        elif data == "keyword_list":
            await self.keyword_list(update, context)
        elif data == "keyword_add":
            await query.edit_message_text(
                "🔑 **إضافة كلمة مفتاحية**\n\n"
                "أرسل الكلمة أو الكلمات (كل سطر كلمة):",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_keywords")]])
            )
            return ADD_KEYWORD
        elif data == "keyword_del":
            await query.edit_message_text(
                "🔑 **حذف كلمة مفتاحية**\n\n"
                "أرسل الكلمة أو الكلمات للحذف (كل سطر كلمة):",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_keywords")]])
            )
            return DEL_KEYWORD

        # Filters
        elif data == "filter_list":
            await self.filter_list(update, context)
        elif data.startswith("filter_toggle|"):
            parts = data.split("|")
            await self.filter_toggle(update, context, parts[1], parts[2])

        # Groups
        elif data == "group_list":
            await self.group_list(update, context)
        elif data == "group_add":
            await query.edit_message_text(
                "🔗 **إضافة جروبات**\n\n"
                "أرسل روابط الجروبات (كل سطر رابط):\n"
                "مثال:\n"
                "`@groupname`\n"
                "`https://t.me/groupname`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")]])
            )
            return ADD_GROUP
        elif data == "group_del":
            await query.edit_message_text(
                "🔗 **حذف جروبات**\n\n"
                "أرسل روابط الجروبات للحذف (كل سطر رابط):",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")]])
            )
            return DEL_GROUP
        elif data == "group_usergroups":
            await self.group_usergroups_menu(update, context)
        elif data == "group_join":
            await self.group_join_menu(update, context)

        # Blocked
        elif data == "blocked_list":
            await self.blocked_list(update, context)
        elif data == "blocked_add":
            await query.edit_message_text(
                "🚫 **حظر مستخدم**\n\n"
                "أرسل: `user_id [username] [display_name]`\n"
                "أو فقط `user_id`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]])
            )
            return ADD_BLKUSER
        elif data == "blocked_del":
            await query.edit_message_text(
                "🚫 **إلغاء حظر مستخدم**\n\n"
                "أرسل user_id للإلغاء:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]])
            )
            return DEL_BLKUSER
        elif data == "blocked_find":
            await query.edit_message_text(
                "🔎 **بحث في المحظورين**\n\n"
                "أرسل نمط البحث:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]])
            )
            # Simple text search - not a conversation state, handled in message handler
            context.user_data["blocked_find_mode"] = True

        # Stats
        elif data == "stats_full":
            await self.stats_full(update, context)
        elif data == "health_check":
            await self.health_check(update, context)
        elif data == "queue_status":
            await self.queue_status(update, context)
        elif data == "autoreplies_list":
            await self.autoreplies_list(update, context)

        # Backup
        elif data == "backup_export":
            await self.backup_export(update, context)
        elif data == "backup_import":
            await query.edit_message_text(
                "🗄 **استرجاع نسخة احتياطية**\n\n"
                "أرسل ملف النسخة الاحتياطية (.json.gz)\n"
                "أو اضغط رجوع لاسترجاع آخر نسخة محلية.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📂 استرجاع آخر نسخة محلية", callback_data="backup_import_last")],
                    [InlineKeyboardButton("🔙 رجوع", callback_data="menu_backup")]
                ])
            )

        # Settings
        elif data == "settings_list":
            await self.settings_list(update, context)
        elif data == "settings_edit":
            await query.edit_message_text(
                "⚙️ **تعديل إعداد**\n\n"
                "أرسل: `key value`\n"
                "مثال: `rate_limit_max 6`\n\n"
                "لعرض القائمة: /config_list",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]])
            )
            return EDIT_SETTING_KEY
        elif data == "settings_reset":
            await query.edit_message_text(
                "⚙️ **إعادة تعيين إعداد**\n\n"
                "أرسل اسم الإعداد للإعادة:\n"
                "مثال: `rate_limit_max`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]])
            )
            context.user_data["settings_reset_mode"] = True

    # ==================== ACCOUNTS MENU ====================

    async def accounts_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        kb = [
            [InlineKeyboardButton("📋 عرض الحسابات", callback_data="account_list")],
            [InlineKeyboardButton("➕ إضافة حساب جديد", callback_data="account_add")],
            [InlineKeyboardButton("▶️ تشغيل الكل", callback_data="account_start_all")],
            [InlineKeyboardButton("⏸️ إيقاف الكل", callback_data="account_stop_all")],
            [InlineKeyboardButton("🛑 إيقاف الانضمام", callback_data="account_stopjoin")],
            [InlineKeyboardButton("🔗 حالة الجروبات", callback_data="group_usergroups")],
            [InlineKeyboardButton("👥 انضمام لجروبات", callback_data="group_join")],
            [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="menu_main")],
        ]
        await query.edit_message_text(
            "📱 **إدارة الحسابات**\n\n"
            "• عرض الحسابات وإدارتها\n"
            "• إضافة حساب جديد (خطوة بخطوة)\n"
            "• تشغيل/إيقاف الكل أو فردي\n"
            "• الانضمام للجروبات\n"
            "• حالة الجروبات لكل حساب",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def account_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all accounts with action buttons."""
        query = update.callback_query
        accounts = await self.db.get_all_accounts()
        if not accounts:
            kb = [[InlineKeyboardButton("➕ إضافة حساب", callback_data="account_add")],
                  [InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]
            await query.edit_message_text("— لا توجد حسابات —", reply_markup=InlineKeyboardMarkup(kb))
            return

        lines = ["📱 **قائمة الحسابات:**\n"]
        kb = []
        for acc in accounts:
            status_emoji = {"active": "🟢", "pending": "⏳", "paused": "⏸️",
                           "error": "🔴", "banned": "🚫", "flood": "⏳", "connecting": "🔄"}.get(acc.status.value, "⚪")
            connected = acc.last_connected.strftime("%Y-%m-%d %H:%M") if acc.last_connected else "—"
            lines.append(
                f"{status_emoji} **{acc.phone}**\n"
                f"   الحالة: `{acc.status.value}` | الوضع: `{acc.mode}`\n"
                f"   آخر اتصال: `{connected}`\n"
                f"   ————————————————"
            )
            # Action buttons per account
            kb.append([
                InlineKeyboardButton(f"▶️ {acc.phone}", callback_data=f"account_start|{acc.phone}"),
                InlineKeyboardButton(f"⏸️ إيقاف", callback_data=f"account_stop|{acc.phone}"),
                InlineKeyboardButton(f"🗑 حذف", callback_data=f"account_remove|{acc.phone}"),
            ])
            kb.append([
                InlineKeyboardButton(f"📊 حالة", callback_data=f"account_status|{acc.phone}"),
                InlineKeyboardButton(f"🔗 انضمام", callback_data=f"account_join|{acc.phone}"),
            ])

        kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")])
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def account_start_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.edit_message_text("⏳ جاري تشغيل كل الحسابات...")
        accounts = await self.db.get_all_accounts()
        started = 0
        for acc in accounts:
            if acc.status == AccountStatus.ACTIVE and acc.session_string:
                existing = next((b for b in self.state.bots if b.phone == acc.phone), None)
                if existing:
                    continue
                try:
                    client = TelegramClient(StringSession(acc.session_string), acc.api_id, acc.api_hash)
                    await client.connect()
                    if await client.is_user_authorized():
                        bot = WorkerBot(
                            cfg=self.cfg, db=self.db, state=self.state, messenger=Messenger(self.logger),
                            group_repo=self.group_repo, formatter=MessageFormatter(self.cfg),
                            fallback=FallbackRouter(self.cfg, self.state, Messenger(self.logger), MessageFormatter(self.cfg), self.logger),
                            client=client, target_group_id=acc.target_group_id, phone=acc.phone,
                            mode=acc.mode, logger=self.logger,
                        )
                        self.state.bots.append(bot)
                        await self.db.update_account_status(acc.phone, AccountStatus.ACTIVE)
                        started += 1
                    else:
                        await client.disconnect()
                except Exception as e:
                    await self.db.update_account_status(acc.phone, AccountStatus.ERROR, str(e))
        await query.edit_message_text(f"✅ تم تشغيل {started} حساب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))

    async def account_stop_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        count = len(self.state.bots)
        for b in self.state.bots:
            try:
                await b.client.disconnect()
            except:
                pass
        self.state.bots.clear()
        accounts = await self.db.get_all_accounts()
        for acc in accounts:
            if acc.status == AccountStatus.ACTIVE:
                await self.db.update_account_status(acc.phone, AccountStatus.PAUSED)
        await query.edit_message_text(f"⏸️ تم إيقاف {count} حساب.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))

    async def account_start_one(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
        query = update.callback_query
        acc = await self.db.get_account(phone)
        if not acc:
            await query.edit_message_text(f"⚠️ الحساب غير موجود: {phone}")
            return
        existing = next((b for b in self.state.bots if b.phone == phone), None)
        if existing:
            await query.edit_message_text(f"ℹ️ {phone} يعمل بالفعل.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))
            return
        session = await self.db.load_session(phone)
        if not session:
            await query.edit_message_text(f"❌ لا يوجد session. أعد الإضافة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))
            return
        try:
            client = TelegramClient(StringSession(session), acc.api_id, acc.api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                await query.edit_message_text("❌ Session غير صالح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))
                return
            bot = WorkerBot(
                cfg=self.cfg, db=self.db, state=self.state, messenger=Messenger(self.logger),
                group_repo=self.group_repo, formatter=MessageFormatter(self.cfg),
                fallback=FallbackRouter(self.cfg, self.state, Messenger(self.logger), MessageFormatter(self.cfg), self.logger),
                client=client, target_group_id=acc.target_group_id, phone=phone, mode=acc.mode, logger=self.logger,
            )
            self.state.bots.append(bot)
            await self.db.update_account_status(phone, AccountStatus.ACTIVE)
            me = await client.get_me()
            await query.edit_message_text(
                f"✅ **{phone}** تم التشغيل!\n👤 {me.first_name or ''} {me.last_name or ''}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]])
            )
        except Exception as e:
            await self.db.update_account_status(phone, AccountStatus.ERROR, str(e))
            await query.edit_message_text(f"❌ فشل: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))

    async def account_stop_one(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
        query = update.callback_query
        bot = next((b for b in self.state.bots if b.phone == phone), None)
        if not bot:
            await query.edit_message_text(f"⚠️ {phone} لا يعمل حاليًا.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))
            return
        try:
            await bot.client.disconnect()
            self.state.bots.remove(bot)
            await self.db.update_account_status(phone, AccountStatus.PAUSED)
            await query.edit_message_text(f"⏸️ **{phone}** تم الإيقاف.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))
        except Exception as e:
            await query.edit_message_text(f"❌ {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))

    async def account_remove_one(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
        query = update.callback_query
        bot = next((b for b in self.state.bots if b.phone == phone), None)
        if bot:
            try:
                await bot.client.disconnect()
            except:
                pass
            self.state.bots.remove(bot)
        await self.db.delete_account(phone)
        await query.edit_message_text(f"🗑️ **{phone}** تم الحذف نهائيًا.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))

    async def account_status_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
        query = update.callback_query
        acc = await self.db.get_account(phone)
        if not acc:
            await query.edit_message_text("⚠️ غير موجود.")
            return
        msg = (
            f"📱 **{phone}**\n"
            f"الحالة: `{acc.status.value}`\n"
            f"المجموعة: `{acc.target_group_id}`\n"
            f"الوضع: `{acc.mode}`\n"
            f"🕒 آخر خطأ: `{acc.last_error or '—'}`"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))

    async def account_join_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
        query = update.callback_query
        target_bot = next((b for b in self.state.bots if b.phone == phone), None)
        if not target_bot:
            await query.edit_message_text(f"⚠️ {phone} غير نشط. شغله أولاً.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="account_list")]]))
            return
        self.state.stop_joining_flags[phone] = False
        t = asyncio.create_task(target_bot.join_groups_with_account(0))
        self.state.joining_now[phone] = t
        await query.edit_message_text(f"🚀 بدأ {phone} بالانضمام...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹ إيقاف الانضمام", callback_data="account_stopjoin")]]))

    async def account_stopjoin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not self.state.joining_now:
            await query.edit_message_text("لا توجد عمليات انضمام حالية.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            return
        for p in list(self.state.joining_now):
            self.state.stop_joining_flags[p] = True
        await query.edit_message_text("⏹ تم إيقاف كل عمليات الانضمام.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))

    # ==================== REPLIES MENU ====================

    async def replies_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        kb = [
            [InlineKeyboardButton("📋 عرض الردود المباشرة", callback_data="reply_direct_list")],
            [InlineKeyboardButton("📋 عرض الردود المحظورة", callback_data="reply_blocked_list")],
            [InlineKeyboardButton("📋 عرض الردود التلقائية", callback_data="reply_auto_list")],
            [InlineKeyboardButton("💬 عرض الرد الافتراضي", callback_data="reply_default_show")],
            [InlineKeyboardButton("✍️ تعديل الرد الافتراضي", callback_data="reply_default_edit")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(
            "💬 **إدارة الردود**\n\n"
            "الردود هي الرسائل التي يرسلها البوت:\n\n"
            "• **الرد المباشر** — الجمل التي تستدعي الرد التلقائي\n"
            "• **الرد المحظور** — الجمل التي تمنع الرد\n"
            "• **الرد التلقائي** — رسائل الرد المتناوبة\n"
            "• **الرد الافتراضي** — الرسالة الأساسية",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def reply_direct_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        items = self.state.direct_triggers
        if not items:
            text = "— لا توجد ردود مباشرة —"
        else:
            text = "📋 **الردود المباشرة:**\n\n" + "\n".join(f"{i+1}. `{s}`" for i, s in enumerate(items))
        kb = [
            [InlineKeyboardButton("➕ إضافة", callback_data="reply_direct_add"), InlineKeyboardButton("🗑 حذف", callback_data="reply_direct_del")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_replies")]
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def reply_blocked_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        items = self.state.blocked_phrases
        if not items:
            text = "— لا توجد ردود محظورة —"
        else:
            text = "📋 **الردود المحظورة:**\n\n" + "\n".join(f"{i+1}. `{s}`" for i, s in enumerate(items))
        kb = [
            [InlineKeyboardButton("➕ إضافة", callback_data="reply_blocked_add"), InlineKeyboardButton("🗑 حذف", callback_data="reply_blocked_del")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_replies")]
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def reply_auto_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        items = self.state.auto_replies
        if not items:
            text = "— لا توجد ردود تلقائية —"
        else:
            text = "📋 **الردود التلقائية:**\n\n" + "\n".join(f"{i+1}. `{s}`" for i, s in enumerate(items))
        kb = [
            [InlineKeyboardButton("➕ إضافة", callback_data="reply_auto_add"), InlineKeyboardButton("🗑 حذف", callback_data="reply_auto_del")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_replies")]
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def reply_default_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        current = await self.db.get_setting("default_auto_reply", "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك")
        text = f"💬 **الرد الافتراضي الحالي:**\n`{current}`\n\nالمتغيرات: `{{first_name}}`, `{{last_name}}`, `{{username}}`, `{{user_id}}`"
        kb = [[InlineKeyboardButton("✍️ تعديل", callback_data="reply_default_edit")],
              [InlineKeyboardButton("🔙 رجوع", callback_data="menu_replies")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ==================== KEYWORDS MENU ====================

    async def keywords_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        keywords = await self.db.get_keywords()
        text = f"🔑 **الكلمات المفتاحية** ({len(keywords)} كلمة)\n\n" + "\n".join(f"• `{k}`" for k in keywords[:30])
        if len(keywords) > 30:
            text += f"\n... و {len(keywords) - 30} أخرى"
        kb = [
            [InlineKeyboardButton("📋 عرض الكل", callback_data="keyword_list"),
             InlineKeyboardButton("➕ إضافة", callback_data="keyword_add"),
             InlineKeyboardButton("🗑 حذف", callback_data="keyword_del")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def keyword_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        rows = await self.db.list_keywords()
        if not rows:
            text = "— لا توجد كلمات مفتاحية —"
        else:
            lines = ["🔑 **الكلمات المفتاحية:**\n"]
            for r in rows:
                lines.append(f"• `{r['word']}` — الفئة: {r['category']}")
            text = "\n".join(lines)
        kb = [
            [InlineKeyboardButton("➕ إضافة", callback_data="keyword_add"), InlineKeyboardButton("🗑 حذف", callback_data="keyword_del")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_keywords")]
        ]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ==================== FILTERS MENU ====================

    async def filters_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        filters_data = await self.db.get_filters()
        lines = ["🛡 **فلاتر التجاهل:**\n"]
        kb = []
        for name, (active, threshold) in filters_data.items():
            emoji = "✅" if active else "❌"
            thresh = f" (حد: {threshold})" if threshold else ""
            lines.append(f"{emoji} `{name}`{thresh}")
            kb.append([
                InlineKeyboardButton(f"{emoji} {name}{thresh}", callback_data=f"filter_toggle|{name}|{'off' if active else 'on'}")
            ])
        kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")])
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def filter_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.filters_menu(update, context)

    async def filter_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, new_state: str):
        query = update.callback_query
        is_active = new_state == "on"
        await self.db.toggle_filter(name, is_active)
        await self.cfg.refresh()
        status = "✅ مفعل" if is_active else "❌ معطل"
        await query.answer(f"{status}: {name}")
        await self.filters_menu(update, context)

    # ==================== GROUPS MENU ====================

    async def groups_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        links = await self.group_repo.all()
        kb = [
            [InlineKeyboardButton("📋 عرض القائمة", callback_data="group_list"),
             InlineKeyboardButton(f"📊 العدد: {len(links)}", callback_data="group_count")],
            [InlineKeyboardButton("➕ إضافة جروبات", callback_data="group_add"),
             InlineKeyboardButton("🗑 حذف جروبات", callback_data="group_del")],
            [InlineKeyboardButton("👥 حالة الحسابات في الجروبات", callback_data="group_usergroups")],
            [InlineKeyboardButton("🔗 انضمام لجروبات", callback_data="group_join")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(
            "🔗 **إدارة الجروبات**\n\n"
            "• عرض/إضافة/حذف روابط الجروبات\n"
            "• معرفة الحسابات المنتسبة لكل جروب\n"
            "• الانضمام لجروبات من حساب معين",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def group_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        links = await self.group_repo.all()
        if not links:
            text = "لا توجد روابط جروبات."
        else:
            text = "🔗 **روابط الجروبات:**\n\n" + "\n".join(f"{i+1}. {lnk}" for i, lnk in enumerate(links))
        kb = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def group_usergroups_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not self.state.bots:
            await query.edit_message_text("لا توجد حسابات نشطة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")]]))
            return
        kb = []
        for b in self.state.bots:
            kb.append([InlineKeyboardButton(b.phone, callback_data=f"usergroups|{b.phone}")])
        kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")])
        await query.edit_message_text("👥 اختر حساب لعرض حالة الجروبات:", reply_markup=InlineKeyboardMarkup(kb))

    async def group_join_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not self.state.bots:
            await query.edit_message_text("لا توجد حسابات نشطة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")]]))
            return
        kb = []
        for b in self.state.bots:
            kb.append([InlineKeyboardButton(b.phone, callback_data=f"account_join|{b.phone}")])
        kb.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")])
        await query.edit_message_text("🔗 اختر حساب للانضمام:", reply_markup=InlineKeyboardMarkup(kb))

    # ==================== BLOCKED MENU ====================

    async def blocked_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        kb = [
            [InlineKeyboardButton("📋 عرض المحظورين", callback_data="blocked_list")],
            [InlineKeyboardButton("➕ حظر مستخدم", callback_data="blocked_add")],
            [InlineKeyboardButton("🗑 إلغاء حظر", callback_data="blocked_del")],
            [InlineKeyboardButton("🔎 بحث", callback_data="blocked_find")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(
            "🚫 **المستخدمون المحظورون**\n\n"
            "المستخدمون المحظورون لا يستقبلون ردود تلقائية.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def blocked_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        rows = await self.db.list_blocked_users()
        if not rows:
            text = "— لا يوجد محظورون —"
        else:
            lines = [f"🔹 #{i+1} | `{r['user_id']}` | @{r['username'] or '—'} | {r['display_name'] or '—'}" for i, r in enumerate(rows)]
            text = "🚫 **المحظورون:**\n\n" + "\n".join(lines)
        kb = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ==================== STATS MENU ====================

    async def stats_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        kb = [
            [InlineKeyboardButton("📊 الإحصائيات الكاملة", callback_data="stats_full")],
            [InlineKeyboardButton("🏥 فحص الصحة", callback_data="health_check")],
            [InlineKeyboardButton("📋 حالة المهام", callback_data="queue_status")],
            [InlineKeyboardButton("📒 سجل الردود", callback_data="autoreplies_list")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(
            "📊 **الإحصائيات والصحة**\n\n"
            "• إحصائيات كاملة للنظام\n"
            "• فحص صحة قاعدة البيانات\n"
            "• حالة مهام الخلفية\n"
            "• سجل الردود التلقائية",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def stats_full(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        s = await self.db.get_stats()
        msg = (
            "📊 **ملخص النظام:**\n\n"
            f"• 🟢 ردود مباشرة: **{s['direct']}**\n"
            f"• ⛔ ردود محظورة: **{s['blocked_text']}**\n"
            f"• 🚫 محظورون: **{s['blocked_users']}**\n"
            f"• 🔗 جروبات: **{s['groups']}**\n"
            f"• 🔑 كلمات مفتاحية: **{s['keywords']}**\n"
            f"• 🚫 مجموعات مستثناة: **{s['excluded_groups']}**\n"
            f"• 📱 إجمالي حسابات: **{s['accounts']}**\n"
            f"• 🟢 حسابات نشطة: **{s['active_accounts']}**\n"
            f"• 📋 مهام قيد الانتظار: **{s['pending_tasks']}**"
        )
        kb = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_stats")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def health_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        db_healthy = await self.db.health_check()
        bots_status = [
            f"{'🟢' if b.client.is_connected() else '🔴'} {b.phone} ({b.mode})"
            for b in self.state.bots
        ]
        msg = (
            f"🏥 **حالة النظام**\n\n"
            f"🗄 قاعدة البيانات: {'✅ سليمة' if db_healthy else '❌ خطأ'}\n"
            f"🤖 الحسابات النشطة: {len(self.state.bots)}\n"
            f"{' | '.join(bots_status)}" if bots_status else "— لا توجد حسابات —"
        )
        kb = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_stats")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def queue_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        async with self.db.pool.acquire() as conn:
            pending = await conn.fetchval("SELECT COUNT(*) FROM task_queue WHERE status='pending'") or 0
            processing = await conn.fetchval("SELECT COUNT(*) FROM task_queue WHERE status='processing'") or 0
            completed = await conn.fetchval("SELECT COUNT(*) FROM task_queue WHERE status='completed'") or 0
        msg = (
            f"📋 **حالة المهام**\n\n"
            f"⏳ قيد الانتظار: {pending}\n"
            f"🔄 قيد المعالجة: {processing}\n"
            f"✅ مكتملة: {completed}"
        )
        kb = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_stats")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))

    async def autoreplies_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        rows = await self.db.list_auto_replies(limit=50)
        if not rows:
            text = "— لا يوجد سجلات —"
        else:
            lines = [f"#{r['id']} | `{r['user_id']}` | 🤖 {r['bot_phone'] or '—'} | 🕒 {r['created_at']}" for r in rows]
            text = "📒 **سجل الردود التلقائية:**\n\n" + "\n".join(lines)
        kb = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_stats")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ==================== BACKUP MENU ====================

    async def backup_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        kb = [
            [InlineKeyboardButton("💾 تصدير نسخة", callback_data="backup_export")],
            [InlineKeyboardButton("📂 استرجاع نسخة", callback_data="backup_import")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(
            "🗄 **النسخ الاحتياطي**\n\n"
            "• تصدير: حفظ كامل البيانات\n"
            "• استرجاع: استعادة من ملف محلي أو مرسل",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def backup_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.edit_message_text("⏳ جاري إنشاء النسخة الاحتياطية...")
        try:
            ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            out_path = f"backups/db_{ts}.json.gz"
            await self.backup.export_json_gz(out_path)
            await query.edit_message_text(
                f"✅ تم الإنشاء: `{out_path}`\n\n"
                f"الملف محفوظ محلياً. يمكنك نسخه يدوياً.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_backup")]])
            )
        except Exception as e:
            await query.edit_message_text(f"❌ فشل: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_backup")]]))

    # ==================== SETTINGS MENU ====================

    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        kb = [
            [InlineKeyboardButton("📋 عرض الإعدادات", callback_data="settings_list")],
            [InlineKeyboardButton("✍️ تعديل إعداد", callback_data="settings_edit")],
            [InlineKeyboardButton("🔄 إعادة تعيين", callback_data="settings_reset")],
            [InlineKeyboardButton("📍 مجموعة Fallback", callback_data="settings_fallback")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ]
        await query.edit_message_text(
            "⚙️ **الإعدادات المتقدمة**\n\n"
            "التحكم الكامل في إعدادات النظام:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def settings_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        settings = await self.db.get_all_settings()
        lines = ["⚙️ **الإعدادات الحالية:**\n"]
        for s in settings:
            lines.append(f"• `{s['key']}` = `{s['value']}`\n  _{s['description'] or '—'}_")
        text = "\n".join(lines[:50])
        if len(lines) > 50:
            text += f"\n... و {len(lines) - 50} أخرى"
        kb = [[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    # ==================== HELP COMMAND ====================

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed help explaining every command."""
        help_text = (
            "📖 **دليل أوامر Ultimate Bot Controller**\n\n"
            "═══════════════════════════════════════\n\n"
            "**🔐 /start** — بدء البوت وإدخال كلمة المرور\n"
            "  - أول مرة: يطلب كلمة المرور\n"
            "  - بعدها: يفتح القائمة الرئيسية\n\n"
            "**🏠 القائمة الرئيسية** — تحتوي 10 أقسام:\n\n"
            "📱 **1. إدارة الحسابات**\n"
            "  • عرض الحسابات — قائمة بكل الحسابات وحالتها\n"
            "  • إضافة حساب — معالج خطوة بخطوة (هاتف، api_id، api_hash، مجموعة، وضع، كود OTP)\n"
            "  • تشغيل/إيقاف الكل — تشغيل أو إيقاف جميع الحسابات\n"
            "  • تشغيل/إيقاف فردي — لكل حساب على حدة\n"
            "  • حذف حساب — حذف نهائي من قاعدة البيانات\n"
            "  • حالة مفصلة — حالة، مجموعة، وضع، آخر خطأ\n"
            "  • انضمام لجروبات — انضمام حساب لكل الجروبات المخزنة\n"
            "  • إيقاف الانضمام — إيقاف عمليات الانضمام الجارية\n\n"
            "💬 **2. إدارة الردود**\n"
            "  • الرد المباشر — جمل تستدعي الرد التلقائي\n"
            "  • الرد المحظور — جمل تمنع الرد تماماً\n"
            "  • الرد التلقائي — رسائل متناوبة ترسل للمستخدم\n"
            "  • الرد الافتراضي — الرسالة الأساسية مع متغيرات {first_name}...\n\n"
            "🔑 **3. الكلمات المفتاحية**\n"
            "  • كلمات يتم البحث عنها في الرسائل\n"
            "  • إضافة/حذف/عرض الكل\n\n"
            "🛡 **4. الفلاتر والإعدادات**\n"
            "  • mention — تجاهل رسائل بإشارات @\n"
            "  • links — تجاهل رسائل بروابط\n"
            "  • digits — تجاهل رسائل بأرقام\n"
            "  • private — تجاهل المحادثات الخاصة\n"
            "  • outgoing — تجاهل الرسائل الصادرة\n"
            "  • bots — تجاهل البوتات\n"
            "  • admins — تجاهل المشرفين\n"
            "  • word_count — حد عدد الكلمات\n\n"
            "🔗 **5. إدارة الجروبات**\n"
            "  • عرض/إضافة/حذف روابط الجروبات\n"
            "  • حالة الحسابات في الجروبات\n"
            "  • الانضمام لجروبات\n\n"
            "🚫 **6. المحظورون**\n"
            "  • مستخدمون محظورون من الرد التلقائي\n"
            "  • إضافة/حذف/بحث\n\n"
            "📊 **7. الإحصائيات**\n"
            "  • إحصائيات كاملة للنظام\n"
            "  • فحص صحة DB والحسابات\n"
            "  • حالة المهام\n"
            "  • سجل الردود التلقائية\n\n"
            "🗄 **8. النسخ الاحتياطي**\n"
            "  • تصدير كامل البيانات\n"
            "  • استرجاع من نسخة\n\n"
            "⚙️ **9. الإعدادات المتقدمة**\n"
            "  • rate_limit_max — حد الردود لكل مستخدم\n"
            "  • rate_limit_window — فترة الحد\n"
            "  • cb_failure_threshold — حد أخطاء الانضمام\n"
            "  • join_delay_base — تأخير الانضمام\n"
            "  • fallback_group_id — مجموعة الاحتياط\n"
            "  • والمزيد...\n\n"
            "❓ **10. المساعدة**\n"
            "  • هذا الدليل\n\n"
            "═══════════════════════════════════════\n"
            "🎯 **كل شيء قابل للتحكم عبر البوت!**"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(help_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")]]))
        else:
            await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_main")]]))

    # ==================== CONVERSATION HANDLERS ====================

    # ---- Account Add Wizard ----
    async def acc_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data["acc_phone"] = update.message.text.strip()
        await update.message.reply_text("الخطوة 2/5: أرسل الـ `api_id` (رقم):", parse_mode="Markdown")
        return ACC_API_ID

    async def acc_api_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data["acc_api_id"] = int(update.message.text.strip())
            await update.message.reply_text("الخطوة 3/5: أرسل الـ `api_hash`:", parse_mode="Markdown")
            return ACC_API_HASH
        except ValueError:
            await update.message.reply_text("❌ يجب أن يكون رقماً. حاول مرة أخرى:")
            return ACC_API_ID

    async def acc_api_hash(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data["acc_api_hash"] = update.message.text.strip()
        await update.message.reply_text("الخطوة 4/5: أرسل `target_group_id` (رقم المجموعة):\nمثال: `-1001234567890`", parse_mode="Markdown")
        return ACC_TARGET_GROUP

    async def acc_target_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            context.user_data["acc_target_group"] = int(update.message.text.strip())
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("forward", callback_data="acc_mode|forward"),
                 InlineKeyboardButton("reply", callback_data="acc_mode|reply"),
                 InlineKeyboardButton("both", callback_data="acc_mode|both")]
            ])
            await update.message.reply_text("الخطوة 5/5: اختر الوضع:", reply_markup=kb)
            return ACC_MODE
        except ValueError:
            await update.message.reply_text("❌ يجب أن يكون رقماً. حاول مرة أخرى:")
            return ACC_TARGET_GROUP

    async def acc_mode_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        mode = query.data.split("|")[1]
        context.user_data["acc_mode"] = mode

        phone = context.user_data["acc_phone"]
        api_id = context.user_data["acc_api_id"]
        api_hash = context.user_data["acc_api_hash"]
        target_group = context.user_data["acc_target_group"]

        await self.db.add_account(phone, api_id, api_hash, target_group, mode)
        await query.edit_message_text(f"⏳ جاري الاتصال بـ {phone}...")

        try:
            client = TelegramClient(StringSession(), api_id, api_hash)
            await client.connect()
            await client.send_code_request(phone)

            self.state.otp_states[phone] = {
                "client": client, "step": "waiting_code",
                "api_id": api_id, "api_hash": api_hash,
                "target_group": target_group, "mode": mode,
            }
            await query.edit_message_text(
                f"📨 تم إرسال الكود إلى {phone}.\n"
                f"أرسل الكود هنا (5-6 أرقام):\n\n"
                f"إذا كان الحساب يحتاج 2FA، ستأتي خطوة إضافية بعد الكود.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_accounts")]])
            )
            return ACC_CODE
        except PhoneNumberInvalidError:
            await query.edit_message_text(f"❌ رقم غير صالح.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            return ConversationHandler.END
        except Exception as e:
            await query.edit_message_text(f"❌ خطأ: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            return ConversationHandler.END

    async def acc_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        code = update.message.text.strip()
        phone = context.user_data["acc_phone"]
        otp_state = self.state.otp_states.get(phone)
        if not otp_state:
            await update.message.reply_text("❌ انتهت الجلسة. ابدأ من جديد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            return ConversationHandler.END
        client = otp_state["client"]
        try:
            await client.sign_in(phone, code)
            await self._finalize_account(phone, otp_state, update, context)
            return ConversationHandler.END
        except SessionPasswordNeededError:
            await update.message.reply_text("🔐 الحساب يحتاج رمز 2FA. أرسل كلمة المرور:")
            return ACC_2FA
        except PhoneCodeInvalidError:
            await update.message.reply_text("❌ الكود غير صحيح. أعد المحاولة:")
            return ACC_CODE
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            self.state.otp_states.pop(phone, None)
            return ConversationHandler.END

    async def acc_2fa(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        password = update.message.text.strip()
        phone = context.user_data["acc_phone"]
        otp_state = self.state.otp_states.get(phone)
        if not otp_state:
            await update.message.reply_text("❌ انتهت الجلسة.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            return ConversationHandler.END
        client = otp_state["client"]
        try:
            await client.sign_in(password=password)
            await self._finalize_account(phone, otp_state, update, context)
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ في 2FA: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            self.state.otp_states.pop(phone, None)
            return ConversationHandler.END

    async def _finalize_account(self, phone: str, otp_state: dict, update: Update, context: ContextTypes.DEFAULT_TYPE):
        client = otp_state["client"]
        try:
            me = await client.get_me()
            session_string = StringSession.save(client.session)
            await self.db.update_account_session(phone, session_string)
            await self.db.update_account_status(phone, AccountStatus.ACTIVE)

            bot = WorkerBot(
                cfg=self.cfg, db=self.db, state=self.state, messenger=Messenger(self.logger),
                group_repo=self.group_repo, formatter=MessageFormatter(self.cfg),
                fallback=FallbackRouter(self.cfg, self.state, Messenger(self.logger), MessageFormatter(self.cfg), self.logger),
                client=client, target_group_id=otp_state["target_group"], phone=phone,
                mode=otp_state["mode"], logger=self.logger,
            )
            self.state.bots.append(bot)
            self.state.otp_states.pop(phone, None)

            await update.message.reply_text(
                f"✅ **{phone} تم الاتصال بنجاح!**\n"
                f"👤 الاسم: {me.first_name or ''} {me.last_name or ''}\n"
                f"🆔 ID: {me.id}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 الحسابات", callback_data="menu_accounts")]])
            )
        except Exception as e:
            await update.message.reply_text(f"❌ فشل التفعيل: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")]]))
            self.state.otp_states.pop(phone, None)
            await client.disconnect()

    # ---- Keyword Add ----
    async def add_keyword_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
        results = []
        for word in lines:
            await self.db.add_keyword(word)
            await self.cfg.refresh()
            results.append(f"✅ `{word}`")
        await update.message.reply_text(
            "🔑 **تم الإضافة:**\n" + "\n".join(results),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_keywords")]])
        )
        return ConversationHandler.END

    async def del_keyword_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
        results = []
        for word in lines:
            await self.db.remove_keyword(word)
            results.append(f"🗑 `{word}`")
        await self.cfg.refresh()
        await update.message.reply_text(
            "🔑 **تم الحذف:**\n" + "\n".join(results),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_keywords")]])
        )
        return ConversationHandler.END

    # ---- Group Add/Del ----
    async def add_group_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
        results = []
        for link in lines:
            await self.group_repo.add(link)
            results.append(f"✅ {link}")
        await update.message.reply_text(
            "🔗 **تم الإضافة:**\n" + "\n".join(results),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")]])
        )
        return ConversationHandler.END

    async def del_group_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
        results = []
        for link in lines:
            await self.group_repo.delete(link)
            results.append(f"🗑 {link}")
        await update.message.reply_text(
            "🔗 **تم الحذف:**\n" + "\n".join(results),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_groups")]])
        )
        return ConversationHandler.END

    # ---- Blocked User Add/Del ----
    async def add_blocked_user_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        parts = update.message.text.strip().split()
        if not parts:
            await update.message.reply_text("❌ صيغة غير صحيحة.")
            return ConversationHandler.END
        try:
            uid = int(parts[0])
            uname = parts[1] if len(parts) > 1 else ""
            dname = " ".join(parts[2:]) if len(parts) > 2 else ""
            await self.db.add_blocked_user(uid, uname, dname)
            self.state.blocked_users[uid] = (uname, dname)
            await update.message.reply_text(
                f"✅ أُضيف للمحظورين: `{uid}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]])
            )
        except ValueError:
            await update.message.reply_text("❌ user_id يجب أن يكون رقماً.")
        return ConversationHandler.END

    async def del_blocked_user_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            uid = int(update.message.text.strip())
            await self.db.del_blocked_user(uid)
            self.state.blocked_users.pop(uid, None)
            await update.message.reply_text(
                f"✅ أُزيل من المحظورين: `{uid}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]])
            )
        except ValueError:
            await update.message.reply_text("❌ user_id يجب أن يكون رقماً.")
        return ConversationHandler.END

    # ---- Default Reply Edit ----
    async def edit_default_reply_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        await self.db.set_setting("default_auto_reply", text, update.effective_user.id)
        await update.message.reply_text(
            f"✅ **تم تحديث الرد الافتراضي:**\n`{text}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_replies")]])
        )
        return ConversationHandler.END

    # ---- Settings Edit ----
    async def edit_setting_key(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        parts = update.message.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ الصيغة: `key value`", parse_mode="Markdown")
            return ConversationHandler.END
        key, value = parts[0], parts[1]
        await self.db.set_setting(key, value, update.effective_user.id)
        await self.cfg.refresh()
        await update.message.reply_text(
            f"✅ تم تحديث `{key}` = `{value}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]])
        )
        return ConversationHandler.END

    # ---- Message Handler for text commands ----
    async def text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages that aren't part of a conversation."""
        user_id = update.effective_user.id
        if not await self.db.is_owner(user_id):
            return

        text = update.message.text.strip()

        # Blocked user find mode
        if context.user_data.get("blocked_find_mode"):
            context.user_data["blocked_find_mode"] = False
            rows = await self.db.find_blocked_users(text)
            if not rows:
                await update.message.reply_text("— لا توجد مطابقة —", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]]))
            else:
                msg = "\n".join([f"- `{r['user_id']}` @{r['username'] or '—'} | {r['display_name'] or '—'}" for r in rows])
                await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_blocked")]]))
            return

        # Settings reset mode
        if context.user_data.get("settings_reset_mode"):
            context.user_data["settings_reset_mode"] = False
            key = text
            success = await self.db.reset_setting(key)
            if success:
                await self.cfg.refresh()
                await update.message.reply_text(f"✅ تم إعادة `{key}` للقيمة الافتراضية.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]]))
            else:
                await update.message.reply_text(f"❌ `{key}` ليس إعداداً معروفاً.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")]]))
            return

        # Handle usergroups callback data from text
        if text.startswith("/"):
            # Could implement command shortcuts here
            pass

    # ==================== SETUP ====================

    def setup(self, application: Application):
        """Register all handlers."""
        self.application = application

        # Auth conversation
        auth_conv = ConversationHandler(
            entry_points=[CommandHandler("start", self.start_command)],
            states={
                AUTH_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.check_password)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
        )
        application.add_handler(auth_conv)

        # Account add wizard
        acc_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.accounts_menu, pattern="^menu_accounts$")],
            states={
                ACC_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_phone)],
                ACC_API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_api_id)],
                ACC_API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_api_hash)],
                ACC_TARGET_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_target_group)],
                ACC_MODE: [CallbackQueryHandler(self.acc_mode_callback, pattern="^acc_mode\\|")],
                ACC_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_code)],
                ACC_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_2fa)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        # Use a separate handler for the add flow triggered by button
        application.add_handler(CallbackQueryHandler(self.callback_router))

        # Add conversation for account_add
        add_acc_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_acc_add(u, c), pattern="^account_add$")],
            states={
                ACC_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_phone)],
                ACC_API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_api_id)],
                ACC_API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_api_hash)],
                ACC_TARGET_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_target_group)],
                ACC_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_code)],
                ACC_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.acc_2fa)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(add_acc_conv)

        # Keyword conversations
        kw_add_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_kw_add(u, c), pattern="^keyword_add$")],
            states={ADD_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_keyword_handler)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(kw_add_conv)

        kw_del_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_kw_del(u, c), pattern="^keyword_del$")],
            states={DEL_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.del_keyword_handler)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(kw_del_conv)

        # Group conversations
        grp_add_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_grp_add(u, c), pattern="^group_add$")],
            states={ADD_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_group_handler)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(grp_add_conv)

        grp_del_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_grp_del(u, c), pattern="^group_del$")],
            states={DEL_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.del_group_handler)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(grp_del_conv)

        # Blocked user conversations
        blk_add_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_blk_add(u, c), pattern="^blocked_add$")],
            states={ADD_BLKUSER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_blocked_user_handler)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(blk_add_conv)

        blk_del_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_blk_del(u, c), pattern="^blocked_del$")],
            states={DEL_BLKUSER: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.del_blocked_user_handler)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(blk_del_conv)

        # Default reply edit
        def_reply_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_def_reply(u, c), pattern="^reply_default_edit$")],
            states={EDIT_DEFAULT_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_default_reply_handler)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(def_reply_conv)

        # Settings edit
        settings_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: self.trigger_settings_edit(u, c), pattern="^settings_edit$")],
            states={EDIT_SETTING_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_setting_key)]},
            fallbacks=[CommandHandler("cancel", self.cancel)],
            per_message=False,
        )
        application.add_handler(settings_conv)

        # Help command
        application.add_handler(CommandHandler("help", self.help_command))

        # Text handler for non-conversation messages
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_handler))

    # Trigger helpers for conversations
    async def trigger_acc_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "📱 **إضافة حساب جديد**\n\n"
            "الخطوة 1/5: أرسل رقم الهاتف مع كود الدولة\n"
            "مثال: `+966500000000`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_accounts")]])
        )
        return ACC_PHONE

    async def trigger_kw_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🔑 **إضافة كلمة مفتاحية**\n\n"
            "أرسل الكلمة أو الكلمات (كل سطر كلمة):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_keywords")]])
        )
        return ADD_KEYWORD

    async def trigger_kw_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🔑 **حذف كلمة مفتاحية**\n\n"
            "أرسل الكلمة أو الكلمات للحذف (كل سطر كلمة):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_keywords")]])
        )
        return DEL_KEYWORD

    async def trigger_grp_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🔗 **إضافة جروبات**\n\n"
            "أرسل روابط الجروبات (كل سطر رابط):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_groups")]])
        )
        return ADD_GROUP

    async def trigger_grp_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🔗 **حذف جروبات**\n\n"
            "أرسل روابط الجروبات للحذف (كل سطر رابط):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_groups")]])
        )
        return DEL_GROUP

    async def trigger_blk_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🚫 **حظر مستخدم**\n\n"
            "أرسل: `user_id [username] [display_name]`\n"
            "أو فقط `user_id`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_blocked")]])
        )
        return ADD_BLKUSER

    async def trigger_blk_del(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🚫 **إلغاء حظر مستخدم**\n\n"
            "أرسل user_id للإلغاء:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_blocked")]])
        )
        return DEL_BLKUSER

    async def trigger_def_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "✍️ أرسل الرد الافتراضي الجديد:\n\n"
            "المتغيرات: `{first_name}`, `{last_name}`, `{username}`, `{user_id}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_replies")]])
        )
        return EDIT_DEFAULT_REPLY

    async def trigger_settings_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "⚙️ **تعديل إعداد**\n\n"
            "أرسل: `key value`\n"
            "مثال: `rate_limit_max 6`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="menu_settings")]])
        )
        return EDIT_SETTING_KEY


# ==================== BOT MANAGER ====================

class BotManager:
    def __init__(self) -> None:
        self.settings = Settings()
        self.cfg = Config(self.settings, None)
        self.db = DB(self.cfg.logger, self.settings)
        self.cfg.db = self.db
        self.state = State(self.settings)
        self.messenger = Messenger(self.cfg.logger)
        self.group_repo = GroupRepo(self.db)
        self.formatter = MessageFormatter(self.cfg)
        self.fallback = FallbackRouter(self.cfg, self.state, self.messenger, self.formatter, self.cfg.logger)
        self.backup = DbBackupManager(self.db, self.cfg.logger)
        self.controller: Optional[ControllerBot] = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        await self.db.init()
        await self.cfg.refresh()

        self.state.direct_triggers = await self.db.load_table("direct_reply_messages")
        self.state.blocked_phrases = await self.db.load_table("blocked_reply_messages")
        self.state.auto_replies = await self.db.load_table("auto_reply_responses")
        self.state.blocked_users = await self.db.blocked_users_map()
        self.cfg.logger.info("Loaded from DB")

        # Setup signals
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        # Start servers
        asyncio.create_task(self._run_health_server())
        asyncio.create_task(self._run_dashboard_server())
        asyncio.create_task(self._process_task_queue())

        # Load active accounts
        accounts = await self.db.get_all_accounts()
        self.cfg.logger.info(f"Found {len(accounts)} accounts in DB")

        for acc in accounts:
            if acc.status == AccountStatus.ACTIVE and acc.session_string:
                try:
                    client = TelegramClient(StringSession(acc.session_string), acc.api_id, acc.api_hash)
                    await client.connect()
                    if await client.is_user_authorized():
                        bot = WorkerBot(
                            cfg=self.cfg, db=self.db, state=self.state, messenger=self.messenger,
                            group_repo=self.group_repo, formatter=self.formatter, fallback=self.fallback,
                            client=client, target_group_id=acc.target_group_id, phone=acc.phone,
                            mode=acc.mode, logger=self.cfg.logger,
                        )
                        self.state.bots.append(bot)
                        self.cfg.logger.info(f"Account {acc.phone} started")
                    else:
                        await client.disconnect()
                        await self.db.update_account_status(acc.phone, AccountStatus.ERROR, "Session invalid")
                except Exception as e:
                    self.cfg.logger.error(f"Failed to start {acc.phone}: {e}")
                    await self.db.update_account_status(acc.phone, AccountStatus.ERROR, str(e))

        # Start controller bot
        self.controller = ControllerBot(self.settings, self.db, self.state, self.group_repo, self.backup, self.cfg, self.cfg.logger)
        application = Application.builder().token(self.settings.bot_token).build()
        self.controller.setup(application)

        self.cfg.logger.info("Starting controller bot...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)

        self.cfg.logger.info("Bot is running! Send /start to your bot in Telegram.")
        await self._shutdown_event.wait()

    async def shutdown(self) -> None:
        self.cfg.logger.info("Shutting down...")
        for phone, task in self.state.joining_now.items():
            task.cancel()
            self.state.stop_joining_flags[phone] = True
        # Shutdown controller
        if self.controller and self.controller.application:
            await self.controller.application.stop()
            await self.controller.application.shutdown()
        await self._shutdown_event.set()
        sys.exit(0)

    async def _run_health_server(self) -> None:
        async def health_handler(request):
            healthy = await self.db.health_check()
            return web.Response(text="OK", status=200) if healthy else web.Response(text="DB Error", status=503)
        app = web.Application()
        app.router.add_get("/health", health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.health_port)
        await site.start()
        self.cfg.logger.info(f"Health server on port {self.settings.health_port}")

    async def _run_dashboard_server(self) -> None:
        app = FastAPI(title="Ultimate Bot Dashboard", version="6.0")

        @app.get("/api/stats")
        async def get_stats():
            try:
                stats = await self.db.get_stats()
                bots = [{"phone": b.phone, "mode": b.mode, "connected": b.client.is_connected()} for b in self.state.bots]
                return JSONResponse({"stats": stats, "bots": bots})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/accounts")
        async def get_accounts():
            try:
                accs = await self.db.get_all_accounts()
                return JSONResponse([{"phone": a.phone, "status": a.status.value, "target_group": a.target_group_id, "mode": a.mode} for a in accs])
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/settings")
        async def get_settings():
            try:
                settings = await self.db.get_all_settings()
                return JSONResponse([{"key": s["key"], "value": s["value"], "description": s["description"]} for s in settings])
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        config = uvicorn.Config(app, host="0.0.0.0", port=self.settings.dashboard_port, log_level="warning")
        server = uvicorn.Server(config)
        self.cfg.logger.info(f"Dashboard on port {self.settings.dashboard_port}")
        await server.serve()

    async def _process_task_queue(self) -> None:
        while True:
            try:
                task = await self.db.dequeue_task()
                if task:
                    task_type = task["task_type"]
                    payload = json.loads(task["payload"])
                    self.cfg.logger.info(f"Task {task['id']}: {task_type}")
                    if task_type == "join_groups":
                        phone = payload.get("phone")
                        start_index = payload.get("start_index", 0)
                        target_bot = next((b for b in self.state.bots if b.phone == phone), None)
                        if target_bot:
                            await target_bot.join_groups_with_account(start_index)
                    await self.db.complete_task(task["id"])
                else:
                    await asyncio.sleep(5)
            except Exception as e:
                self.cfg.logger.error(f"Task queue error: {e}")
                await asyncio.sleep(10)

# ==================== ENTRY ====================

if __name__ == "__main__":
    try:
        asyncio.run(BotManager().start())
    except Exception:
        AppLogger.build().exception("Fatal error", exc_info=True)
