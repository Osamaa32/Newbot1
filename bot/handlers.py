"""
═══════════════════════════════════════════════════════════════
  CONTROL BOT HANDLERS — Interactive Telegram Bot Commands
═══════════════════════════════════════════════════════════════
"""

import logging
import asyncio
import json
from typing import Dict, Optional, Any
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    ContextTypes, ConversationTypes,
)

from shared.database import (
    AccountRepository, GroupRepository, KeywordRepository,
    TriggerPhraseRepository, BlockedUserRepository,
    ExcludedGroupRepository, BotSettingRepository,
    AdminSessionRepository, StatsService,
)
from shared.models import AccountStatus, AdminSession
from engine.utils import MessageFormatter

logger = logging.getLogger(__name__)

# ─── Conversation States ───
(
    AUTH_PASSWORD,
    ADD_ACCOUNT_PHONE,
    ADD_ACCOUNT_API_ID,
    ADD_ACCOUNT_API_HASH,
    ADD_ACCOUNT_GROUP,
    ADD_ACCOUNT_MODE,
    ADD_OTP_CODE,
    SEND_MESSAGE_TEXT,
    BROADCAST_MESSAGE,
    SET_SETTING_VALUE,
    BULK_ADD_ITEMS,
) = range(11)


class BotHandlers:
    """All bot command handlers"""

    def __init__(self, db_pool, engine_manager, settings):
        self.db_pool = db_pool
        self.engine = engine_manager
        self.settings = settings
        self.admin_password = settings.ADMIN_PASSWORD
        self.owner_id = settings.OWNER_ID

    # ═════════════════ AUTHENTICATION ═════════════════

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start command - authenticate first"""
        user_id = update.effective_user.id
        username = update.effective_user.username
        first_name = update.effective_user.first_name

        # Check if owner
        if self.owner_id and str(user_id) == str(self.owner_id):
            async with self.db_pool() as db:
                repo = AdminSessionRepository(db)
                await repo.create_or_update(
                    user_id=user_id, username=username,
                    first_name=first_name, is_authenticated=True,
                )
            await self._show_main_menu(update, context, f"👋 مرحباً يا مالك البوت! {first_name}")
            return

        # Check existing session
        async with self.db_pool() as db:
            repo = AdminSessionRepository(db)
            session = await repo.get_by_user_id(user_id)

            if session and session.is_authenticated:
                # Update activity
                await repo.create_or_update(user_id=user_id)
                await self._show_main_menu(update, context, f"👋 أهلاً بعودتك {first_name}!")
                return

        # Request password
        await update.message.reply_text(
            "🔐 **البوت محمي بكلمة مرور**\n\n"
            "الرجاء إدخال كلمة المرور للوصول:\n"
            "(أرسل /cancel للإلغاء)",
            parse_mode="Markdown",
        )
        return AUTH_PASSWORD

    async def auth_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle password authentication"""
        user_id = update.effective_user.id
        username = update.effective_user.username
        first_name = update.effective_user.first_name
        password = update.message.text.strip()

        if password == "/cancel":
            await update.message.reply_text("❌ تم الإلغاء.")
            return

        if password == self.admin_password:
            async with self.db_pool() as db:
                repo = AdminSessionRepository(db)
                await repo.create_or_update(
                    user_id=user_id, username=username,
                    first_name=first_name, is_authenticated=True,
                )

            await update.message.reply_text("✅ **تم التحقق بنجاح!**", parse_mode="Markdown")
            await self._show_main_menu(update, context, f"🎉 مرحباً {first_name}!")
        else:
            await update.message.reply_text(
                "❌ **كلمة المرور خاطئة!**\n\nحاول مرة أخرى أو أرسل /cancel",
                parse_mode="Markdown",
            )
            return AUTH_PASSWORD

    # ═════════════════ MAIN MENU ═════════════════

    async def _show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                              greeting: str = "") -> None:
        """Show the main menu"""
        keyboard = [
            ["📱 الحسابات", "📊 الإحصائيات"],
            ["🔑 الكلمات المفتاحية", "🔗 الجروبات"],
            ["⚙️ الإعدادات", "🛡 الفلاتر"],
            ["📨 إرسال رسالة", "📋 السجلات"],
            ["❓ المساعدة", "🔄 تحديث"],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        menu_text = (
            f"{greeting}\n\n"
            f"🏠 **القائمة الرئيسية**\n\n"
            f"اختر من القائمة أدناه أو اكتب /help لعرض جميع الأوامر."
        )

        await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode="Markdown")

    # ═════════════════ MENU HANDLERS ═════════════════

    async def menu_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle menu button clicks"""
        text = update.message.text

        menu_map = {
            "📱 الحسابات": self.cmd_accounts,
            "📊 الإحصائيات": self.cmd_stats,
            "🔑 الكلمات المفتاحية": self.cmd_keywords_menu,
            "🔗 الجروبات": self.cmd_groups_menu,
            "⚙️ الإعدادات": self.cmd_settings_menu,
            "🛡 الفلاتر": self.cmd_filters_menu,
            "📨 إرسال رسالة": self.cmd_send_msg,
            "📋 السجلات": self.cmd_logs,
            "🔄 تحديث": self.cmd_refresh,
        }

        handler = menu_map.get(text)
        if handler:
            await handler(update, context)
        else:
            await update.message.reply_text("⌨️ استخدم القائمة أو اكتب /help")

    # ═════════════════ ACCOUNT COMMANDS ═════════════════

    async def cmd_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show accounts menu"""
        async with self.db_pool() as db:
            repo = AccountRepository(db)
            accounts = await repo.get_all()

        if not accounts:
            keyboard = [[InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")]]
            await update.message.reply_text(
                "📱 لا توجد حسابات.\n\nاضغط لإضافة حساب جديد:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        lines = ["📱 **قائمة الحسابات:**\n"]
        buttons = []

        for acc in accounts:
            status_emoji = {
                "active": "🟢", "pending": "⏳", "paused": "⏸️",
                "error": "🔴", "banned": "🚫", "flood": "⏳", "connecting": "🔄",
            }.get(acc.status, "⚪")

            conn_time = acc.last_connected.strftime("%m/%d %H:%M") if acc.last_connected else "—"
            lines.append(
                f"{status_emoji} **{acc.phone}**\n"
                f"   الحالة: `{acc.status}` | الوضع: `{acc.mode}`\n"
                f"   آخر اتصال: `{conn_time}`\n"
                f"   الرسائل: {acc.total_messages} | الردود: {acc.total_replies}\n"
                f"   —————————————"
            )

            # Control buttons per account
            row = []
            if acc.status == AccountStatus.ACTIVE.value:
                row.append(InlineKeyboardButton(f"⏸️ {acc.phone}", callback_data=f"stop:{acc.phone}"))
            else:
                row.append(InlineKeyboardButton(f"▶️ {acc.phone}", callback_data=f"start:{acc.phone}"))
            row.append(InlineKeyboardButton("🗑", callback_data=f"remove:{acc.phone}"))
            buttons.append(row)

        buttons.append([InlineKeyboardButton("➕ إضافة حساب جديد", callback_data="add_account")])
        buttons.append([InlineKeyboardButton("🔄 تحديث", callback_data="refresh_accounts")])

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )

    async def cmd_add_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start add account flow"""
        await update.message.reply_text(
            "📱 **إضافة حساب جديد**\n\n"
            "الخطوة 1/5: أرسل رقم الهاتف (مع كود الدولة):\n"
            "مثال: `+966501234567`\n\n"
            "أرسل /cancel للإلغاء",
            parse_mode="Markdown",
        )
        return ADD_ACCOUNT_PHONE

    async def add_account_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        phone = update.message.text.strip()
        if phone == "/cancel":
            await update.message.reply_text("❌ تم الإلغاء.")
            return

        context.user_data["new_account"] = {"phone": phone}
        await update.message.reply_text(
            "📱 **الخطوة 2/5: API ID**\n\n"
            "أرسل API ID من my.telegram.org:\n"
            "أرسل /cancel للإلغاء",
            parse_mode="Markdown",
        )
        return ADD_ACCOUNT_API_ID

    async def add_account_api_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()
        if text == "/cancel":
            await update.message.reply_text("❌ تم الإلغاء.")
            return

        try:
            api_id = int(text)
            context.user_data["new_account"]["api_id"] = api_id
            await update.message.reply_text(
                "📱 **الخطوة 3/5: API Hash**\n\n"
                "أرسل API Hash من my.telegram.org:\n"
                "أرسل /cancel للإلغاء",
                parse_mode="Markdown",
            )
            return ADD_ACCOUNT_API_HASH
        except ValueError:
            await update.message.reply_text("❌ يجب أن يكون رقماً. حاول مرة أخرى:")
            return ADD_ACCOUNT_API_ID

    async def add_account_api_hash(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        api_hash = update.message.text.strip()
        if api_hash == "/cancel":
            await update.message.reply_text("❌ تم الإلغاء.")
            return

        context.user_data["new_account"]["api_hash"] = api_hash
        await update.message.reply_text(
            "📱 **الخطوة 4/5: Target Group ID**\n\n"
            "أرسل ID المجموعة المستهدفة:\n"
            "مثال: `-1001234567890`\n\n"
            "أرسل /cancel للإلغاء",
            parse_mode="Markdown",
        )
        return ADD_ACCOUNT_GROUP

    async def add_account_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()
        if text == "/cancel":
            await update.message.reply_text("❌ تم الإلغاء.")
            return

        try:
            group_id = int(text)
            context.user_data["new_account"]["target_group_id"] = group_id

            keyboard = [
                [InlineKeyboardButton("📤 توجيه فقط (forward)", callback_data="mode:forward")],
                [InlineKeyboardButton("💬 رد فقط (reply)", callback_data="mode:reply")],
                [InlineKeyboardButton("📤💬 كلاهما (both)", callback_data="mode:both")],
            ]
            await update.message.reply_text(
                "📱 **الخطوة 5/5: الوضع**\n\n"
                "اختر وضع الحساب:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return ConversationHandler.END
        except ValueError:
            await update.message.reply_text("❌ يجب أن يكون رقماً. حاول مرة أخرى:")
            return ADD_ACCOUNT_GROUP

    # ═════════════════ STATS ═════════════════

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show system statistics"""
        async with self.db_pool() as db:
            service = StatsService(db)
            stats = await service.get_full_stats()

        text = (
            f"📊 **إحصائيات النظام**\n\n"
            f"📱 **الحسابات:**\n"
            f"   إجمالي: {stats['accounts']['total']}\n"
            f"   🟢 نشط: {stats['accounts']['active']}\n"
            f"   ⏳ معلق: {stats['accounts']['by_status'].get('pending', 0)}\n"
            f"   ⏸️ متوقف: {stats['accounts']['by_status'].get('paused', 0)}\n"
            f"   🔴 خطأ: {stats['accounts']['by_status'].get('error', 0)}\n\n"
            f"🔗 **الجروبات:** {stats['groups']['total']}\n"
            f"🔑 **الكلمات المفتاحية:** {stats['keywords']['total']}\n"
            f"🚫 **المحظورون:** {stats['blocked_users']}\n"
            f"📨 **التوجيهات اليوم:** {stats['forwards_today']}\n\n"
            f"📋 **المهام:**\n"
            f"   ⏳ قيد الانتظار: {stats['tasks'].get('pending', 0)}\n"
            f"   🔄 قيد المعالجة: {stats['tasks'].get('processing', 0)}\n"
            f"   ✅ مكتملة: {stats['tasks'].get('completed', 0)}\n\n"
            f"🕒 {stats['timestamp'][:19]}"
        )

        keyboard = [
            [InlineKeyboardButton("🔄 تحديث", callback_data="refresh_stats")],
        ]
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
        )

    # ═════════════════ KEYWORDS MENU ═════════════════

    async def cmd_keywords_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show keywords management menu"""
        async with self.db_pool() as db:
            repo = KeywordRepository(db)
            keywords = await repo.get_all()

        kw_list = "\n".join([f"• `{k.word}` ({k.category})" for k in keywords[:20]])
        remaining = len(keywords) - 20 if len(keywords) > 20 else 0

        text = (
            f"🔑 **إدارة الكلمات المفتاحية**\n\n"
            f"**الكلمة الحالية ({len(keywords)}):**\n{kw_list}\n"
        )
        if remaining:
            text += f"\n_... و {remaining} كلمة أخرى_"

        keyboard = [
            [InlineKeyboardButton("➕ إضافة كلمة", callback_data="kw_add")],
            [InlineKeyboardButton("🗑 حذف كلمة", callback_data="kw_del")],
            [InlineKeyboardButton("🔍 بحث", callback_data="kw_search")],
            [InlineKeyboardButton("📋 عرض الكل", callback_data="kw_list")],
        ]
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
        )

    # ═════════════════ GROUPS MENU ═════════════════

    async def cmd_groups_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show groups management menu"""
        async with self.db_pool() as db:
            repo = GroupRepository(db)
            groups = await repo.get_all()

        group_list = "\n".join([f"{i+1}. {g.group_link}" for i, g in enumerate(groups[:15])])

        text = (
            f"🔗 **إدارة الجروبات**\n\n"
            f"**العدد:** {len(groups)}\n\n"
            f"{group_list}"
        )

        keyboard = [
            [InlineKeyboardButton("➕ إضافة جروب", callback_data="group_add")],
            [InlineKeyboardButton("🗑 حذف جروب", callback_data="group_del")],
            [InlineKeyboardButton("👥 الانضمام للكل", callback_data="group_join")],
            [InlineKeyboardButton("🔄 تحديث القائمة", callback_data="group_refresh")],
        ]
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
        )

    # ═════════════════ SETTINGS MENU ═════════════════

    async def cmd_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show settings menu"""
        async with self.db_pool() as db:
            repo = BotSettingRepository(db)
            settings_list = await repo.get_all()

        important_settings = [
            "fallback_group_id", "rate_limit_max", "rate_limit_window",
            "default_auto_reply", "word_count_limit", "auto_reply_enabled",
            "forward_enabled",
        ]

        lines = ["⚙️ **الإعدادات:**\n"]
        for s in settings_list:
            if s.key in important_settings:
                lines.append(f"• `{s.key}` = `{s.value}`")

        keyboard = [
            [InlineKeyboardButton("✏️ تعديل إعداد", callback_data="setting_edit")],
            [InlineKeyboardButton("📋 عرض الكل", callback_data="setting_list")],
            [InlineKeyboardButton("🔄 إعادة ضبط", callback_data="setting_reset")],
        ]
        await update.message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
        )

    # ═════════════════ FILTERS MENU ═════════════════

    async def cmd_filters_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show filters menu"""
        async with self.db_pool() as db:
            repo = BotSettingRepository(db)

            filters_status = {
                "@Mentions": await repo.get_bool("filter_mention", True),
                "🔗 Links": await repo.get_bool("filter_links", True),
                "🔢 Digits": await repo.get_bool("filter_digits", True),
                "💬 Private": await repo.get_bool("filter_private", True),
                "📤 Outgoing": await repo.get_bool("filter_outgoing", True),
                "🤖 Bots": await repo.get_bool("filter_bots", True),
                "👑 Admins": await repo.get_bool("filter_admins", True),
            }

        lines = ["🛡 **فلاتر التجاهل:**\n"]
        for name, active in filters_status.items():
            emoji = "✅" if active else "❌"
            lines.append(f"{emoji} {name}")

        lines.append(f"\n📝 **حد الكلمات:** `{await repo.get_int('word_count_limit', 17)}`")

        keyboard = [
            [InlineKeyboardButton("✏️ تعديل فلتر", callback_data="filter_edit")],
            [InlineKeyboardButton("📋 عرض التفاصيل", callback_data="filter_details")],
        ]
        await update.message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
        )

    # ═════════════════ SEND MESSAGE ═════════════════

    async def cmd_send_msg(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start send message flow"""
        accounts = list(self.engine.accounts.keys())
        if not accounts:
            await update.message.reply_text("❌ لا توجد حسابات نشطة.")
            return

        keyboard = [[InlineKeyboardButton(f"📱 {phone}", callback_data=f"send_from:{phone}")]
                    for phone in accounts]
        keyboard.append([InlineKeyboardButton("📨 إرسال من الكل", callback_data="send_from:all")])

        await update.message.reply_text(
            "📨 **إرسال رسالة**\n\n"
            "اختر الحساب المرسل:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    # ═════════════════ LOGS ═════════════════

    async def cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recent logs"""
        async with self.db_pool() as db:
            from shared.database import SystemLogRepository
            repo = SystemLogRepository(db)
            logs = await repo.get_recent(limit=15)

        if not logs:
            await update.message.reply_text("📋 لا توجد سجلات.")
            return

        lines = ["📋 **آخر السجلات:**\n"]
        for log in logs:
            emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}.get(log.level, "•")
            time_str = log.created_at.strftime("%H:%M:%S") if log.created_at else "—"
            lines.append(f"{emoji} `[{time_str}]` {log.message[:100]}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ═════════════════ REFRESH ═════════════════

    async def cmd_refresh(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Refresh configuration"""
        await self.engine._refresh_config()
        await update.message.reply_text("✅ **تم تحديث الإعدادات بنجاح!**", parse_mode="Markdown")

    # ═════════════════ CALLBACK HANDLERS ═════════════════

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard callbacks"""
        query = update.callback_query
        await query.answer()

        data = query.data

        # Account controls
        if data == "add_account":
            await query.edit_message_text(
                "📱 **إضافة حساب جديد**\n\n"
                "أرسل الآن البيانات بالصيغة:\n"
                "```\n/phone <رقم>\n/api_id <id>\n/api_hash <hash>\n/group <group_id>\n/mode <forward|reply|both>\n```\n\n"
                "أو اكتب `/addaccount` لبدء المعالج التفاعلي.",
                parse_mode="Markdown",
            )
            return

        if data.startswith("start:"):
            phone = data.split(":", 1)[1]
            await query.edit_message_text(f"⏳ جاري تشغيل الحساب {phone}...")
            success, msg = await self.engine.start_account(phone)
            await query.edit_message_text(f"{'✅' if success else '❌'} {msg}")
            return

        if data.startswith("stop:"):
            phone = data.split(":", 1)[1]
            await query.edit_message_text(f"⏳ جاري إيقاف الحساب {phone}...")
            success, msg = await self.engine.stop_account(phone)
            await query.edit_message_text(f"{'✅' if success else '❌'} {msg}")
            return

        if data.startswith("remove:"):
            phone = data.split(":", 1)[1]
            # Confirmation
            keyboard = [
                [InlineKeyboardButton("✅ نعم، احذف", callback_data=f"confirm_remove:{phone}")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="refresh_accounts")],
            ]
            await query.edit_message_text(
                f"⚠️ **هل أنت متأكد من حذف الحساب {phone}؟**\n\n"
                f"هذا الإجراء لا يمكن التراجع عنه!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            return

        if data.startswith("confirm_remove:"):
            phone = data.split(":", 1)[1]
            success, msg = await self.engine.remove_account(phone)
            await query.edit_message_text(f"{'✅' if success else '❌'} {msg}")
            return

        if data == "refresh_accounts":
            await self.cmd_accounts(update, context)
            return

        if data == "refresh_stats":
            await self.cmd_stats(update, context)
            return

        # Stats & refresh
        if data == "refresh_stats":
            await query.edit_message_text("🔄 جاري التحديث...")
            await self.cmd_stats(update, context)
            return

        # Default
        await query.edit_message_text(f"📌 اختر من القائمة أو اكتب /help")

    # ═════════════════ HELP ═════════════════

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show comprehensive help"""
        help_text = self._get_help_text()
        await update.message.reply_text(help_text, parse_mode="Markdown")

    def _get_help_text(self) -> str:
        """Get comprehensive help text"""
        return (
            "✨ **دليل استخدام البوت — النسخة المتكاملة** ✨\n\n"

            "═══════════════════════════════════════\n"
            "📱 **إدارة الحسابات**\n"
            "═══════════════════════════════════════\n"
            "`/accounts` — عرض جميع الحسابات مع التحكم\n"
            "`/addaccount` — إضافة حساب جديد (معالج تفاعلي)\n"
            "`/startacc <phone>` — تشغيل حساب\n"
            "`/stopacc <phone>` — إيقاف حساب\n"
            "`/removeacc <phone>` — حذف حساب نهائياً\n"
            "`/restartacc <phone>` — إعادة تشغيل حساب\n"
            "`/setmode <phone> <forward/reply/both>` — تغيير الوضع\n"
            "`/setgroup <phone> <group_id>` — تغيير المجموعة\n"
            "`/accountinfo <phone>` — معلومات الحساب\n\n"

            "═══════════════════════════════════════\n"
            "📊 **الإحصائيات والمراقبة**\n"
            "═══════════════════════════════════════\n"
            "`/stats` — إحصائيات النظام الكاملة\n"
            "`/status` — حالة جميع الحسابات\n"
            "`/engine` — حالة المحرك\n"
            "`/logs` — آخر السجلات\n\n"

            "═══════════════════════════════════════\n"
            "🔑 **الكلمات المفتاحية**\n"
            "═══════════════════════════════════════\n"
            "`/keywords` — إدارة الكلمات\n"
            "`/addkw <كلمة>` — إضافة كلمة\n"
            "`/delkw <كلمة>` — حذف كلمة\n"
            "`/listkw` — عرض الكلمات\n\n"

            "═══════════════════════════════════════\n"
            "🔗 **إدارة الجروبات**\n"
            "═══════════════════════════════════════\n"
            "`/groups` — إدارة الجروبات\n"
            "`/addgroup <رابط>` — إضافة جروب\n"
            "`/delgroup <id>` — حذف جروب\n"
            "`/listgroups` — عرض الجروبات\n"
            "`/joinall <phone>` — انضمام لكل الجروبات\n\n"

            "═══════════════════════════════════════\n"
            "⚙️ **الإعدادات**\n"
            "═══════════════════════════════════════\n"
            "`/settings` — عرض الإعدادات\n"
            "`/set <key> <value>` — تعديل إعداد\n"
            "`/reset <key>` — إعادة للافتراضي\n"
            "`/filters` — إدارة الفلاتر\n"
            "`/togglefilter <name>` — تفعيل/تعطيل فلتر\n\n"

            "═══════════════════════════════════════\n"
            "📨 **الرسائل**\n"
            "═══════════════════════════════════════\n"
            "`/send <phone> <رسالة>` — إرسال من حساب\n"
            "`/broadcast <رسالة>` — إرسال من الكل\n"
            "`/replyset <نص>` — تعيين الرد التلقائي\n"
            "`/replyshow` — عرض الرد التلقائي\n\n"

            "═══════════════════════════════════════\n"
            "🛡 **الأمان**\n"
            "═══════════════════════════════════════\n"
            "`/block <user_id>` — حظر مستخدم\n"
            "`/unblock <user_id>` — فك حظر\n"
            "`/blocked` — قائمة المحظورين\n"
            "`/excluded` — الجروبات المستثناة\n\n"

            "═══════════════════════════════════════\n"
            "🔧 **أوامر النظام**\n"
            "═══════════════════════════════════════\n"
            "`/refresh` — تحديث الإعدادات\n"
            "`/restart` — إعادة تشغيل الكل\n"
            "`/health` — فحص صحة النظام\n"
            "`/backup` — نسخ احتياطي\n"
            "`/menu` — القائمة الرئيسية\n"
            "`/help` — هذا المساعدة\n\n"

            "═══════════════════════════════════════\n"
            "🎯 **المتغيرات في الرد التلقائي**\n"
            "═══════════════════════════════════════\n"
            "`{{first_name}}` — اسم المستخدم\n"
            "`{{last_name}}` — اسم العائلة\n"
            "`{{username}}` — المعرف\n"
            "`{{user_id}}` — الرقم التعريفي\n\n"

            "═══════════════════════════════════════\n"
            "💡 **نصائح**\n"
            "═══════════════════════════════════════\n"
            "• استخدم الأزرار في القائمة للتحكم السريع\n"
            "• كل الإعدادات تُحفظ تلقائياً\n"
            "• الحسابات تُشغل تلقائياً عند تشغيل البوت\n"
            "• يمكنك الوصول للوحة التحكم عبر المتصفح\n\n"

            "🤖 **بوت التحكم في حسابات تليجرام — v6.0**"
        )

    # ═════════════════ QUICK COMMANDS ═════════════════

    async def cmd_start_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Quick start account command"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/startacc <phone>`", parse_mode="Markdown")
            return
        phone = context.args[0]
        msg = await update.message.reply_text(f"⏳ جاري تشغيل {phone}...")
        success, result = await self.engine.start_account(phone)
        await msg.edit_text(f"{'✅' if success else '❌'} {result}")

    async def cmd_stop_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Quick stop account command"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/stopacc <phone>`", parse_mode="Markdown")
            return
        phone = context.args[0]
        success, result = await self.engine.stop_account(phone)
        await update.message.reply_text(f"{'✅' if success else '❌'} {result}")

    async def cmd_remove_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Quick remove account command"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/removeacc <phone>`", parse_mode="Markdown")
            return
        phone = context.args[0]
        keyboard = [
            [InlineKeyboardButton("✅ نعم", callback_data=f"confirm_remove:{phone}")],
            [InlineKeyboardButton("❌ لا", callback_data="refresh_accounts")],
        ]
        await update.message.reply_text(
            f"⚠️ تأكيد حذف الحساب **{phone}**؟",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    async def cmd_engine_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show engine status"""
        stats = self.engine.get_stats()
        text = (
            f"⚙️ **حالة المحرك**\n\n"
            f"📱 الحسابات النشطة: {stats['connected']}/{stats['total_accounts']}\n"
            f"🟢 متصل: {stats['connected']}\n"
            f"📦 Forward Cache: {stats['forward_cache_size']}\n"
            f"💬 Reply Cache: {stats['reply_cache_size']}\n"
            f"🔑 Keywords: {stats['keywords_loaded']}\n"
            f"🚫 Excluded: {stats['excluded_groups']}\n"
            f"🚫 Blocked Users: {stats['blocked_users']}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Health check"""
        db_ok = False
        try:
            async with self.db_pool() as db:
                from sqlalchemy import text
                await db.execute(text("SELECT 1"))
                db_ok = True
        except Exception as e:
            db_ok = False

        accounts_ok = len(self.engine.accounts) > 0

        text = (
            f"🏥 **فحص صحة النظام**\n\n"
            f"🗄 قاعدة البيانات: {'✅' if db_ok else '❌'}\n"
            f"⚙️ المحرك: {'✅ نشط' if accounts_ok else '⚠️ لا يوجد حسابات'}\n"
            f"📱 الحسابات: {len(self.engine.accounts)}\n"
            f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Restart all accounts"""
        msg = await update.message.reply_text("🔄 جاري إعادة تشغيل جميع الحسابات...")
        results = await self.engine.restart_all()
        success_count = sum(1 for _, s, _ in results if s)
        text = (
            f"✅ **تمت إعادة التشغيل**\n\n"
            f"🟢 نجح: {success_count}\n"
            f"❌ فشل: {len(results) - success_count}"
        )
        await msg.edit_text(text)

    async def cmd_add_keyword(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add keyword command"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/addkw <كلمة>`", parse_mode="Markdown")
            return
        word = " ".join(context.args)
        async with self.db_pool() as db:
            repo = KeywordRepository(db)
            await repo.create(word)
        await self.engine._refresh_config()
        await update.message.reply_text(f"✅ تم إضافة الكلمة: `{word}`", parse_mode="Markdown")

    async def cmd_del_keyword(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Delete keyword command"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/delkw <كلمة>`", parse_mode="Markdown")
            return
        word = " ".join(context.args)
        async with self.db_pool() as db:
            repo = KeywordRepository(db)
            kw = await repo.get_by_word(word)
            if kw:
                await repo.delete(kw.id)
                await update.message.reply_text(f"✅ تم حذف الكلمة: `{word}`", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"⚠️ الكلمة غير موجودة: `{word}`", parse_mode="Markdown")
        await self.engine._refresh_config()

    async def cmd_list_keywords(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List keywords command"""
        async with self.db_pool() as db:
            repo = KeywordRepository(db)
            keywords = await repo.get_all()

        if not keywords:
            await update.message.reply_text("🔑 لا توجد كلمات مفتاحية.")
            return

        kw_list = "\n".join([f"{i+1}. `{k.word}` ({k.category})" for i, k in enumerate(keywords)])
        await update.message.reply_text(f"🔑 **الكلمات المفتاحية ({len(keywords)}):**\n\n{kw_list}", parse_mode="Markdown")

    async def cmd_add_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add group command"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/addgroup <رابط>`", parse_mode="Markdown")
            return
        link = context.args[0]
        async with self.db_pool() as db:
            repo = GroupRepository(db)
            await repo.create(link)
        await update.message.reply_text(f"✅ تم إضافة الجروب: `{link}`", parse_mode="Markdown")

    async def cmd_list_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List groups command"""
        async with self.db_pool() as db:
            repo = GroupRepository(db)
            groups = await repo.get_all()

        if not groups:
            await update.message.reply_text("🔗 لا توجد جروبات.")
            return

        group_list = "\n".join([f"{i+1}. {g.group_link}" for i, g in enumerate(groups)])
        await update.message.reply_text(f"🔗 **الجروبات ({len(groups)}):**\n\n{group_list}")

    async def cmd_send_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send message from account"""
        if len(context.args) < 2:
            await update.message.reply_text("❌ استخدم: `/send <phone> <رسالة>`", parse_mode="Markdown")
            return
        phone = context.args[0]
        message = " ".join(context.args[1:])

        if phone == "all":
            results = await self.engine.broadcast_message(message)
            success = sum(1 for ok, _ in results.values() if ok)
            await update.message.reply_text(f"📨 تم الإرسال: {success}/{len(results)}")
        else:
            engine = self.engine.accounts.get(phone)
            if not engine:
                await update.message.reply_text(f"❌ الحساب {phone} غير نشط.")
                return
            await safe_send(engine.client, engine.target_group_id, message)
            await update.message.reply_text(f"✅ تم الإرسال من {phone}")

    async def cmd_set_setting(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set setting command"""
        if len(context.args) < 2:
            await update.message.reply_text("❌ استخدم: `/set <key> <value>`", parse_mode="Markdown")
            return
        key = context.args[0]
        value = " ".join(context.args[1:])
        async with self.db_pool() as db:
            repo = BotSettingRepository(db)
            await repo.set(key, value, update.effective_user.id)
        await self.engine._refresh_config()
        await update.message.reply_text(f"✅ تم تحديث: `{key}` = `{value}`", parse_mode="Markdown")

    async def cmd_show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show all settings"""
        async with self.db_pool() as db:
            repo = BotSettingRepository(db)
            settings = await repo.get_all()

        lines = ["⚙️ **الإعدادات:**\n"]
        for s in settings:
            lines.append(f"• `{s.key}` = `{s.value}`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_set_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set auto reply message"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/replyset <نص الرد>`", parse_mode="Markdown")
            return
        text = " ".join(context.args)
        async with self.db_pool() as db:
            repo = BotSettingRepository(db)
            await repo.set("default_auto_reply", text, update.effective_user.id)
        await update.message.reply_text(f"✅ تم تحديث الرد التلقائي:\n`{text}`", parse_mode="Markdown")

    async def cmd_show_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show auto reply message"""
        async with self.db_pool() as db:
            repo = BotSettingRepository(db)
            reply = await repo.get("default_auto_reply", "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك")
        await update.message.reply_text(f"💬 **الرد التلقائي:**\n`{reply}`", parse_mode="Markdown")

    async def cmd_block_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Block user"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/block <user_id> [سبب]`", parse_mode="Markdown")
            return
        try:
            user_id = int(context.args[0])
            reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
            async with self.db_pool() as db:
                repo = BlockedUserRepository(db)
                await repo.create(user_id, reason=reason, blocked_by=update.effective_user.id)
            await self.engine._refresh_config()
            await update.message.reply_text(f"🚫 تم حظر المستخدم: `{user_id}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ user_id يجب أن يكون رقماً.")

    async def cmd_unblock_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Unblock user"""
        if not context.args:
            await update.message.reply_text("❌ استخدم: `/unblock <user_id>`", parse_mode="Markdown")
            return
        try:
            user_id = int(context.args[0])
            async with self.db_pool() as db:
                repo = BlockedUserRepository(db)
                await repo.delete(user_id)
            await self.engine._refresh_config()
            await update.message.reply_text(f"✅ تم فك الحظر: `{user_id}`", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ user_id يجب أن يكون رقماً.")

    async def cmd_blocked_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List blocked users"""
        async with self.db_pool() as db:
            repo = BlockedUserRepository(db)
            users = await repo.get_all(limit=50)

        if not users:
            await update.message.reply_text("🚫 لا يوجد محظورون.")
            return

        lines = ["🚫 **المستخدمون المحظورون:**\n"]
        for u in users:
            lines.append(f"• `{u.user_id}` @{u.username or '—'} | {u.display_name or '—'}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def cmd_backup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create backup"""
        import json, gzip
        from pathlib import Path

        try:
            async with self.db_pool() as db:
                # Collect all data
                acc_repo = AccountRepository(db)
                accounts = await acc_repo.get_all()

                grp_repo = GroupRepository(db)
                groups = await grp_repo.get_all()

                kw_repo = KeywordRepository(db)
                keywords = await kw_repo.get_all()

                data = {
                    "accounts": [a.to_dict() for a in accounts],
                    "groups": [g.to_dict() for g in groups],
                    "keywords": [k.to_dict() for k in keywords],
                    "created_at": datetime.utcnow().isoformat(),
                }

            # Save backup
            Path("backups").mkdir(exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"backups/backup_{ts}.json.gz"

            with gzip.open(filename, "wt", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)

            await update.message.reply_document(
                document=open(filename, "rb"),
                caption=f"✅ نسخة احتياطية: `{filename}`",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ في النسخ الاحتياطي: {str(e)[:200]}")

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Cancel current operation"""
        await update.message.reply_text("❌ تم الإلغاء.", reply_markup=ReplyKeyboardRemove())
        return
