"""
═══════════════════════════════════════════════════════════════
  CONTROL BOT — python-telegram-bot Main Application
═══════════════════════════════════════════════════════════════
"""

import logging
import asyncio
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters,
    ContextTypes,
)

from config.settings import get_settings
from bot.handlers import BotHandlers, AUTH_PASSWORD

logger = logging.getLogger(__name__)

# ─── Middleware: Authentication ───

async def auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is authenticated"""
    user_id = update.effective_user.id
    settings = get_settings()

    # Owner always allowed
    if settings.OWNER_ID and str(user_id) == str(settings.OWNER_ID):
        return True

    # Check session
    from bot.handlers import BotHandlers
    # We check via the handlers instance stored in bot_data
    handlers = context.bot_data.get("handlers")
    if handlers:
        from shared.database import AdminSessionRepository
        from shared.models import init_engine, SessionLocal

        db_pool = context.bot_data.get("db_pool")
        if db_pool:
            async with db_pool() as db:
                repo = AdminSessionRepository(db)
                is_auth = await repo.is_authenticated(user_id)
                if is_auth:
                    return True

    # Not authenticated
    if update.message:
        await update.message.reply_text(
            "🔐 **غير مصرح!**\n\n"
            "أرسل /start للمصادقة أولاً.",
            parse_mode="Markdown",
        )
    return False


class AuthenticatedCommandHandler(CommandHandler):
    """Command handler with authentication check"""

    def __init__(self, command, callback, **kwargs):
        self._original_callback = callback
        super().__init__(command, self._wrapped_callback, **kwargs)

    async def _wrapped_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await auth_middleware(update, context):
            await self._original_callback(update, context)


class ControlBot:
    """Telegram Control Bot Application"""

    def __init__(self, db_pool, engine_manager, settings):
        self.db_pool = db_pool
        self.engine = engine_manager
        self.settings = settings
        self.application: Optional[Application] = None
        self.handlers = BotHandlers(db_pool, engine_manager, settings)

    async def start(self):
        """Start the control bot"""
        logger.info("Starting Control Bot...")

        # Build application
        self.application = (
            Application.builder()
            .token(self.settings.BOT_TOKEN)
            .post_init(self._post_init)
            .build()
        )

        # Store references
        self.application.bot_data["db_pool"] = self.db_pool
        self.application.bot_data["engine"] = self.engine
        self.application.bot_data["handlers"] = self.handlers
        self.application.bot_data["settings"] = self.settings

        # ─── Register Handlers ───

        # Auth conversation
        auth_conv = ConversationHandler(
            entry_points=[CommandHandler("start", self.handlers.start)],
            states={
                AUTH_PASSWORD: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.handlers.auth_password),
                ],
            },
            fallbacks=[CommandHandler("cancel", self.handlers.cmd_cancel)],
            allow_reentry=True,
        )
        self.application.add_handler(auth_conv)

        # Menu handler (text buttons)
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handlers.menu_handler)
        )

        # Callback query handler
        self.application.add_handler(CallbackQueryHandler(self.handlers.callback_handler))

        # ─── Command Handlers (Authenticated) ───

        # Main commands
        self._add_cmd("menu", self.handlers._show_main_menu)
        self._add_cmd("help", self.handlers.cmd_help)
        self._add_cmd("accounts", self.handlers.cmd_accounts)
        self._add_cmd("stats", self.handlers.cmd_stats)
        self._add_cmd("status", self.handlers.cmd_engine_status)
        self._add_cmd("engine", self.handlers.cmd_engine_status)
        self._add_cmd("logs", self.handlers.cmd_logs)
        self._add_cmd("refresh", self.handlers.cmd_refresh)
        self._add_cmd("health", self.handlers.cmd_health)
        self._add_cmd("restart", self.handlers.cmd_restart)
        self._add_cmd("backup", self.handlers.cmd_backup)

        # Account commands
        self._add_cmd("startacc", self.handlers.cmd_start_account)
        self._add_cmd("stopacc", self.handlers.cmd_stop_account)
        self._add_cmd("removeacc", self.handlers.cmd_remove_account)

        # Keyword commands
        self._add_cmd("keywords", self.handlers.cmd_keywords_menu)
        self._add_cmd("addkw", self.handlers.cmd_add_keyword)
        self._add_cmd("delkw", self.handlers.cmd_del_keyword)
        self._add_cmd("listkw", self.handlers.cmd_list_keywords)

        # Group commands
        self._add_cmd("groups", self.handlers.cmd_groups_menu)
        self._add_cmd("addgroup", self.handlers.cmd_add_group)
        self._add_cmd("listgroups", self.handlers.cmd_list_groups)

        # Message commands
        self._add_cmd("send", self.handlers.cmd_send_message)

        # Settings commands
        self._add_cmd("settings", self.handlers.cmd_show_settings)
        self._add_cmd("set", self.handlers.cmd_set_setting)
        self._add_cmd("replyset", self.handlers.cmd_set_reply)
        self._add_cmd("replyshow", self.handlers.cmd_show_reply)

        # Block commands
        self._add_cmd("block", self.handlers.cmd_block_user)
        self._add_cmd("unblock", self.handlers.cmd_unblock_user)
        self._add_cmd("blocked", self.handlers.cmd_blocked_users)

        # Cancel
        self._add_cmd("cancel", self.handlers.cmd_cancel, authenticated=False)

        # ─── Start ───
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)

        logger.info("✅ Control Bot started!")

        # Send startup message to owner
        if self.settings.OWNER_ID:
            try:
                await self.application.bot.send_message(
                    chat_id=self.settings.OWNER_ID,
                    text=(
                        "🤖 **بوت التحكم اشتغل!**\n\n"
                        "✅ المحرك جاهز\n"
                        f"📱 الحسابات النشطة: {len(self.engine.accounts)}\n"
                        f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Could not send startup message: {e}")

    async def stop(self):
        """Stop the control bot"""
        if self.application:
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Control Bot stopped.")

    async def _post_init(self, application: Application):
        """Post initialization hook"""
        logger.info("Bot post-initialized")

    def _add_cmd(self, command, handler, authenticated: bool = True):
        """Add command handler"""
        if authenticated:
            self.application.add_handler(
                AuthenticatedCommandHandler(command, handler)
            )
        else:
            self.application.add_handler(CommandHandler(command, handler))

    import datetime

    # ─── Webhook Support (for Railway) ───

    async def setup_webhook(self, webhook_url: str, port: int = 8080):
        """Setup webhook for production (Railway)"""
        await self.application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
        await self.application.initialize()
        await self.application.start()
        logger.info(f"Webhook set: {webhook_url}")
