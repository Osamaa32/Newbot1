"""
═══════════════════════════════════════════════════════════════
  TELEGRAM MULTI-ACCOUNT CONTROL SYSTEM — Main Entry Point
═══════════════════════════════════════════════════════════════

  🎯 One Bot to Control Them All
  📱 Account Management: Add/Start/Stop/Remove via Telegram
  ⚙️ Full Settings Control: Everything is command-controllable
  🎛 Interactive Dashboard: Web UI with real-time updates
  🛡 Secure: Password authentication + owner verification
  🚀 Production Ready: Railway + Docker support

═══════════════════════════════════════════════════════════════
"""

import asyncio
import logging
import sys
import os
from pathlib import Path

# ─── Logging Setup ───

def setup_logging():
    """Configure application logging"""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format))

    # File handler
    Path("logs").mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/bot.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(log_format))

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Reduce noise from libraries
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return root_logger


# ─── Main Application ───

class Application:
    """Main application orchestrator"""

    def __init__(self):
        self.logger = logging.getLogger("App")
        self.db_engine = None
        self.engine_manager = None
        self.control_bot = None
        self.api_server = None
        self._shutdown_event = asyncio.Event()

    async def initialize(self):
        """Initialize all components"""
        self.logger.info("=" * 60)
        self.logger.info("  Telegram Multi-Account Control System v6.0")
        self.logger.info("=" * 60)

        # Load settings
        from config.settings import get_settings
        settings = get_settings()

        self.logger.info(f"Environment: {settings.ENVIRONMENT}")
        self.logger.info(f"Database: {settings.DATABASE_URL.split('@')[-1] if settings.DATABASE_URL else 'Not configured'}")

        # Initialize database
        self.logger.info("Initializing database...")
        from shared.models import init_engine, create_tables, seed_defaults, SessionLocal
        self.db_engine = init_engine(settings.DATABASE_URL, pool_size=settings.db_pool_size)
        await create_tables()

        # Seed defaults
        async with SessionLocal() as db:
            await seed_defaults(db)
        self.logger.info("Database initialized")

        # Create database session pool function
        async def db_pool():
            async with SessionLocal() as session:
                yield session

        # Initialize engine manager
        self.logger.info("Initializing account engine...")
        from engine.manager import AccountManager

        # Create engine manager with a fresh session
        from sqlalchemy.ext.asyncio import AsyncSession
        async with SessionLocal() as db:
            self.engine_manager = AccountManager(db)
            await self.engine_manager.initialize()

            # Auto-start saved accounts
            if settings.AUTO_START_ACCOUNTS:
                from shared.database import AccountRepository
                from shared.models import AccountStatus
                repo = AccountRepository(db)
                accounts = await repo.get_all()
                active_accounts = [a for a in accounts if a.status == AccountStatus.ACTIVE.value]

                if active_accounts:
                    self.logger.info(f"Auto-starting {len(active_accounts)} accounts...")
                    for acc in active_accounts:
                        success, msg = await self.engine_manager.start_account(acc.phone)
                        self.logger.info(f"  {acc.phone}: {msg}")
                        await asyncio.sleep(1)  # Delay between starts
                else:
                    self.logger.info("No accounts to auto-start. Use bot to add accounts.")

        # Initialize control bot
        self.logger.info("Starting control bot...")
        from bot.main import ControlBot
        self.control_bot = ControlBot(db_pool, self.engine_manager, settings)
        await self.control_bot.start()

        # Start API server
        self.logger.info("Starting API server...")
        from api.server import create_api_app
        import uvicorn

        api_app = create_api_app(db_pool, self.engine_manager, settings)

        # Run API server in background
        config = uvicorn.Config(
            api_app,
            host=settings.API_HOST,
            port=settings.API_PORT,
            log_level="warning",
        )
        self.api_server = uvicorn.Server(config)
        asyncio.create_task(self.api_server.serve())

        # Health check server
        from aiohttp import web
        async def health_handler(request):
            from shared.models import SessionLocal
            try:
                async with SessionLocal() as db:
                    from sqlalchemy import text
                    await db.execute(text("SELECT 1"))
                return web.Response(text="OK", status=200)
            except Exception as e:
                return web.Response(text=f"Error: {e}", status=503)

        health_app = web.Application()
        health_app.router.add_get("/health", health_handler)
        health_runner = web.AppRunner(health_app)
        await health_runner.setup()
        health_site = web.TCPSite(health_runner, "0.0.0.0", settings.HEALTH_PORT)
        await health_site.start()

        self.logger.info(f"Health check on port {settings.HEALTH_PORT}")

        # Send startup notification
        if settings.OWNER_ID:
            try:
                import telegram
                bot = telegram.Bot(token=settings.BOT_TOKEN)
                await bot.send_message(
                    chat_id=settings.OWNER_ID,
                    text=(
                        "🚀 **System Started!**\n\n"
                        f"📱 Accounts: {len(self.engine_manager.accounts)} active\n"
                        f"⚙️ Engine: Running\n"
                        f"🌐 API: http://{settings.API_HOST}:{settings.API_PORT}\n"
                        f"🏥 Health: port {settings.HEALTH_PORT}\n\n"
                        f"Send /start to the bot to begin."
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                self.logger.warning(f"Could not send startup notification: {e}")

        self.logger.info("=" * 60)
        self.logger.info("  System Ready!")
        self.logger.info("=" * 60)

        # Keep running
        await self._shutdown_event.wait()

    async def shutdown(self):
        """Graceful shutdown"""
        self.logger.info("Shutting down...")

        # Stop control bot
        if self.control_bot:
            await self.control_bot.stop()

        # Stop engine manager
        if self.engine_manager:
            await self.engine_manager.shutdown()

        # Stop API server
        if self.api_server:
            self.api_server.should_exit = True

        # Close database
        if self.db_engine:
            await self.db_engine.dispose()

        self._shutdown_event.set()
        self.logger.info("Shutdown complete.")


# ─── Entry Point ───

async def main():
    """Main entry point"""
    setup_logging()
    app = Application()

    # Handle signals
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(app.shutdown()))

    try:
        await app.initialize()
    except KeyboardInterrupt:
        await app.shutdown()
    except Exception as e:
        logging.getLogger("Main").exception("Fatal error", exc_info=True)
        sys.exit(1)


import signal

if __name__ == "__main__":
    asyncio.run(main())
