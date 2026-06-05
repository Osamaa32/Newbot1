"""
═══════════════════════════════════════════════════════════════════════════════
Controller Bot — python-telegram-bot based control center via @BotFather
═══════════════════════════════════════════════════════════════════════════════

The brain of the system. All control happens through this bot:
- Password authentication on first use
- Add/start/stop/remove accounts
- Manage keywords, filters, groups
- Monitor system health and stats
- Interactive step-by-step flows with inline buttons
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import re
import sys
import time
from functools import wraps
from io import BytesIO
from typing import Any, Callable, Coroutine, Dict, List, Optional

import asyncpg
import bcrypt
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import Database
from models import AccountStatus
from utils import TextUtils

logger = logging.getLogger("telegram-bot")

# ─── Conversation States ───
(
    STATE_AUTH_PASSWORD,
    STATE_ADD_PHONE,
    STATE_ADD_API_ID,
    STATE_ADD_API_HASH,
    STATE_ADD_GROUP,
    STATE_ADD_MODE,
    STATE_ADD_OTP,
    STATE_ADD_2FA,
    STATE_SET_PASSWORD,
    STATE_JOIN_PHONE,
    STATE_JOIN_START,
    STATE_EDIT_REPLY,
    STATE_EDIT_CONFIG_KEY,
    STATE_EDIT_CONFIG_VALUE,
    STATE_BULK_GROUPS,
    STATE_EXCLUDED_ADD,
    STATE_EXCLUDED_REASON,
    STATE_KEYWORD_ADD,
    STATE_KEYWORD_CAT,
    STATE_BLKUSER_ADD,
    STATE_FILTER_NAME,
    STATE_FILTER_VALUE,
    STATE_CONFIRM_ACTION,
) = range(23)


# ─── Decorators ───

def require_auth(func: Callable) -> Callable:
    """Ensure user is authenticated before executing command."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, **kwargs):
        db: Database = context.application.db
        user_id = update.effective_user.id
        owner_id = await db.get_setting("owner_id", "")
        if owner_id and str(user_id) != owner_id:
            await safe_reply(update, "⚠️ غير مصرح لك باستخدام هذا البوت.")
            return
        if not await db.is_authenticated():
            if update.callback_query:
                await update.callback_query.answer("يجب إعداد كلمة المرور أولاً!", show_alert=True)
            else:
                await safe_reply(update, "🔐 أرسل /start لإعداد كلمة المرور أولاً.")
            return
        return await func(update, context, **kwargs)
    return wrapper


def require_owner_id(func: Callable) -> Callable:
    """Check if owner_id is set, if not set it from the current user."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, **kwargs):
        db: Database = context.application.db
        user_id = update.effective_user.id
        owner_id = await db.get_setting("owner_id", "")
        if not owner_id:
            await db.set_setting("owner_id", str(user_id))
            logger.info(f"Owner set to: {user_id}")
        elif str(user_id) != owner_id:
            await safe_reply(update, "⚠️ أنت لست المالك. لا يمكنك استخدام هذا البوت.")
            return
        return await func(update, context, **kwargs)
    return wrapper


# ─── Helpers ───

async def safe_reply(update: Update, text: str, reply_markup=None, parse_mode: str = "Markdown") -> None:
    """Safely reply to a message or callback query."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    elif update.message:
        if len(text) > 4000:
            for part in TextUtils.split_long(text, 4000):
                await update.message.reply_text(part, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


def build_menu(buttons: list, n_cols: int = 2) -> List[List[InlineKeyboardButton]]:
    return [buttons[i : i + n_cols] for i in range(0, len(buttons), n_cols)]


def get_main_menu() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("📱 الحسابات", callback_data="menu_accounts"),
        InlineKeyboardButton("⚙️ الإعدادات", callback_data="menu_settings"),
        InlineKeyboardButton("🔑 الكلمات المفتاحية", callback_data="menu_keywords"),
        InlineKeyboardButton("🛡 الفلاتر", callback_data="menu_filters"),
        InlineKeyboardButton("🔗 الجروبات", callback_data="menu_groups"),
        InlineKeyboardButton("💬 الردود", callback_data="menu_replies"),
        InlineKeyboardButton("🚫 المحظورون", callback_data="menu_blocked"),
        InlineKeyboardButton("📊 الإحصائيات", callback_data="menu_stats"),
        InlineKeyboardButton("🏥 الصحة", callback_data="menu_health"),
        InlineKeyboardButton("❓ المساعدة", callback_data="menu_help"),
    ]
    return InlineKeyboardMarkup(build_menu(buttons, n_cols=2))


# ─── Command Handlers ───

@require_owner_id
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — set password if not set, otherwise show main menu."""
    db: Database = context.application.db
    user_id = update.effective_user.id

    # Set owner
    await db.set_setting("owner_id", str(user_id))

    # Check if password is set
    password_hash = await db.get_password_hash()

    if not password_hash:
        # First time — ask to set password
        await safe_reply(
            update,
            "🔐 **مرحباً! هذا أول استخدام للبوت.**\n\n"
            "يرجى إعداد كلمة مرور للتحكم:\n"
            "(أرسل كلمة مرور قوية تحتوي على أحرف وأرقام)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return STATE_SET_PASSWORD

    if not await db.is_authenticated():
        await safe_reply(update, "🔐 **أهلاً بك!**\n\nأرسل كلمة المرور للدخول:")
        return STATE_AUTH_PASSWORD

    # Already authenticated — show main menu
    await show_main_menu(update, context)
    return ConversationHandler.END


async def state_set_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle password setting."""
    db: Database = context.application.db
    password = update.message.text.strip()

    if len(password) < 4:
        await safe_reply(update, "⚠️ كلمة المرور قصيرة جداً. أرسل كلمة أطول (4 أحرف على الأقل):")
        return STATE_SET_PASSWORD

    # Hash password
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    await db.set_password(password_hash)
    await db.set_authenticated(True)

    await safe_reply(
        update,
        "✅ **تم إعداد كلمة المرور بنجاح!**\n\n"
        "الآن يمكنك التحكم الكامل في البوت.\n\n"
        "⬇️ القائمة الرئيسية:",
        reply_markup=get_main_menu(),
    )
    return ConversationHandler.END


