"""
═══════════════════════════════════════════════════════════════
  ACCOUNT MANAGER — Multi-Account Engine Coordinator
═══════════════════════════════════════════════════════════════
"""

import asyncio
import logging
import random
import datetime
from typing import Dict, List, Optional, Tuple, Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from shared.models import (
    Account, AccountStatus, AccountMode,
    Group, Keyword, TriggerPhrase, BlockedUser,
    ExcludedGroup, BotSetting,
)
from shared.database import (
    AccountRepository, GroupRepository, KeywordRepository,
    TriggerPhraseRepository, BlockedUserRepository,
    ExcludedGroupRepository, BotSettingRepository,
)
from engine.utils import TTLCache, RateLimiter, CircuitBreaker, TextUtils
from engine.account import AccountEngine

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages multiple Telegram accounts"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session
        self.accounts: Dict[str, AccountEngine] = {}
        self.db_cache: Dict[str, Any] = {}

        # Shared caches
        self.forward_cache = TTLCache(ttl_seconds=7200)
        self.reply_cache = TTLCache(ttl_seconds=7200)

        # Rate limiter
        self.rate_limiter = RateLimiter(max_requests=4, window=3600)

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=1800)

        # Background tasks
        self._refresh_task = None
        self._running = False

        # Config
        self.filters: Dict[str, Any] = {}
        self.keywords: List[str] = []
        self.excluded_groups: set = set()
        self.blocked_users: Dict[int, Tuple[str, str]] = {}
        self.direct_triggers: List[str] = []
        self.blocked_phrases: List[str] = []
        self.auto_replies: List[str] = []

        # Event callbacks
        self.on_status_change: Optional[Callable] = None
        self.on_message: Optional[Callable] = None

    async def initialize(self) -> None:
        """Initialize the manager - load configs and start accounts"""
        logger.info("Initializing AccountManager...")

        # Load all configuration
        await self._refresh_config()

        # Start background refresh
        self._running = True
        self._refresh_task = asyncio.create_task(self._periodic_refresh())

        logger.info(f"AccountManager initialized with {len(self.accounts)} active accounts")

    async def shutdown(self) -> None:
        """Shutdown all accounts"""
        logger.info("Shutting down AccountManager...")
        self._running = False

        if self._refresh_task:
            self._refresh_task.cancel()

        # Disconnect all accounts
        disconnect_tasks = [acc.disconnect() for acc in self.accounts.values()]
        await asyncio.gather(*disconnect_tasks, return_exceptions=True)

        self.accounts.clear()
        logger.info("AccountManager shutdown complete")

    async def add_account(self, phone: str, api_id: int, api_hash: str,
                          target_group_id: int, mode: str = "both",
                          session_string: Optional[str] = None) -> Tuple[bool, str]:
        """Add a new account"""
        # Check if already exists
        if phone in self.accounts:
            return False, f"الحساب {phone} موجود بالفعل"

        # Create engine
        engine = AccountEngine(
            phone=phone, api_id=api_id, api_hash=api_hash,
            target_group_id=target_group_id, mode=mode,
            session_string=session_string,
        )

        # Set shared caches
        engine.forward_cache = self.forward_cache
        engine.reply_cache = self.reply_cache
        engine.filters = self.filters
        engine.keywords = self.keywords
        engine.excluded_groups = self.excluded_groups
        engine.blocked_users = self.blocked_users
        engine.direct_triggers = self.direct_triggers
        engine.blocked_phrases = self.blocked_phrases
        engine.auto_replies = self.auto_replies

        # Connect
        success = await engine.connect()
        if not success:
            return False, f"فشل الاتصال: {engine.last_error}"

        self.accounts[phone] = engine

        # Update DB status
        repo = AccountRepository(self.db)
        await repo.update_status(phone, AccountStatus.ACTIVE)

        # Save session
        new_session = await engine.get_session_string()
        if new_session:
            await repo.update_session(phone, new_session)

        # Trigger callback
        if self.on_status_change:
            asyncio.create_task(self.on_status_change(phone, AccountStatus.ACTIVE.value))

        return True, f"✅ الحساب {phone} متصل بنجاح"

    async def remove_account(self, phone: str) -> Tuple[bool, str]:
        """Remove an account"""
        engine = self.accounts.get(phone)
        if engine:
            await engine.disconnect()
            del self.accounts[phone]

        # Delete from DB
        repo = AccountRepository(self.db)
        await repo.delete(phone)

        return True, f"🗑️ الحساب {phone} تم حذفه"

    async def start_account(self, phone: str) -> Tuple[bool, str]:
        """Start a stopped account"""
        if phone in self.accounts:
            return False, f"الحساب {phone} يعمل بالفعل"

        # Load from DB
        repo = AccountRepository(self.db)
        acc = await repo.get_by_phone(phone)
        if not acc:
            return False, f"الحساب {phone} غير موجود في قاعدة البيانات"

        if not acc.session_string:
            return False, f"لا يوجد session_string للحساب {phone}"

        return await self.add_account(
            phone=acc.phone,
            api_id=acc.api_id,
            api_hash=acc.api_hash,
            target_group_id=acc.target_group_id,
            mode=acc.mode,
            session_string=acc.session_string,
        )

    async def stop_account(self, phone: str) -> Tuple[bool, str]:
        """Stop an active account"""
        engine = self.accounts.get(phone)
        if not engine:
            return False, f"الحساب {phone} غير نشط"

        await engine.disconnect()
        del self.accounts[phone]

        # Update DB
        repo = AccountRepository(self.db)
        await repo.update_status(phone, AccountStatus.PAUSED)

        if self.on_status_change:
            asyncio.create_task(self.on_status_change(phone, AccountStatus.PAUSED.value))

        return True, f"⏸️ الحساب {phone} متوقف"

    async def restart_account(self, phone: str) -> Tuple[bool, str]:
        """Restart an account"""
        if phone in self.accounts:
            await self.stop_account(phone)
            await asyncio.sleep(2)
        return await self.start_account(phone)

    async def restart_all(self) -> List[Tuple[str, bool, str]]:
        """Restart all accounts"""
        results = []
        for phone in list(self.accounts.keys()):
            success, msg = await self.restart_account(phone)
            results.append((phone, success, msg))
            await asyncio.sleep(1)
        return results

    async def get_account_status(self, phone: str) -> Optional[dict]:
        """Get account status"""
        engine = self.accounts.get(phone)
        if engine:
            return engine.to_dict()

        repo = AccountRepository(self.db)
        acc = await repo.get_by_phone(phone)
        return acc.to_dict() if acc else None

    async def get_all_status(self) -> List[dict]:
        """Get status of all accounts"""
        # Active engines
        active = [eng.to_dict() for eng in self.accounts.values()]

        # DB accounts not active
        repo = AccountRepository(self.db)
        all_accs = await repo.get_all()
        active_phones = {a["phone"] for a in active}

        for acc in all_accs:
            if acc.phone not in active_phones:
                active.append(acc.to_dict())

        return active

    async def join_groups(self, phone: str, group_links: List[str],
                          delay_base: int = 30, delay_random: int = 30,
                          progress_callback=None) -> List[Tuple[str, bool, str]]:
        """Join groups with an account"""
        engine = self.accounts.get(phone)
        if not engine:
            return [(phone, False, "الحساب غير نشط")]

        results = []
        for i, link in enumerate(group_links):
            # Check circuit breaker
            if await self.circuit_breaker.is_open(phone):
                results.append((link, False, "Circuit breaker مفتوح"))
                continue

            # Join
            success, msg = await engine.join_group(link)
            results.append((link, success, msg))

            if progress_callback:
                await progress_callback(phone, i + 1, len(group_links), link, success)

            # Delay between joins
            if i < len(group_links) - 1:
                delay = delay_base + random.randint(0, delay_random)
                await asyncio.sleep(delay)

        return results

    async def update_account_mode(self, phone: str, mode: str) -> Tuple[bool, str]:
        """Update account mode"""
        if mode not in ("forward", "reply", "both", "self"):
            return False, "الوضع يجب أن يكون: forward, reply, both, self"

        # Update engine
        engine = self.accounts.get(phone)
        if engine:
            engine.mode = mode

        # Update DB
        repo = AccountRepository(self.db)
        await repo.update_info(phone, mode=mode)

        return True, f"✅ وضع الحساب {phone} تغير إلى {mode}"

    async def update_target_group(self, phone: str, group_id: int) -> Tuple[bool, str]:
        """Update target group"""
        engine = self.accounts.get(phone)
        if engine:
            engine.target_group_id = group_id

        repo = AccountRepository(self.db)
        await repo.update_info(phone, target_group_id=group_id)

        return True, f"✅ مجموعة الهدف لـ {phone} تحدثت إلى {group_id}"

    async def _refresh_config(self) -> None:
        """Refresh configuration from database"""
        try:
            # Load settings
            settings_repo = BotSettingRepository(self.db)

            self.filters = {
                "filter_mention": await settings_repo.get_bool("filter_mention", True),
                "filter_links": await settings_repo.get_bool("filter_links", True),
                "filter_digits": await settings_repo.get_bool("filter_digits", True),
                "filter_private": await settings_repo.get_bool("filter_private", True),
                "filter_outgoing": await settings_repo.get_bool("filter_outgoing", True),
                "filter_bots": await settings_repo.get_bool("filter_bots", True),
                "filter_admins": await settings_repo.get_bool("filter_admins", True),
                "word_count_limit": await settings_repo.get_int("word_count_limit", 17),
            }

            # Load keywords
            kw_repo = KeywordRepository(self.db)
            keywords = await kw_repo.get_all(active_only=True)
            self.keywords = [k.word for k in keywords]

            # Load excluded groups
            eg_repo = ExcludedGroupRepository(self.db)
            self.excluded_groups = set(await eg_repo.get_all_ids())

            # Load blocked users
            bu_repo = BlockedUserRepository(self.db)
            blocked = await bu_repo.get_all(limit=10000)
            self.blocked_users = {
                b.user_id: (b.username or "", b.display_name or "")
                for b in blocked
            }

            # Load trigger phrases
            tp_repo = TriggerPhraseRepository(self.db)
            self.direct_triggers = [
                p.phrase for p in await tp_repo.get_all(phrase_type="direct", active_only=True)
            ]
            self.blocked_phrases = [
                p.phrase for p in await tp_repo.get_all(phrase_type="blocked", active_only=True)
            ]
            self.auto_replies = [
                p.phrase for p in await tp_repo.get_all(phrase_type="auto", active_only=True)
            ]

            # Update all engines
            for engine in self.accounts.values():
                engine.filters = self.filters
                engine.keywords = self.keywords
                engine.excluded_groups = self.excluded_groups
                engine.blocked_users = self.blocked_users
                engine.direct_triggers = self.direct_triggers
                engine.blocked_phrases = self.blocked_phrases
                engine.auto_replies = self.auto_replies

            logger.debug("Configuration refreshed")

        except Exception as e:
            logger.error(f"Config refresh error: {e}")

    async def _periodic_refresh(self) -> None:
        """Periodically refresh configuration"""
        while self._running:
            try:
                await asyncio.sleep(60)  # Refresh every 60 seconds
                if self._running:
                    await self._refresh_config()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Periodic refresh error: {e}")

    async def broadcast_message(self, message: str) -> Dict[str, Tuple[bool, str]]:
        """Send a message from all accounts"""
        results = {}
        for phone, engine in self.accounts.items():
            try:
                from engine.utils import safe_send
                # Send to target group
                await safe_send(engine.client, engine.target_group_id, message)
                results[phone] = (True, "تم الإرسال")
            except Exception as e:
                results[phone] = (False, str(e))
        return results

    def get_stats(self) -> dict:
        """Get manager statistics"""
        return {
            "total_accounts": len(self.accounts),
            "connected": sum(1 for a in self.accounts.values() if a.is_connected),
            "forward_cache_size": self.forward_cache._data.__len__(),
            "reply_cache_size": self.reply_cache._data.__len__(),
            "keywords_loaded": len(self.keywords),
            "excluded_groups": len(self.excluded_groups),
            "blocked_users": len(self.blocked_users),
        }
