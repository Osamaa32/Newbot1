"""
═══════════════════════════════════════════════════════════════════════════════
Telegram Multi-Account Bot — ULTIMATE CONTROLLER EDITION v6.0
═══════════════════════════════════════════════════════════════════════════════

Architecture:
  1. Controller Bot (python-telegram-bot) — Receives commands via @BotFather bot
  2. Account Workers (Telethon) — User accounts that monitor groups & auto-reply
  3. PostgreSQL Database — Persistent storage with connection pooling
  4. Async everything — High performance, handles thousands of messages

Features:
  - Password authentication on first startup
  - Complete control via Telegram bot (no command group needed)
  - Add/start/stop/remove accounts interactively
  - Keyword & filter management
  - Group join automation with circuit breaker
  - Auto-reply with rate limiting
  - Full backup/restore
  - Health monitoring dashboard
  - Ready for Railway deployment

Environment Variables:
  - DATABASE_URL: PostgreSQL connection string
  - BOT_TOKEN: Token from @BotFather
  - OWNER_ID (optional): Telegram user ID of owner

═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import signal
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
from aiohttp import web

# Setup logging first
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
logger = logging.getLogger("telegram-bot")

# Suppress noisy libraries
for noisy in ("telethon.network.mtprotosender", "telethon.client.downloads",
              "telegram.ext._application", "telegram._bot"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from database import Database
from models import AccountStatus
from utils import CircuitBreaker, Messenger, RateLimiter, TextUtils
from controller import setup_controller_application


# ─── Shared State ───

class BotState:
    """Shared state across all components."""

    def __init__(self) -> None:
        self.bots: List[Any] = []  # AccountWorker instances
        self.FORWARD_DONE = _TTLCacheSet(ttl_seconds=7200)
        self.REPLY_DONE = _TTLCacheSet(ttl_seconds=7200)
        self.PROCESS_LOCK = asyncio.Lock()
        self.dispatch_semaphore = asyncio.Semaphore(50)

        self.direct_triggers: List[str] = []
        self.blocked_phrases: List[str] = []
        self.auto_replies: List[str] = []
        self._auto_index = 0

        self.stop_joining_flags: Dict[str, bool] = {}
        self.joining_now: Dict[str, asyncio.Task] = {}

        self.blocked_users: Dict[int, tuple] = {}
        self.REPLY_LOCKS: Dict[Any, asyncio.Lock] = {}

        self.circuit_breaker = CircuitBreaker()
        self.rate_limiter = RateLimiter()

        # Fallback config (loaded from DB)
        self.fallback_group_id: int = -1002353780992

    def next_auto_reply(self, default: str) -> str:
        if not self.auto_replies:
            return default
        msg = self.auto_replies[self._auto_index]
        self._auto_index = (self._auto_index + 1) % len(self.auto_replies)
        return msg

    def get_reply_lock(self, key: Any) -> asyncio.Lock:
        if len(self.REPLY_LOCKS) > 5000:
            self.REPLY_LOCKS = dict(list(self.REPLY_LOCKS.items())[-1000:])
        return self.REPLY_LOCKS.setdefault(key, asyncio.Lock())


class _TTLCacheSet:
    """Simple TTL cache for deduplication."""
    def __init__(self, ttl_seconds: int = 7200):
        self.ttl = ttl_seconds
        self._data: OrderedDict = OrderedDict()
        self._access_count = 0

    def add(self, key: Any) -> None:
        now = time.time()
        self._evict_expired(now)
        self._data[key] = now + self.ttl
        self._data.move_to_end(key)
        self._access_count += 1

    def __contains__(self, key: Any) -> bool:
        self._evict_expired(time.time())
        return key in self._data

    def _evict_expired(self, now: float) -> None:
        if self._access_count % 1000 == 0:
            expired = [k for k, v in self._data.items() if v < now]
            for k in expired:
                self._data.pop(k, None)
        else:
            while self._data:
                key, expiry = next(iter(self._data.items()))
                if expiry < now:
                    self._data.pop(key)
                else:
                    break


# ─── Health Server ───

async def run_health_server(db: Database, port: int = 8080):
    """Lightweight health check server for Railway."""
    async def health_handler(request):
        healthy = await db.health_check()
        status = 200 if healthy else 503
        body = {"status": "healthy" if healthy else "unhealthy", "timestamp": time.time()}
        return web.json_response(body, status=status)

    async def ready_handler(request):
        return web.json_response({"ready": True})

    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", health_handler)
    app.router.add_get("/ready", ready_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server on port {port}")


# ─── Account Loader ───

async def load_accounts(db: Database, state: BotState):
    """Load and start all saved active accounts."""
    from worker import AccountWorker

    accounts = await db.get_all_accounts()
    logger.info(f"Found {len(accounts)} accounts in DB")

    started = 0
    for acc in accounts:
        if acc.status == AccountStatus.ACTIVE and acc.session_string:
            try:
                worker = AccountWorker(
                    db=db, state=state, phone=acc.phone,
                    api_id=acc.api_id, api_hash=acc.api_hash,
                    target_group_id=acc.target_group_id, mode=acc.mode,
                    session_string=acc.session_string,
                )
                if await worker.connect():
                    state.bots.append(worker)
                    started += 1
                    logger.info(f"Account {acc.phone} started")
            except Exception as e:
                logger.error(f"Failed to start {acc.phone}: {e}")
                await db.update_account_status(acc.phone, AccountStatus.ERROR, str(e))

    logger.info(f"Started {started}/{len(accounts)} accounts")
    return started


# ─── Main ───

async def main():
    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║  Telegram Bot Ultimate Controller v6.0   ║")
    logger.info("╚══════════════════════════════════════════╝")

    # Environment checks
    database_url = os.environ.get("DATABASE_URL", "")
    bot_token = os.environ.get("BOT_TOKEN", "")

    if not database_url:
        logger.error("DATABASE_URL not set!")
        sys.exit(1)
    if not bot_token:
        logger.error("BOT_TOKEN not set! Get one from @BotFather")
        sys.exit(1)

    # Convert Railway postgres:// to postgresql://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    # Initialize database
    db = Database(database_url)
    await db.init()

    # Initialize state
    state = BotState()

    # Load data from DB
    state.direct_triggers = await db.load_table("direct_reply_messages")
    state.blocked_phrases = await db.load_table("blocked_reply_messages")
    state.auto_replies = await db.load_table("auto_reply_responses")
    state.blocked_users = await db.blocked_users_map()

    # Load fallback group ID
    try:
        state.fallback_group_id = int(await db.get_setting("fallback_group_id", "-1002353780992"))
    except Exception:
        pass

    logger.info(f"Loaded: {len(state.direct_triggers)} triggers, {len(state.blocked_phrases)} blocked, "
                f"{len(state.auto_replies)} replies, {len(state.blocked_users)} blocked users")

    # Setup signal handlers
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    async def shutdown(sig=None):
        logger.info(f"Received signal {sig}, shutting down...")
        for phone, task in state.joining_now.items():
            task.cancel()
            state.stop_joining_flags[phone] = True
        for b in list(state.bots):
            try:
                await b.disconnect()
            except Exception:
                pass
        state.bots.clear()
        await db.close()
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s)))

    # Start health server
    health_port = int(os.environ.get("PORT", "8080"))
    asyncio.create_task(run_health_server(db, health_port))

    # Load saved accounts
    asyncio.create_task(load_accounts(db, state))

    # Start controller bot
    logger.info("Starting controller bot...")
    app = setup_controller_application(db, state)

    # Run bot polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("Bot is running! Send /start to your bot.")

    # Wait for shutdown
    await shutdown_event.wait()

    await app.stop()
    await app.shutdown()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)
