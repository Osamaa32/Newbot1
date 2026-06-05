"""
═══════════════════════════════════════════════════════════════════════════════
Database Layer — Async PostgreSQL with Connection Pooling & Migrations
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncpg

from models import AccountInfo, AccountStatus

logger = logging.getLogger("telegram-bot")


class Database:
    """Async PostgreSQL database with automatic migrations and connection pooling."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        """Initialize connection pool and run migrations."""
        if not self.database_url:
            logger.error("DATABASE_URL not set!")
            sys.exit(1)

        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=5,
            max_size=30,
            command_timeout=60,
            server_settings={"jit": "off"},
        )
        logger.info("Database pool created (min=5, max=30)")
        await self._run_migrations()
        await self._seed_defaults()
        logger.info("Database initialized successfully")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            logger.info("Database pool closed")

    async def health_check(self) -> bool:
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ─── Migrations ───

    async def _run_migrations(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            current = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM schema_migrations") or 0

            migrations = self._get_migrations()
            for version, sql in sorted(migrations.items()):
                if version > current:
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations(version) VALUES($1)", version)
                    logger.info(f"Migration {version} applied")

    def _get_migrations(self) -> Dict[int, str]:
        return {
            1: "CREATE TABLE IF NOT EXISTS direct_reply_messages (id SERIAL PRIMARY KEY, message_text VARCHAR(255) NOT NULL)",
            2: "CREATE TABLE IF NOT EXISTS blocked_reply_messages (id SERIAL PRIMARY KEY, message_text VARCHAR(255) NOT NULL)",
            3: "CREATE TABLE IF NOT EXISTS auto_reply_responses (id SERIAL PRIMARY KEY, message_text VARCHAR(255) NOT NULL)",
            4: "CREATE TABLE IF NOT EXISTS join_groups (id SERIAL PRIMARY KEY, group_link VARCHAR(255) NOT NULL UNIQUE)",
            5: """CREATE TABLE IF NOT EXISTS blocked_users (
                user_id BIGINT PRIMARY KEY, username VARCHAR(64) NULL,
                display_name VARCHAR(255) NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            6: """CREATE TABLE IF NOT EXISTS auto_reply_log (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
                dedupe_key VARCHAR(64) NOT NULL DEFAULT '',
                username VARCHAR(64) NULL, display_name VARCHAR(255) NULL,
                bot_phone VARCHAR(32) NULL, message_id BIGINT NULL,
                src_chat_id BIGINT NULL, src_msg_id BIGINT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            7: "CREATE INDEX IF NOT EXISTS idx_auto_reply_user ON auto_reply_log(user_id)",
            8: "CREATE INDEX IF NOT EXISTS idx_auto_reply_created ON auto_reply_log(created_at)",
            9: "CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_user_dedupe ON auto_reply_log (user_id, dedupe_key)",
            10: """CREATE TABLE IF NOT EXISTS task_queue (
                id SERIAL PRIMARY KEY, task_type VARCHAR(50) NOT NULL,
                payload JSONB NOT NULL, status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, processed_at TIMESTAMP NULL)""",
            11: """CREATE TABLE IF NOT EXISTS bot_accounts (
                id SERIAL PRIMARY KEY, phone VARCHAR(32) NOT NULL UNIQUE,
                api_id INT NOT NULL, api_hash VARCHAR(64) NOT NULL,
                target_group_id BIGINT NOT NULL, mode VARCHAR(20) DEFAULT 'both',
                status VARCHAR(20) DEFAULT 'pending', session_string TEXT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_connected TIMESTAMP NULL, last_error TEXT NULL)""",
            12: """CREATE TABLE IF NOT EXISTS bot_settings (
                key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL,
                description TEXT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by BIGINT NULL)""",
            13: """CREATE TABLE IF NOT EXISTS excluded_groups (
                group_id BIGINT PRIMARY KEY, reason TEXT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            14: """CREATE TABLE IF NOT EXISTS keywords (
                word VARCHAR(255) PRIMARY KEY, category VARCHAR(50) DEFAULT 'general',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            15: """CREATE TABLE IF NOT EXISTS ignore_filters (
                filter_name VARCHAR(50) PRIMARY KEY, is_active BOOLEAN DEFAULT true,
                threshold INT NULL)""",
            16: """CREATE TABLE IF NOT EXISTS bot_sessions (
                phone VARCHAR(32) PRIMARY KEY, session_string TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
            17: """CREATE TABLE IF NOT EXISTS auth_state (
                id INT PRIMARY KEY DEFAULT 1, is_authenticated BOOLEAN DEFAULT false,
                password_hash VARCHAR(128) NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        }

    async def _seed_defaults(self) -> None:
        async with self.pool.acquire() as conn:
            defaults = [
                ("fallback_group_id", "-1002353780992", "Fallback group for failed forwards"),
                ("command_group_id", "-1002311800895", "Group where commands are accepted"),
                ("rate_limit_max", "4", "Max auto-replies per user per window"),
                ("rate_limit_window", "3600", "Rate limit window in seconds"),
                ("cb_failure_threshold", "5", "Failures before circuit breaker opens"),
                ("cb_recovery_timeout", "1800", "Seconds before circuit breaker resets"),
                ("max_concurrent_dispatch", "50", "Max concurrent message processing"),
                ("ttl_cache_seconds", "7200", "TTL for forward/reply dedupe cache"),
                ("fuzzy_threshold", "80", "Fuzzy match threshold for find commands"),
                ("fuzzy_exact_threshold", "100", "Exact match threshold for blkfind"),
                ("join_delay_base", "30", "Base delay between joins in seconds"),
                ("join_delay_random", "30", "Random additional delay for joins"),
                ("max_message_length", "4000", "Max message length before splitting"),
                ("default_auto_reply", "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك", "Default auto-reply message"),
                ("word_count_limit", "17", "Max words before ignoring message"),
                ("filter_mention", "true", "Ignore messages with @mentions"),
                ("filter_links", "true", "Ignore messages with URLs"),
                ("filter_digits", "true", "Ignore messages with digits"),
                ("filter_private", "true", "Ignore private chats"),
                ("filter_outgoing", "true", "Ignore outgoing messages"),
                ("filter_bots", "true", "Ignore messages from bots"),
                ("filter_admins", "true", "Ignore messages from admins/creators"),
                ("command_prefix", "/", "Prefix for bot commands"),
                ("owner_id", "", "Telegram user ID of bot owner"),
                ("bot_token", "", "BotFather token for controller bot"),
            ]
            for key, value, desc in defaults:
                await conn.execute(
                    """INSERT INTO bot_settings (key, value, description)
                       VALUES ($1, $2, $3) ON CONFLICT (key) DO NOTHING""",
                    key, value, desc,
                )

            default_keywords = [
                "ابي مساعده", "يسوي", "يحل", "خصوصي", "شاطر", "تحل", "تسوي",
                "يعرف", "تعرف", "واجب", "بروجكت", "فاهم", "سكليف", "بحث",
                "مشروع", "يساعد", "اسايمنت", "ابغى مساعده", "ابغا مساعده",
                "محتاج مساعده", "حد يساعدني", "احد يساعدني",
                "ابي حد يحضر عني", "ابغا حد يحضر عني", "يحضر عني", "يحظر", "يحضر",
                "عندي اختبار", "احد عنده خصوصي", "احد يعرف مختص",
                "س ك ل ي ف", "case study", "كيس ستدي",
                "بوربوينت", "بووربوينت", "عذر طبي", "اجازة مرضية",
            ]
            for kw in default_keywords:
                await conn.execute(
                    "INSERT INTO keywords (word) VALUES ($1) ON CONFLICT DO NOTHING", kw
                )

            default_filters = [
                ("mention", True, None), ("links", True, None), ("digits", True, None),
                ("private", True, None), ("outgoing", True, None), ("bots", True, None),
                ("admins", True, None), ("word_count", True, 17),
            ]
            for name, active, thresh in default_filters:
                await conn.execute(
                    """INSERT INTO ignore_filters (filter_name, is_active, threshold)
                       VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                    name, active, thresh,
                )

    # ─── Settings ───

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM bot_settings WHERE key=$1", key)
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str, updated_by: Optional[int] = None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO bot_settings (key, value, updated_at, updated_by)
                   VALUES ($1, $2, NOW(), $3)
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = NOW(), updated_by = EXCLUDED.updated_by""",
                key, value, updated_by,
            )

    async def get_all_settings(self) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT key, value, description, updated_at FROM bot_settings ORDER BY key"
            )

    # ─── Auth State ───

    async def is_authenticated(self) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT is_authenticated, password_hash FROM auth_state WHERE id=1")
            if not row:
                return False
            return row["is_authenticated"] and row["password_hash"] is not None

    async def get_password_hash(self) -> Optional[str]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT password_hash FROM auth_state WHERE id=1")
            return row["password_hash"] if row else None

    async def set_password(self, password_hash: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO auth_state (id, is_authenticated, password_hash)
                   VALUES (1, false, $1)
                   ON CONFLICT (id) DO UPDATE SET password_hash = EXCLUDED.password_hash""",
                password_hash,
            )

    async def set_authenticated(self, authenticated: bool = True) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO auth_state (id, is_authenticated)
                   VALUES (1, $1)
                   ON CONFLICT (id) DO UPDATE SET is_authenticated = EXCLUDED.is_authenticated""",
                authenticated,
            )

    # ─── Accounts ───

    async def add_account(self, phone: str, api_id: int, api_hash: str, target_group_id: int, mode: str = "both") -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO bot_accounts (phone, api_id, api_hash, target_group_id, mode, status)
                   VALUES ($1, $2, $3, $4, $5, 'pending')
                   ON CONFLICT (phone) DO UPDATE
                   SET api_id=EXCLUDED.api_id, api_hash=EXCLUDED.api_hash,
                       target_group_id=EXCLUDED.target_group_id, mode=EXCLUDED.mode, status='pending'
                   RETURNING id""",
                phone, api_id, api_hash, target_group_id, mode,
            )
            return row["id"] if row else 0

    async def get_account(self, phone: str) -> Optional[AccountInfo]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM bot_accounts WHERE phone=$1", phone)
            if not row:
                return None
            return AccountInfo(
                id=row["id"], phone=row["phone"], api_id=row["api_id"],
                api_hash=row["api_hash"], target_group_id=row["target_group_id"],
                mode=row["mode"], status=AccountStatus(row["status"]),
                session_string=row["session_string"], created_at=row["created_at"],
                last_error=row["last_error"], last_connected=row["last_connected"],
            )

    async def get_all_accounts(self) -> List[AccountInfo]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bot_accounts ORDER BY id")
            return [
                AccountInfo(
                    id=r["id"], phone=r["phone"], api_id=r["api_id"],
                    api_hash=r["api_hash"], target_group_id=r["target_group_id"],
                    mode=r["mode"], status=AccountStatus(r["status"]),
                    session_string=r["session_string"], created_at=r["created_at"],
                    last_error=r["last_error"], last_connected=r["last_connected"],
                )
                for r in rows
            ]

    async def update_account_status(self, phone: str, status: AccountStatus, error: Optional[str] = None) -> None:
        async with self.pool.acquire() as conn:
            if error:
                await conn.execute(
                    "UPDATE bot_accounts SET status=$1, last_error=$2, last_connected=NOW() WHERE phone=$3",
                    status.value, error, phone,
                )
            elif status == AccountStatus.ACTIVE:
                await conn.execute(
                    "UPDATE bot_accounts SET status=$1, last_connected=NOW(), last_error=NULL WHERE phone=$2",
                    status.value, phone,
                )
            else:
                await conn.execute(
                    "UPDATE bot_accounts SET status=$1 WHERE phone=$2",
                    status.value, phone,
                )

    async def update_account_session(self, phone: str, session_string: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE bot_accounts SET session_string=$1 WHERE phone=$2",
                session_string, phone,
            )
            await conn.execute(
                """INSERT INTO bot_sessions (phone, session_string, updated_at)
                   VALUES ($1, $2, NOW())
                   ON CONFLICT (phone) DO UPDATE
                   SET session_string=EXCLUDED.session_string, updated_at=NOW()""",
                phone, session_string,
            )

    async def update_account_group(self, phone: str, group_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE bot_accounts SET target_group_id=$1 WHERE phone=$2",
                group_id, phone,
            )

    async def update_account_mode(self, phone: str, mode: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE bot_accounts SET mode=$1 WHERE phone=$2",
                mode, phone,
            )

    async def delete_account(self, phone: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM bot_accounts WHERE phone=$1", phone)
            await conn.execute("DELETE FROM bot_sessions WHERE phone=$1", phone)

    async def load_session(self, phone: str) -> Optional[str]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT session_string FROM bot_sessions WHERE phone=$1", phone)
            return row["session_string"] if row else None

    # ─── Text Stores ───

    async def load_table(self, name: str) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT message_text FROM {name}")
            return [r["message_text"] for r in rows]

    async def insert_table(self, name: str, text: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(f"INSERT INTO {name}(message_text) VALUES($1)", text)

    async def delete_table(self, name: str, text: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {name} WHERE message_text=$1", text)

    # ─── Groups ───

    async def add_group(self, link: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO join_groups (group_link) VALUES ($1) ON CONFLICT DO NOTHING", link
            )

    async def delete_group(self, link: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM join_groups WHERE group_link=$1", link)

    async def update_group(self, old_link: str, new_link: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE join_groups SET group_link=$1 WHERE group_link=$2", new_link, old_link
            )

    async def get_all_groups(self) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT group_link FROM join_groups")
            return [r["group_link"] for r in rows]

    # ─── Keywords ───

    async def add_keyword(self, word: str, category: str = "general") -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO keywords (word, category) VALUES ($1, $2)
                   ON CONFLICT (word) DO UPDATE SET category = EXCLUDED.category""",
                word, category,
            )

    async def remove_keyword(self, word: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM keywords WHERE word=$1", word)

    async def get_keywords(self) -> List[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT word FROM keywords ORDER BY added_at DESC")
            return [r["word"] for r in rows]

    async def list_keywords(self) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM keywords ORDER BY added_at DESC")

    # ─── Filters ───

    async def get_filters(self) -> Dict[str, Tuple[bool, Optional[int]]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM ignore_filters")
            return {r["filter_name"]: (r["is_active"], r["threshold"]) for r in rows}

    async def toggle_filter(self, filter_name: str, is_active: bool) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ignore_filters (filter_name, is_active) VALUES ($1, $2)
                   ON CONFLICT (filter_name) DO UPDATE SET is_active = EXCLUDED.is_active""",
                filter_name, is_active,
            )

    async def update_filter_threshold(self, filter_name: str, threshold: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE ignore_filters SET threshold=$1 WHERE filter_name=$2",
                threshold, filter_name,
            )

    # ─── Excluded Groups ───

    async def add_excluded_group(self, group_id: int, reason: Optional[str] = None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO excluded_groups (group_id, reason) VALUES ($1, $2)
                   ON CONFLICT (group_id) DO UPDATE SET reason = EXCLUDED.reason""",
                group_id, reason,
            )

    async def remove_excluded_group(self, group_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM excluded_groups WHERE group_id=$1", group_id)

    async def get_excluded_groups(self) -> Set[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT group_id FROM excluded_groups")
            return {r["group_id"] for r in rows}

    async def list_excluded_groups(self) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM excluded_groups ORDER BY added_at DESC")

    # ─── Blocked Users ───

    async def blocked_users_map(self) -> Dict[int, Tuple[str, str]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, COALESCE(username,'') AS username, COALESCE(display_name,'') AS display_name FROM blocked_users"
            )
            return {int(r["user_id"]): (r["username"], r["display_name"]) for r in rows}

    async def add_blocked_user(self, user_id: int, username: Optional[str], display_name: Optional[str]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO blocked_users (user_id, username, display_name, created_at)
                   VALUES ($1, $2, $3, NOW())
                   ON CONFLICT (user_id) DO UPDATE
                   SET username=EXCLUDED.username, display_name=EXCLUDED.display_name""",
                user_id, username or "", display_name or "",
            )

    async def del_blocked_user(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM blocked_users WHERE user_id=$1", user_id)
            try:
                return int(result.split()[-1])
            except Exception:
                return 0

    async def list_blocked_users(self, limit: int = 200) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM blocked_users ORDER BY created_at DESC LIMIT $1", limit
            )

    async def find_blocked_users(self, pattern: str) -> List[asyncpg.Record]:
        like = f"%{pattern}%"
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """SELECT * FROM blocked_users
                   WHERE CAST(user_id AS TEXT) LIKE $1 OR username LIKE $2 OR display_name LIKE $3
                   ORDER BY created_at DESC""",
                like, like, like,
            )

    # ─── Auto Reply Log ───

    async def count_auto_replies(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS c FROM auto_reply_log WHERE user_id=$1", user_id)
            return int(row["c"] if row else 0)

    async def count_auto_replies_distinct(self, user_id: int, hours: int = 24) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT COUNT(DISTINCT dedupe_key) AS c FROM auto_reply_log
                   WHERE user_id=$1 AND created_at >= NOW() - INTERVAL '$2 hours'""",
                user_id, hours,
            )
            return int(row["c"] if row else 0)

    async def log_auto_reply_pending(self, user_id: int, username: Optional[str],
                                     display_name: Optional[str], dedupe_key: str,
                                     src_chat_id: Optional[int], src_msg_id: Optional[int]) -> int:
        async with self.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """INSERT INTO auto_reply_log (user_id, dedupe_key, username, display_name,
                                                    bot_phone, message_id, src_chat_id, src_msg_id)
                       VALUES ($1, $2, $3, $4, NULL, NULL, $5, $6)
                       ON CONFLICT (user_id, dedupe_key) DO NOTHING RETURNING id""",
                    user_id, dedupe_key, username or "", display_name or "", src_chat_id, src_msg_id,
                )
                return int(row["id"]) if row else 0
            except Exception:
                return 0

    async def update_auto_reply_log(self, log_id: int, bot_phone: Optional[str] = None,
                                    message_id: Optional[int] = None) -> None:
        if not log_id:
            return
        sets, vals = [], []
        if bot_phone is not None:
            sets.append(f"bot_phone=${len(vals)+1}")
            vals.append(bot_phone)
        if message_id is not None:
            sets.append(f"message_id=${len(vals)+1}")
            vals.append(message_id)
        if not sets:
            return
        vals.append(log_id)
        q = f"UPDATE auto_reply_log SET {', '.join(sets)} WHERE id=${len(vals)}"
        async with self.pool.acquire() as conn:
            await conn.execute(q, *vals)

    async def list_auto_replies(self, limit: int = 50) -> List[asyncpg.Record]:
        limit = max(1, min(limit, 500))
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM auto_reply_log ORDER BY id DESC LIMIT $1", limit
            )

    async def list_auto_replies_for_user(self, user_id: int, limit: int = 50) -> List[asyncpg.Record]:
        limit = max(1, min(limit, 500))
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM auto_reply_log WHERE user_id=$1 ORDER BY id DESC LIMIT $2",
                user_id, limit,
            )

    async def clear_auto_replies(self, user_id: Optional[int] = None) -> int:
        async with self.pool.acquire() as conn:
            if user_id:
                result = await conn.execute("DELETE FROM auto_reply_log WHERE user_id=$1", user_id)
            else:
                result = await conn.execute("TRUNCATE TABLE auto_reply_log")
            try:
                return int(result.split()[-1])
            except Exception:
                return 0

    # ─── Stats ───

    async def get_stats(self) -> Dict[str, int]:
        async with self.pool.acquire() as conn:
            return {
                "direct": await conn.fetchval("SELECT COUNT(*) FROM direct_reply_messages") or 0,
                "blocked_text": await conn.fetchval("SELECT COUNT(*) FROM blocked_reply_messages") or 0,
                "blocked_users": await conn.fetchval("SELECT COUNT(*) FROM blocked_users") or 0,
                "groups": await conn.fetchval("SELECT COUNT(*) FROM join_groups") or 0,
                "accounts": await conn.fetchval("SELECT COUNT(*) FROM bot_accounts") or 0,
                "active_accounts": await conn.fetchval("SELECT COUNT(*) FROM bot_accounts WHERE status='active'") or 0,
                "pending_tasks": await conn.fetchval("SELECT COUNT(*) FROM task_queue WHERE status='pending'") or 0,
                "keywords": await conn.fetchval("SELECT COUNT(*) FROM keywords") or 0,
                "excluded_groups": await conn.fetchval("SELECT COUNT(*) FROM excluded_groups") or 0,
            }

    # ─── Task Queue ───

    async def enqueue_task(self, task_type: str, payload: dict) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO task_queue(task_type, payload) VALUES($1, $2) RETURNING id",
                task_type, json.dumps(payload),
            )
            return row["id"] if row else 0

    async def dequeue_task(self) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM task_queue WHERE status='pending' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
                if row:
                    await conn.execute(
                        "UPDATE task_queue SET status='processing', processed_at=NOW() WHERE id=$1",
                        row["id"],
                    )
                return row

    async def complete_task(self, task_id: int, status: str = "completed") -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE task_queue SET status=$1 WHERE id=$2", status, task_id)

    # ─── Backup ───

    async def get_all_for_backup(self) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            return {
                "direct_reply_messages": [r["message_text"] for r in await conn.fetch("SELECT message_text FROM direct_reply_messages")],
                "blocked_reply_messages": [r["message_text"] for r in await conn.fetch("SELECT message_text FROM blocked_reply_messages")],
                "auto_reply_responses": [r["message_text"] for r in await conn.fetch("SELECT message_text FROM auto_reply_responses")],
                "join_groups": [r["group_link"] for r in await conn.fetch("SELECT group_link FROM join_groups")],
                "blocked_users": [dict(r) for r in await conn.fetch("SELECT * FROM blocked_users")],
                "bot_accounts": [dict(r) for r in await conn.fetch("SELECT * FROM bot_accounts")],
                "bot_settings": [dict(r) for r in await conn.fetch("SELECT key, value, description FROM bot_settings")],
                "excluded_groups": [dict(r) for r in await conn.fetch("SELECT * FROM excluded_groups")],
                "keywords": [dict(r) for r in await conn.fetch("SELECT * FROM keywords")],
                "ignore_filters": [dict(r) for r in await conn.fetch("SELECT * FROM ignore_filters")],
            }

    async def restore_from_backup(self, data: Dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for table, rows in data.items():
                    if not rows:
                        continue
                    if table == "direct_reply_messages":
                        for val in rows:
                            await conn.execute("INSERT INTO direct_reply_messages(message_text) VALUES($1) ON CONFLICT DO NOTHING", val)
                    elif table == "blocked_reply_messages":
                        for val in rows:
                            await conn.execute("INSERT INTO blocked_reply_messages(message_text) VALUES($1) ON CONFLICT DO NOTHING", val)
                    elif table == "auto_reply_responses":
                        for val in rows:
                            await conn.execute("INSERT INTO auto_reply_responses(message_text) VALUES($1) ON CONFLICT DO NOTHING", val)
                    elif table == "join_groups":
                        for val in rows:
                            await conn.execute("INSERT INTO join_groups(group_link) VALUES($1) ON CONFLICT DO NOTHING", val)
                    elif table == "blocked_users":
                        for r in rows:
                            await conn.execute(
                                """INSERT INTO blocked_users (user_id, username, display_name, created_at)
                                   VALUES ($1, $2, $3, COALESCE($4, NOW()))
                                   ON CONFLICT (user_id) DO UPDATE
                                   SET username=EXCLUDED.username, display_name=EXCLUDED.display_name""",
                                r.get("user_id"), r.get("username", ""), r.get("display_name", ""), r.get("created_at"),
                            )
                    elif table == "bot_settings":
                        for r in rows:
                            await conn.execute(
                                """INSERT INTO bot_settings (key, value, description) VALUES ($1, $2, $3)
                                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
                                r.get("key"), r.get("value"), r.get("description", ""),
                            )
                    elif table == "excluded_groups":
                        for r in rows:
                            await conn.execute(
                                "INSERT INTO excluded_groups (group_id, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                r.get("group_id"), r.get("reason"),
                            )
                    elif table == "keywords":
                        for r in rows:
                            await conn.execute(
                                "INSERT INTO keywords (word, category) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                r.get("word"), r.get("category", "general"),
                            )
                    elif table == "ignore_filters":
                        for r in rows:
                            await conn.execute(
                                """INSERT INTO ignore_filters (filter_name, is_active, threshold) VALUES ($1, $2, $3)
                                   ON CONFLICT (filter_name) DO UPDATE
                                   SET is_active=EXCLUDED.is_active, threshold=EXCLUDED.threshold""",
                                r.get("filter_name"), r.get("is_active", True), r.get("threshold"),
                            )