async def state_auth_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle password authentication."""
    db: Database = context.application.db
    password = update.message.text.strip()
    stored_hash = await db.get_password_hash()

    if stored_hash and bcrypt.checkpw(password.encode(), stored_hash.encode()):
        await db.set_authenticated(True)
        await safe_reply(
            update,
            "✅ **تم الدخول بنجاح!**\n\n"
            "⬇️ القائمة الرئيسية:",
            reply_markup=get_main_menu(),
        )
        return ConversationHandler.END
    else:
        await safe_reply(update, "❌ **كلمة المرور خاطئة!**\n\nحاول مرة أخرى:")
        return STATE_AUTH_PASSWORD


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(
        update,
        "✨ **مركز التحكم — القائمة الرئيسية** ✨\n\n"
        "اختر من القائمة أدناه:\n"
        "📱 إدارة الحسابات والأرقام\n"
        "⚙️ إعدادات النظام والحدود\n"
        "🔑 الكلمات المفتاحية\n"
        "🛡 فلاتر التجاهل\n"
        "🔗 إدارة الجروبات\n"
        "💬 الردود التلقائية\n"
        "🚫 المستخدمون المحظورون\n"
        "📊 الإحصائيات والمراقبة\n"
        "🏥 فحص صحة النظام\n"
        "❓ المساعدة والأوامر",
        reply_markup=get_main_menu(),
    )


@require_auth
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)


@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "✨ **مركز التحكم الكامل — دليل الأوامر** ✨\n\n"
        "📱 **إدارة الحسابات**\n"
        "`/addaccount` — إضافة حساب جديد (خطوة بخطوة)\n"
        "`/accounts` — عرض جميع الحسابات\n"
        "`/startacc <رقم>` — تشغيل حساب\n"
        "`/stopacc <رقم>` — إيقاف حساب\n"
        "`/removeacc <رقم>` — حذف حساب نهائياً\n"
        "`/setgroup <رقم> <group_id>` — تغيير مجموعة الهدف\n"
        "`/setmode <رقم> <forward/reply/both>` — تغيير الوضع\n\n"
        "🔗 **إدارة الجروبات**\n"
        "`/groups` — عرض الجروبات المخزنة\n"
        "`/addgroup <رابط>` — إضافة جروب\n"
        "`/delgroup <رابط>` — حذف جروب\n"
        "`/joingroups <رقم> [بداية]` — انضمام لجروبات\n"
        "`/stopjoin` — إيقاف الانضمام\n"
        "`/usergroups <رقم>` — حالة الانضمام\n\n"
        "🔑 **الكلمات المفتاحية**\n"
        "`/keywords` — عرض الكلمات\n"
        "`/addkw <كلمة> [فئة]` — إضافة كلمة\n"
        "`/delkw <كلمة>` — حذف كلمة\n\n"
        "💬 **الردود**\n"
        "`/replies` — عرض الردود\n"
        "`/addreply <نص>` — إضافة رد\n"
        "`/delreply <نص>` — حذف رد\n"
        "`/defaultreply` — تعديل الرد الافتراضي\n\n"
        "🛡 **الفلاتر**\n"
        "`/filters` — عرض الفلاتر\n"
        "`/togglefilter <name> on/off` — تفعيل/تعطيل\n\n"
        "🚫 **المحظورون**\n"
        "`/blocked` — عرض المحظورين\n"
        "`/block <user_id>` — حظر مستخدم\n"
        "`/unblock <user_id>` — فك الحظر\n\n"
        "⚙️ **النظام**\n"
        "`/stats` — إحصائيات كاملة\n"
        "`/health` — فحص صحة النظام\n"
        "`/config` — عرض الإعدادات\n"
        "`/setconfig <key> <value>` — تعديل إعداد\n"
        "`/backup` — نسخ احتياطي\n"
        "`/restart` — إعادة تشغيل\n"
        "`/menu` — القائمة الرئيسية\n\n"
        "═══════════════════\n"
        "🎯 **كل شيء قابل للتحكم — لا حاجة لتعديل الكود!**"
    )
    await safe_reply(update, help_text)


# ─── Account Management ───

@require_auth
async def cmd_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await safe_reply(
        update,
        "📱 **إضافة حساب جديد**\n\n"
        "الخطوة 1/5: أرسل رقم الهاتف (مع كود الدولة):\n"
        "مثال: `+966500000000`",
        reply_markup=ReplyKeyboardRemove(),
    )
    return STATE_ADD_PHONE


async def state_add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not re.match(r"^\+\d{10,15}$", phone):
        await safe_reply(update, "⚠️ رقم غير صالح. أرسل الرقم مع كود الدولة (مثال: +966500000000):")
        return STATE_ADD_PHONE
    context.user_data["add_phone"] = phone
    await safe_reply(update, "الخطوة 2/5: أرسل API ID (رقم):")
    return STATE_ADD_API_ID


async def state_add_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        api_id = int(update.message.text.strip())
    except ValueError:
        await safe_reply(update, "⚠️ API ID يجب أن يكون رقمًا. حاول مرة أخرى:")
        return STATE_ADD_API_ID
    context.user_data["add_api_id"] = api_id
    await safe_reply(update, "الخطوة 3/5: أرسل API Hash:")
    return STATE_ADD_API_HASH


async def state_add_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    api_hash = update.message.text.strip()
    if len(api_hash) < 10:
        await safe_reply(update, "⚠️ API Hash قصير جداً. حاول مرة أخرى:")
        return STATE_ADD_API_HASH
    context.user_data["add_api_hash"] = api_hash
    await safe_reply(
        update,
        "الخطوة 4/5: أرسل ID المجموعة المستهدفة:\n"
        "(مثال: -1001234567890)",
    )
    return STATE_ADD_GROUP


async def state_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        group_id = int(update.message.text.strip())
    except ValueError:
        await safe_reply(update, "⚠️ يجب أن يكون رقمًا. حاول مرة أخرى:")
        return STATE_ADD_GROUP
    context.user_data["add_group"] = group_id

    buttons = [
        [InlineKeyboardButton("forward — إعادة توجيه فقط", callback_data="mode_forward")],
        [InlineKeyboardButton("reply — رد تلقائي فقط", callback_data="mode_reply")],
        [InlineKeyboardButton("both — الاثنين معاً", callback_data="mode_both")],
    ]
    await safe_reply(update, "الخطوة 5/5: اختر الوضع:", reply_markup=InlineKeyboardMarkup(buttons))
    return STATE_ADD_MODE


async def state_add_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode = query.data.replace("mode_", "")
    context.user_data["add_mode"] = mode

    phone = context.user_data["add_phone"]
    api_id = context.user_data["add_api_id"]
    api_hash = context.user_data["add_api_hash"]
    group_id = context.user_data["add_group"]

    db: Database = context.application.db
    await db.add_account(phone, api_id, api_hash, group_id, mode)

    await query.edit_message_text(
        f"⏳ **جاري الاتصال بـ {phone}...**\n\n"
        f"ستصلك رسالة Telegram بالكود.\n"
        f"أرسل الكود هنا (5-6 أرقام):"
    )

    # Start OTP flow
    asyncio.create_task(_start_otp_flow(context, phone, api_id, api_hash, group_id, mode, update))
    return STATE_ADD_OTP


async def _start_otp_flow(context, phone, api_id, api_hash, group_id, mode, update):
    """Background task to send OTP request via Telethon."""
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.errors import PhoneNumberInvalidError

        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        await client.send_code_request(phone)

        # Store OTP state
        context.application.otp_states[phone] = {
            "client": client,
            "step": "waiting_code",
            "api_id": api_id,
            "api_hash": api_hash,
            "target_group": group_id,
            "mode": mode,
            "chat_id": update.effective_chat.id,
        }
    except PhoneNumberInvalidError:
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ **{phone}: رقم غير صالح!**\n\nأرسل /addaccount للمحاولة مجدداً."
        )
        db = context.application.db
        await db.update_account_status(phone, AccountStatus.ERROR, "Invalid phone number")
    except Exception as e:
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ **خطأ:** `{e}`\n\nأرسل /addaccount للمحاولة مجدداً."
        )


async def state_add_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    if not re.match(r"^\d{5,6}$", code):
        await safe_reply(update, "⚠️ الكود يجب أن يكون 5-6 أرقام. أرسل الكود:")
        return STATE_ADD_OTP

    # Find matching OTP state
    db: Database = context.application.db
    for phone, otp_state in list(context.application.otp_states.items()):
        if otp_state.get("step") == "waiting_code":
            client = otp_state["client"]
            try:
                from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
                await client.sign_in(phone, code)
                await _finalize_account(context, phone, client, otp_state, update)
                return ConversationHandler.END
            except SessionPasswordNeededError:
                otp_state["step"] = "waiting_2fa"
                await safe_reply(update, f"🔐 **{phone} يحتاج رمز 2FA.**\n\nأرسل كلمة المرور الآن:")
                return STATE_ADD_2FA
            except PhoneCodeInvalidError:
                await safe_reply(update, f"❌ **كود خاطئ!**\n\nأرسل الكود الصحيح أو /cancel للإلغاء:")
                return STATE_ADD_OTP
            except PhoneCodeExpiredError:
                await safe_reply(update, f"❌ **الكود منتهي!**\n\nأرسل /addaccount للبدء من جديد.")
                context.application.otp_states.pop(phone, None)
                await client.disconnect()
                return ConversationHandler.END
            except Exception as e:
                await safe_reply(update, f"❌ **خطأ:** `{e}`\n\nأرسل /addaccount مجدداً.")
                context.application.otp_states.pop(phone, None)
                await client.disconnect()
                return ConversationHandler.END

    await safe_reply(update, "⚠️ لا يوجد طلب OTP نشط. أرسل /addaccount للبدء.")
    return ConversationHandler.END


async def state_add_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    db: Database = context.application.db

    for phone, otp_state in list(context.application.otp_states.items()):
        if otp_state.get("step") == "waiting_2fa":
            client = otp_state["client"]
            try:
                await client.sign_in(password=password)
                await _finalize_account(context, phone, client, otp_state, update)
                return ConversationHandler.END
            except Exception as e:
                await safe_reply(update, f"❌ **خطأ في 2FA:** `{e}`\n\nحاول مرة أخرى أو /cancel:")
                return STATE_ADD_2FA

    await safe_reply(update, "⚠️ لا يوجد طلب 2FA نشط.")
    return ConversationHandler.END


async def _finalize_account(context, phone, client, otp_state, update):
    """Finalize account setup after successful OTP."""
    from telethon.sessions import StringSession
    from worker import AccountWorker

    db: Database = context.application.db
    state = context.application.bot_state

    try:
        me = await client.get_me()
        session_string = StringSession.save(client.session)
        await db.update_account_session(phone, session_string)
        await db.update_account_status(phone, AccountStatus.ACTIVE)

        # Create worker
        worker = AccountWorker(
            db=db,
            state=state,
            phone=phone,
            api_id=otp_state["api_id"],
            api_hash=otp_state["api_hash"],
            target_group_id=otp_state["target_group"],
            mode=otp_state["mode"],
            session_string=session_string,
        )
        success = await worker.connect()
        if success:
            state.bots.append(worker)
            context.application.otp_states.pop(phone, None)
            await safe_reply(
                update,
                f"✅ **{phone} تم الاتصال بنجاح!**\n\n"
                f"👤 الاسم: {me.first_name or ''} {me.last_name or ''}\n"
                f"🆔 ID: `{me.id}`\n"
                f"📊 الوضع: `{otp_state['mode']}`\n\n"
                f"الحساب جاهز للعمل! 🚀",
                reply_markup=get_main_menu(),
            )
        else:
            await safe_reply(update, f"⚠️ **تم حفظ الحساب لكن الاتصال فشل.**\n\nجرب `/startacc {phone}` لاحقاً.")
    except Exception as e:
        logger.error(f"Finalize error: {e}")
        await safe_reply(update, f"❌ **فشل في التفعيل:** `{e}`")
        context.application.otp_states.pop(phone, None)
        await client.disconnect()


@require_auth
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    accounts = await db.get_all_accounts()
    if not accounts:
        buttons = [[InlineKeyboardButton("➕ إضافة حساب", callback_data="menu_add_account")]]
        await safe_reply(update, "— لا توجد حسابات —", reply_markup=InlineKeyboardMarkup(buttons))
        return

    lines = ["📱 **قائمة الحسابات:**\n"]
    status_emoji = {
        "active": "🟢", "pending": "⏳", "paused": "⏸️",
        "error": "🔴", "banned": "🚫", "flood": "⏳", "connecting": "🔄",
    }
    buttons = []
    for acc in accounts:
        emoji = status_emoji.get(acc.status.value, "⚪️")
        connected = acc.last_connected.strftime("%Y-%m-%d %H:%M") if acc.last_connected else "—"
        lines.append(
            f"{emoji} **{acc.phone}**\n"
            f"   الحالة: `{acc.status.value}`\n"
            f"   المجموعة: `{acc.target_group_id}`\n"
            f"   الوضع: `{acc.mode}`\n"
            f"   آخر اتصال: `{connected}`\n"
            f"   ————————————————"
        )
        cb_data = f"acc_{acc.phone}"
        buttons.append(InlineKeyboardButton(f"{emoji} {acc.phone}", callback_data=cb_data))

    lines.append(f"\n📊 الإجمالي: {len(accounts)}")
    nav = [
        [InlineKeyboardButton("➕ إضافة", callback_data="menu_add_account"),
         InlineKeyboardButton("🔄 تحديث", callback_data="menu_accounts")],
    ]
    await safe_reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(build_menu(buttons, 2) + nav))


@require_auth
async def cmd_start_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    state = context.application.bot_state
    args = context.args

    if not args:
        await safe_reply(update, "⚠️ استخدم: `/startacc <رقم الهاتف>`")
        return

    phone = args[0].strip()
    acc = await db.get_account(phone)
    if not acc:
        await safe_reply(update, f"⚠️ الحساب غير موجود: `{phone}`")
        return

    existing = next((b for b in state.bots if b.phone == phone), None)
    if existing:
        await safe_reply(update, f"ℹ️ **{phone}** يعمل بالفعل!")
        return

    session = await db.load_session(phone)
    if not session:
        await safe_reply(update, f"❌ **{phone}**: لا يوجد سجل جلسة. استخدم /addaccount مجددًا.")
        return

    await safe_reply(update, f"⏳ جاري تشغيل **{phone}**...")

    try:
        from worker import AccountWorker
        worker = AccountWorker(
            db=db, state=state, phone=phone,
            api_id=acc.api_id, api_hash=acc.api_hash,
            target_group_id=acc.target_group_id, mode=acc.mode,
            session_string=session,
        )
        success = await worker.connect()
        if success:
            state.bots.append(worker)
            me = await worker.client.get_me()
            await safe_reply(
                update,
                f"✅ **{phone} تم التشغيل!**\n\n"
                f"👤 {me.first_name or ''} {me.last_name or ''}\n"
                f"🟢 الحالة: نشط",
            )
        else:
            await safe_reply(update, f"❌ **{phone}**: فشل الاتصال. تحقق من الـ session.")
    except Exception as e:
        await safe_reply(update, f"❌ **{phone}**: خطأ: `{e}`")
        await db.update_account_status(phone, AccountStatus.ERROR, str(e))


@require_auth
async def cmd_stop_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    state = context.application.bot_state
    args = context.args

    if not args:
        await safe_reply(update, "⚠️ استخدم: `/stopacc <رقم الهاتف>`")
        return

    phone = args[0].strip()
    bot = next((b for b in state.bots if b.phone == phone), None)
    if not bot:
        await safe_reply(update, f"⚠️ **{phone}** لا يعمل حاليًا.")
        return

    try:
        await bot.disconnect()
        state.bots.remove(bot)
        await safe_reply(update, f"⏸️ **{phone}** تم الإيقاف.")
    except Exception as e:
        await safe_reply(update, f"❌ **خطأ:** `{e}`")


@require_auth
async def cmd_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    state = context.application.bot_state
    args = context.args

    if not args:
        await safe_reply(update, "⚠️ استخدم: `/removeacc <رقم الهاتف>`")
        return

    phone = args[0].strip()
    bot = next((b for b in state.bots if b.phone == phone), None)
    if bot:
        await bot.disconnect()
        state.bots.remove(bot)
    await db.delete_account(phone)
    await safe_reply(update, f"🗑️ **{phone}** تم الحذف نهائيًا.")


@require_auth
async def cmd_set_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    state = context.application.bot_state
    args = context.args

    if len(args) < 2:
        await safe_reply(update, "⚠️ استخدم: `/setgroup <رقم> <group_id>`")
        return

    phone = args[0]
    try:
        group_id = int(args[1])
    except ValueError:
        await safe_reply(update, "❌ group_id يجب أن يكون رقمًا.")
        return

    await db.update_account_group(phone, group_id)
    bot = next((b for b in state.bots if b.phone == phone), None)
    if bot:
        bot.target_group_id = group_id
    await safe_reply(update, f"✅ **{phone}**: تم تحديث المجموعة إلى `{group_id}`.")


@require_auth
async def cmd_set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    state = context.application.bot_state
    args = context.args

    if len(args) < 2:
        await safe_reply(update, "⚠️ استخدم: `/setmode <رقم> <forward|reply|both>`")
        return

    phone = args[0]
    mode = args[1].lower()
    if mode not in ("forward", "reply", "both"):
        await safe_reply(update, "❌ الوضع يجب أن يكون: forward, reply, or both")
        return

    await db.update_account_mode(phone, mode)
    bot = next((b for b in state.bots if b.phone == phone), None)
    if bot:
        bot.mode = mode
    await safe_reply(update, f"✅ **{phone}**: الوضع تغير إلى `{mode}`.")


# ─── Groups ───

@require_auth
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    links = await db.get_all_groups()
    if not links:
        await safe_reply(update, "— لا توجد جروبات —")
        return

    msg = f"🔗 **الجروبات المخزنة ({len(links)}):**\n\n"
    for i, link in enumerate(links, 1):
        msg += f"{i}. {link}\n"

    buttons = [
        [InlineKeyboardButton("➕ إضافة", callback_data="menu_add_group"),
         InlineKeyboardButton("🗑 حذف", callback_data="menu_del_group")],
    ]
    await safe_reply(update, msg, reply_markup=InlineKeyboardMarkup(buttons))


@require_auth
async def cmd_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await safe_reply(update, "⚠️ استخدم: `/addgroup <رابط الجروب>`\nأو أرسل /groups لإدارة الجروبات.")
        return

    db: Database = context.application.db
    link = args[0].strip()
    await db.add_group(link)
    await safe_reply(update, f"✅ تم إضافة: `{link}`")


@require_auth
async def cmd_del_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await safe_reply(update, "⚠️ استخدم: `/delgroup <رابط الجروب>`")
        return

    db: Database = context.application.db
    link = args[0].strip()
    await db.delete_group(link)
    await safe_reply(update, f"✅ تم حذف: `{link}`")


@require_auth
async def cmd_join_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db: Database = context.application.db
    state = context.application.bot_state
    args = context.args

    if not args:
        # Show available accounts
        accs = [f"- {b.phone}" for b in state.bots]
        if not accs:
            await safe_reply(update, "⚠️ لا توجد حسابات نشطة.")
            return ConversationHandler.END
        msg = "**الحسابات المتاحة:**\n" + "\n".join(accs) + "\n\n✍️ أرسل رقم الحساب:"
        await safe_reply(update, msg)
        return STATE_JOIN_PHONE

    phone = args[0]
    start_index = int(args[1]) - 1 if len(args) > 1 and args[1].isdigit() else 0
    await _do_join(update, context, phone, start_index)
    return ConversationHandler.END


async def state_join_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    context.user_data["join_phone"] = phone
    await safe_reply(update, "✍️ أرسل رقم البداية (أو ارسل 1 للبدء من الأول):")
    return STATE_JOIN_START


async def state_join_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        start = int(update.message.text.strip())
    except ValueError:
        start = 1
    phone = context.user_data.get("join_phone", "")
    await _do_join(update, context, phone, start - 1)
    return ConversationHandler.END


async def _do_join(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str, start_index: int):
    state = context.application.bot_state
    target_bot = next((b for b in state.bots if b.phone == phone), None)
    if not target_bot:
        await safe_reply(update, f"⚠️ لا يوجد حساب نشط: `{phone}`")
        return

    state.stop_joining_flags[phone] = False
    task = asyncio.create_task(target_bot.join_groups_with_account(start_index))
    state.joining_now[phone] = task
    await safe_reply(update, f"🚀 بدأ **{phone}** بالانضمام من رقم {start_index + 1}!")


@require_auth
async def cmd_stop_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.application.bot_state
    if not state.joining_now:
        await safe_reply(update, "لا توجد عمليات انضمام حالية.")
        return

    args = context.args
    if args and args[0].lower() == "all":
        for p in list(state.joining_now):
            state.stop_joining_flags[p] = True
        await safe_reply(update, "⏹ تم إيقاف **كل** عمليات الانضمام.")
    elif args:
        phone = args[0]
        if phone in state.joining_now:
            state.stop_joining_flags[phone] = True
            await safe_reply(update, f"⏹ تم إيقاف **{phone}**.")
        else:
            await safe_reply(update, f"⚠️ لا توجد عملية نشطة للحساب: `{phone}`")
    else:
        await safe_reply(update, "استخدم: `/stopjoin all` أو `/stopjoin <رقم>`")


@require_auth
async def cmd_user_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    state = context.application.bot_state
    args = context.args

    if not args:
        await safe_reply(update, "⚠️ استخدم: `/usergroups <رقم>`")
        return

    phone = args[0].strip()
    target_bot = next((b for b in state.bots if b.phone == phone), None)
    if not target_bot:
        await safe_reply(update, f"⚠️ الحساب غير موجود: `{phone}`")
        return

    await safe_reply(update, f"⏳ جاري الفحص...")
    in_g, not_in = await target_bot.user_groups_status()
    msg = (
        f"🔢 **{phone}**\n\n"
        f"✅ عضو في: {len(in_g)}\n"
        f"❌ خارج: {len(not_in)}\n"
        f"📊 الإجمالي: {len(in_g) + len(not_in)}"
    )
    if not_in:
        msg += f"\n\n**الجروبات غير المنتسب لها:**\n" + "\n".join(not_in[:20])
    await safe_reply(update, msg)


# ─── Keywords ───

@require_auth
async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    keywords = await db.get_keywords()
    if not keywords:
        buttons = [[InlineKeyboardButton("➕ إضافة كلمة", callback_data="menu_add_kw")]]
        await safe_reply(update, "— لا توجد كلمات مفتاحية —", reply_markup=InlineKeyboardMarkup(buttons))
        return

    msg = f"🔑 **الكلمات المفتاحية ({len(keywords)}):**\n\n"
    msg += "\n".join(f"• `{k}`" for k in keywords[:50])
    if len(keywords) > 50:
        msg += f"\n\n... و {len(keywords) - 50} كلمة أخرى"

    buttons = [
        [InlineKeyboardButton("➕ إضافة", callback_data="menu_add_kw"),
         InlineKeyboardButton("🗑 حذف", callback_data="menu_del_kw")],
    ]
    await safe_reply(update, msg, reply_markup=InlineKeyboardMarkup(buttons))


@require_auth
async def cmd_add_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await safe_reply(update, "⚠️ استخدم: `/addkw <كلمة> [فئة]`")
        return

    db: Database = context.application.db
    word = args[0]
    category = args[1] if len(args) > 1 else "general"
    await db.add_keyword(word, category)
    await safe_reply(update, f"✅ تم إضافة: `{word}` (الفئة: {category})")


@require_auth
async def cmd_del_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await safe_reply(update, "⚠️ استخدم: `/delkw <كلمة>`")
        return

    db: Database = context.application.db
    word = args[0]
    await db.remove_keyword(word)
    await safe_reply(update, f"✅ تم حذف: `{word}`")


# ─── Replies ───

@require_auth
async def cmd_replies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    replies = await db.load_table("auto_reply_responses")
    if not replies:
        await safe_reply(update, "— لا توجد ردود —")
        return

    msg = f"💬 **الردود التلقائية ({len(replies)}):**\n\n"
    for i, r in enumerate(replies[:20], 1):
        msg += f"{i}. `{r[:50]}{'...' if len(r) > 50 else ''}`\n"
    if len(replies) > 20:
        msg += f"\n... و {len(replies) - 20} رد آخر"
    await safe_reply(update, msg)


@require_auth
async def cmd_add_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await safe_reply(update, "⚠️ استخدم: `/addreply <النص>`")
        return

    db: Database = context.application.db
    text = " ".join(args)
    await db.insert_table("auto_reply_responses", text)
    context.application.bot_state.auto_replies.append(text)
    await safe_reply(update, f"✅ تم إضافة الرد.")


@require_auth
async def cmd_del_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await safe_reply(update, "⚠️ استخدم: `/delreply <النص>`")
        return

    db: Database = context.application.db
    text = " ".join(args)
    await db.delete_table("auto_reply_responses", text)
    if text in context.application.bot_state.auto_replies:
        context.application.bot_state.auto_replies.remove(text)
    await safe_reply(update, f"✅ تم حذف الرد.")


@require_auth
async def cmd_default_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    args = context.args

    if not args:
        current = await db.get_setting("default_auto_reply", "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك")
        await safe_reply(
            update,
            f"💬 **الرد الافتراضي الحالي:**\n`{current}`\n\n"
            f"المتغيرات: `{{first_name}}`, `{{last_name}}`, `{{username}}`, `{{user_id}}`\n\n"
            f"للتعديل أرسل: `/defaultreply <النص الجديد>`"
        )
        return

    text = " ".join(args)
    await db.set_setting("default_auto_reply", text, update.effective_user.id)
    await safe_reply(update, f"✅ تم تحديث الرد الافتراضي.")


# ─── Filters ───

@require_auth
async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    filters_data = await db.get_filters()
    if not filters_data:
        await safe_reply(update, "— لا توجد فلاتر —")
        return

    lines = ["🛡 **فلاتر التجاهل:**\n"]
    for name, (active, threshold) in filters_data.items():
        emoji = "✅" if active else "❌"
        thresh = f" (حد: {threshold})" if threshold else ""
        lines.append(f"{emoji} `{name}`{thresh}")

    buttons = [
        [InlineKeyboardButton("تفعيل الكل", callback_data="filters_on"),
         InlineKeyboardButton("تعطيل الكل", callback_data="filters_off")],
    ]
    await safe_reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


@require_auth
async def cmd_toggle_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await safe_reply(update, "⚠️ استخدم: `/togglefilter <name> on/off`\nالفلاتر: mention, links, digits, private, outgoing, bots, admins, word_count")
        return

    db: Database = context.application.db
    filter_name = args[0]
    is_active = args[1].lower() in ("on", "true", "1", "yes")
    await db.toggle_filter(filter_name, is_active)
    status = "✅ مفعل" if is_active else "❌ معطل"
    await safe_reply(update, f"{status} الفلتر: `{filter_name}`")


# ─── Blocked Users ───

@require_auth
async def cmd_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    rows = await db.list_blocked_users(limit=50)
    if not rows:
        await safe_reply(update, "— لا يوجد محظورون —")
        return

    msg = f"🚫 **المستخدمون المحظورون ({len(rows)} معروض):**\n\n"
    for i, r in enumerate(rows, 1):
        msg += (
            f"{i}. 👤 `{r['user_id']}`"
            f"{' | @' + r['username'] if r['username'] else ''}\n"
            f"   📝 {r['display_name'] or '—'}\n"
        )

    await safe_reply(update, msg)


@require_auth
async def cmd_block_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    db: Database = context.application.db
    state = context.application.bot_state

    if update.message.reply_to_message:
        # Block by reply
        replied = update.message.reply_to_message
        uid = replied.from_user.id if replied.from_user else None
        uname = replied.from_user.username if replied.from_user else ""
        dname = replied.from_user.full_name if replied.from_user else ""
    elif args:
        try:
            uid = int(args[0])
        except ValueError:
            await safe_reply(update, "❌ user_id يجب أن يكون رقمًا.")
            return
        uname = args[1] if len(args) > 1 else ""
        dname = " ".join(args[2:]) if len(args) > 2 else ""
    else:
        await safe_reply(update, "⚠️ استخدم: `/block <user_id>` أو بالرد على رسالة.")
        return

    if not uid:
        await safe_reply(update, "❌ لا يمكن تحديد المستخدم.")
        return

    await db.add_blocked_user(uid, uname, dname)
    state.blocked_users[uid] = (uname, dname)
    await safe_reply(update, f"✅ تم حظر: `{uid}` @{uname or '—'}")


@require_auth
async def cmd_unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await safe_reply(update, "⚠️ استخدم: `/unblock <user_id>`")
        return

    try:
        uid = int(args[0])
    except ValueError:
        await safe_reply(update, "❌ user_id يجب أن يكون رقمًا.")
        return

    db: Database = context.application.db
    state = context.application.bot_state
    c = await db.del_blocked_user(uid)
    state.blocked_users.pop(uid, None)
    await safe_reply(update, f"✅ تم فك الحظر: `{uid}` (حُذف {c})")


# ─── System ───

@require_auth
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    try:
        s = await db.get_stats()
        msg = (
            "📊 **إحصائيات النظام:**\n\n"
            f"• 🟢 ردود مباشرة: **{s['direct']}**\n"
            f"• ⛔ جمل محظورة: **{s['blocked_text']}**\n"
            f"• 🚫 محظورون: **{s['blocked_users']}**\n"
            f"• 🔗 جروبات: **{s['groups']}**\n"
            f"• 🔑 كلمات مفتاحية: **{s['keywords']}**\n"
            f"• 🚫 مجموعات مستثناة: **{s['excluded_groups']}**\n"
            f"• 📱 إجمالي حسابات: **{s['accounts']}**\n"
            f"• 🟢 حسابات نشطة: **{s['active_accounts']}**\n"
            f"• 📋 مهام معلقة: **{s['pending_tasks']}**"
        )
        await safe_reply(update, msg)
    except Exception as e:
        await safe_reply(update, f"❌ تعذر جلب الإحصائيات: `{e}`")


@require_auth
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    state = context.application.bot_state

    db_healthy = await db.health_check()
    bots_status = [
        f"{'🟢' if b.is_connected() else '🔴'} {b.phone} ({b.mode})"
        for b in state.bots
    ]
    msg = (
        f"🏥 **حالة النظام**\n\n"
        f"🗄 قاعدة البيانات: {'✅ سليمة' if db_healthy else '❌ خطأ'}\n"
        f"🤖 الحسابات النشطة: {len(state.bots)}\n\n"
    )
    if bots_status:
        msg += "**الحسابات:**\n" + "\n".join(bots_status)
    else:
        msg += "— لا توجد حسابات نشطة —"

    msg += f"\n\n⚡ المهام الجارية: {len(state.joining_now)}"
    await safe_reply(update, msg)


@require_auth
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    settings = await db.get_all_settings()
    if not settings:
        await safe_reply(update, "— لا توجد إعدادات —")
        return

    lines = ["⚙️ **الإعدادات:**\n"]
    for s in settings:
        lines.append(f"• `{s['key']}` = `{s['value']}`\n  _{s['description'] or '—'}_")
    await safe_reply(update, "\n".join(lines))


@require_auth
async def cmd_set_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await safe_reply(update, "⚠️ استخدم: `/setconfig <key> <value>`\nمثال: `/setconfig rate_limit_max 6`")
        return

    db: Database = context.application.db
    key = args[0]
    value = args[1]
    await db.set_setting(key, value, update.effective_user.id)
    await safe_reply(update, f"✅ تم تحديث `{key}` = `{value}`")


@require_auth
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.db
    import gzip
    from pathlib import Path

    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    await safe_reply(update, "⏳ جاري إنشاء النسخة الاحتياطية...")

    try:
        data = await db.get_all_for_backup()
        payload = {
            "meta": {"version": 6, "created_at": datetime.datetime.utcnow().isoformat() + "Z", "db_type": "postgresql"},
            "tables": data,
        }
        buf = BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        buf.seek(0)

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=buf,
            filename=f"backup_{ts}.json.gz",
            caption=f"✅ نسخة احتياطية\nالتاريخ: {ts}",
        )
    except Exception as e:
        await safe_reply(update, f"❌ فشل النسخ الاحتياطي: `{e}`")


@require_auth
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(update, "🔄 **جاري إعادة التشغيل...**\n\nسيتم إعادة تشغيل الحسابات.")
    state = context.application.bot_state

    # Stop all bots
    for b in list(state.bots):
        try:
            await b.disconnect()
        except Exception:
            pass
    state.bots.clear()

    # Restart
    accounts = await context.application.db.get_all_accounts()
    started = 0
    for acc in accounts:
        if acc.status == AccountStatus.ACTIVE and acc.session_string:
            try:
                from worker import AccountWorker
                worker = AccountWorker(
                    db=context.application.db, state=state,
                    phone=acc.phone, api_id=acc.api_id, api_hash=acc.api_hash,
                    target_group_id=acc.target_group_id, mode=acc.mode,
                    session_string=acc.session_string,
                )
                if await worker.connect():
                    state.bots.append(worker)
                    started += 1
            except Exception as e:
                logger.error(f"Restart failed for {acc.phone}: {e}")

    await safe_reply(update, f"✅ **تمت إعادة التشغيل!**\n🟢 {started} حساب نشط.")


@require_auth
async def cmd_unblock_spam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.application.bot_state
    for b in state.bots:
        asyncio.create_task(b.unblock_spambot())
    await safe_reply(update, "✓ تم إرسال /start إلى @SpamBot لكل الحسابات.")


# ─── Callback Query Handler ───

@require_auth
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_accounts":
        await cmd_accounts(update, context)
    elif data == "menu_settings":
        await cmd_config(update, context)
    elif data == "menu_keywords":
        await cmd_keywords(update, context)
    elif data == "menu_filters":
        await cmd_filters(update, context)
    elif data == "menu_groups":
        await cmd_groups(update, context)
    elif data == "menu_replies":
        await cmd_replies(update, context)
    elif data == "menu_blocked":
        await cmd_blocked(update, context)
    elif data == "menu_stats":
        await cmd_stats(update, context)
    elif data == "menu_health":
        await cmd_health(update, context)
    elif data == "menu_help":
        await cmd_help(update, context)
    elif data == "menu_add_account":
        await query.edit_message_text("أرسل /addaccount لإضافة حساب جديد.")
    elif data == "menu_add_group":
        await query.edit_message_text("أرسل /addgroup <رابط> لإضافة جروب.")
    elif data == "menu_del_group":
        await query.edit_message_text("أرسل /delgroup <رابط> لحذف جروب.")
    elif data == "menu_add_kw":
        await query.edit_message_text("أرسل /addkw <كلمة> لإضافة كلمة مفتاحية.")
    elif data == "menu_del_kw":
        await query.edit_message_text("أرسل /delkw <كلمة> لحذف كلمة مفتاحية.")
    elif data == "menu_main":
        await show_main_menu(update, context)
    elif data.startswith("acc_"):
        phone = data.replace("acc_", "")
        db = context.application.db
        acc = await db.get_account(phone)
        if acc:
            status_emoji = {"active": "🟢", "pending": "⏳", "paused": "⏸️", "error": "🔴", "banned": "🚫"}
            emoji = status_emoji.get(acc.status.value, "⚪️")
            msg = (
                f"📱 **{acc.phone}**\n\n"
                f"الحالة: {emoji} `{acc.status.value}`\n"
                f"المجموعة: `{acc.target_group_id}`\n"
                f"الوضع: `{acc.mode}`\n"
                f"آخر خطأ: `{acc.last_error or '—'}`\n"
            )
            buttons = [
                [InlineKeyboardButton("▶️ تشغيل", callback_data=f"start_{phone}"),
                 InlineKeyboardButton("⏸️ إيقاف", callback_data=f"stop_{phone}")],
                [InlineKeyboardButton("🗑 حذف", callback_data=f"remove_{phone}"),
                 InlineKeyboardButton("🔙 رجوع", callback_data="menu_accounts")],
            ]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("start_"):
        phone = data.replace("start_", "")
        context.args = [phone]
        await cmd_start_account(update, context)
    elif data.startswith("stop_"):
        phone = data.replace("stop_", "")
        context.args = [phone]
        await cmd_stop_account(update, context)
    elif data.startswith("remove_"):
        phone = data.replace("remove_", "")
        context.args = [phone]
        await cmd_remove_account(update, context)
    elif data.startswith("mode_"):
        await state_add_mode_callback(update, context)
    elif data == "filters_on":
        db = context.application.db
        for name in ["mention", "links", "digits", "private", "outgoing", "bots", "admins"]:
            await db.toggle_filter(name, True)
        await cmd_filters(update, context)
    elif data == "filters_off":
        db = context.application.db
        for name in ["mention", "links", "digits", "private", "outgoing", "bots", "admins"]:
            await db.toggle_filter(name, False)
        await cmd_filters(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await safe_reply(update, "❌ تم الإلغاء.", reply_markup=get_main_menu())
    return ConversationHandler.END


# ─── Setup ───

def setup_controller_application(db: Database, state: Any) -> Application:
    """Build and configure the controller bot application."""
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        logger.error("BOT_TOKEN not set! Get one from @BotFather")
        sys.exit(1)

    application = Application.builder().token(token).build()
    application.db = db
    application.bot_state = state
    application.otp_states = {}

    # Conversation handler for account addition
    add_account_conv = ConversationHandler(
        entry_points=[CommandHandler("addaccount", cmd_add_account)],
        states={
            STATE_ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_add_phone)],
            STATE_ADD_API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_add_api_id)],
            STATE_ADD_API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_add_api_hash)],
            STATE_ADD_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_add_group)],
            STATE_ADD_MODE: [CallbackQueryHandler(state_add_mode_callback, pattern=r"^mode_")],
            STATE_ADD_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_add_otp)],
            STATE_ADD_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_add_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # Conversation handler for join groups
    join_conv = ConversationHandler(
        entry_points=[CommandHandler("joingroups", cmd_join_groups)],
        states={
            STATE_JOIN_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_join_phone)],
            STATE_JOIN_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_join_start)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # Auth conversation (password)
    auth_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            STATE_SET_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_set_password)],
            STATE_AUTH_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, state_auth_password)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # Register handlers
    application.add_handler(auth_conv)
    application.add_handler(add_account_conv)
    application.add_handler(join_conv)

    # Command handlers
    commands = [
        ("menu", cmd_menu),
        ("help", cmd_help),
        ("accounts", cmd_accounts),
        ("startacc", cmd_start_account),
        ("stopacc", cmd_stop_account),
        ("removeacc", cmd_remove_account),
        ("setgroup", cmd_set_group),
        ("setmode", cmd_set_mode),
        ("groups", cmd_groups),
        ("addgroup", cmd_add_group),
        ("delgroup", cmd_del_group),
        ("stopjoin", cmd_stop_join),
        ("usergroups", cmd_user_groups),
        ("keywords", cmd_keywords),
        ("addkw", cmd_add_keyword),
        ("delkw", cmd_del_keyword),
        ("replies", cmd_replies),
        ("addreply", cmd_add_reply),
        ("delreply", cmd_del_reply),
        ("defaultreply", cmd_default_reply),
        ("filters", cmd_filters),
        ("togglefilter", cmd_toggle_filter),
        ("blocked", cmd_blocked),
        ("block", cmd_block_user),
        ("unblock", cmd_unblock_user),
        ("stats", cmd_stats),
        ("health", cmd_health),
        ("config", cmd_config),
        ("setconfig", cmd_set_config),
        ("backup", cmd_backup),
        ("restart", cmd_restart),
        ("unblockspam", cmd_unblock_spam),
    ]

    for cmd_name, handler in commands:
        application.add_handler(CommandHandler(cmd_name, handler))

    application.add_handler(CallbackQueryHandler(callback_handler))

    # Set bot commands
    asyncio.create_task(_set_commands(application))

    return application


async def _set_commands(application: Application):
    """Set bot command menu in Telegram."""
    await asyncio.sleep(2)
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "بدء البوت وإدخال كلمة المرور"),
            BotCommand("menu", "القائمة الرئيسية"),
            BotCommand("help", "دليل الأوامر الكامل"),
            BotCommand("addaccount", "إضافة حساب جديد (تفاعلي)"),
            BotCommand("accounts", "عرض الحسابات"),
            BotCommand("startacc", "تشغيل حساب"),
            BotCommand("stopacc", "إيقاف حساب"),
            BotCommand("removeacc", "حذف حساب"),
            BotCommand("groups", "عرض الجروبات"),
            BotCommand("addgroup", "إضافة جروب"),
            BotCommand("delgroup", "حذف جروب"),
            BotCommand("joingroups", "الانضمام للجروبات"),
            BotCommand("stopjoin", "إيقاف الانضمام"),
            BotCommand("keywords", "عرض الكلمات المفتاحية"),
            BotCommand("addkw", "إضافة كلمة مفتاحية"),
            BotCommand("delkw", "حذف كلمة مفتاحية"),
            BotCommand("replies", "عرض الردود"),
            BotCommand("defaultreply", "تعديل الرد الافتراضي"),
            BotCommand("filters", "عرض الفلاتر"),
            BotCommand("blocked", "عرض المحظورين"),
            BotCommand("block", "حظر مستخدم"),
            BotCommand("unblock", "فك حظر"),
            BotCommand("stats", "إحصائيات النظام"),
            BotCommand("health", "فحص صحة النظام"),
            BotCommand("config", "عرض الإعدادات"),
            BotCommand("setconfig", "تعديل إعداد"),
            BotCommand("backup", "نسخة احتياطية"),
            BotCommand("restart", "إعادة تشغيل"),
            BotCommand("unblockspam", "فك حظر @SpamBot"),
        ])
        logger.info("Bot commands set")
    except Exception as e:
        logger.warning(f"Failed to set commands: {e}")
