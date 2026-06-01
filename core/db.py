import os
import datetime
from pathlib import Path
from typing import Optional, Any

import asyncpg
import orjson
import gzip

from core.config import AppConfig


class PostgresDB:
    """ Ultra-fast PostgreSQL layer with connection pooling. """

    VALID_TABLES = frozenset({
        "direct_reply_messages",
        "blocked_reply_messages",
        "auto_reply_responses",
        "join_groups",
        "blocked_users",
        "auto_reply_log",
        "accounts",
    })

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        db_url = os.getenv("DATABASE_URL") or os.getenv("JAWSDB_URL", "")
        if not db_url:
            raise RuntimeError("DATABASE_URL environment variable is required")

        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)

        self.pool = await asyncpg.create_pool(
            dsn=db_url,
            min_size=10,
            max_size=50,
            command_timeout=10,
            server_settings={"jit": "off"}
        )

        async with self.pool.acquire() as conn:
            await self._create_tables(conn)
            await self._create_indexes(conn)

        self.cfg.logger.info("PostgreSQL pool initialized")

    async def _create_tables(self, conn: asyncpg.Connection):
        # Core tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS direct_reply_messages (
                id SERIAL PRIMARY KEY,
                message_text VARCHAR(500) NOT NULL UNIQUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_reply_messages (
                id SERIAL PRIMARY KEY,
                message_text VARCHAR(500) NOT NULL UNIQUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_reply_responses (
                id SERIAL PRIMARY KEY,
                message_text VARCHAR(500) NOT NULL UNIQUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS join_groups (
                id SERIAL PRIMARY KEY,
                group_link VARCHAR(255) NOT NULL UNIQUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(64),
                display_name VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_reply_log (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                dedupe_key VARCHAR(32) NOT NULL DEFAULT '',
                username VARCHAR(64),
                display_name VARCHAR(255),
                bot_phone VARCHAR(32),
                message_id BIGINT,
                src_chat_id BIGINT,
                src_msg_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Accounts table — stores Telegram session strings
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                phone VARCHAR(32) UNIQUE NOT NULL,
                api_id BIGINT NOT NULL,
                api_hash VARCHAR(64) NOT NULL,
                target_group_id BIGINT NOT NULL,
                mode VARCHAR(10) DEFAULT 'both',
                session_string TEXT,
                is_active BOOLEAN DEFAULT true,
                is_connected BOOLEAN DEFAULT false,
                display_name VARCHAR(255),
                telegram_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

    async def _create_indexes(self, conn: asyncpg.Connection):
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_auto_reply_user_time 
            ON auto_reply_log(user_id, created_at)
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_dedupe 
            ON auto_reply_log(user_id, dedupe_key)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_accounts_active 
            ON accounts(is_active)
        """)

    # ===== Validation =====

    def _validate_table(self, table: str):
        if table not in self.VALID_TABLES:
            raise ValueError(f"Invalid table: {table}")

    # ===== Table Operations =====

    async def load_table(self, table: str) -> list[str]:
        self._validate_table(table)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f'SELECT message_text FROM {table}')
            return [r['message_text'] for r in rows]

    async def insert_table(self, table: str, text: str) -> None:
        self._validate_table(table)
        async with self.pool.acquire() as conn:
            await conn.execute(
                f'INSERT INTO {table}(message_text) VALUES($1) ON CONFLICT DO NOTHING',
                text
            )

    async def delete_table(self, table: str, text: str) -> None:
        self._validate_table(table)
        async with self.pool.acquire() as conn:
            await conn.execute(f'DELETE FROM {table} WHERE message_text=$1', text)

    # ===== Blocked Users =====

    async def blocked_users_map(self) -> dict[int, tuple[str, str]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, COALESCE(username,'') as username, "
                "COALESCE(display_name,'') as display_name FROM blocked_users"
            )
            return {
                int(r['user_id']): (r['username'], r['display_name'])
                for r in rows
            }

    async def add_blocked_user(self, user_id: int, username: Optional[str],
                                display_name: Optional[str]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO blocked_users (user_id, username, display_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    display_name = EXCLUDED.display_name
            """, user_id, username or "", display_name or "")

    async def del_blocked_user(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.execute(
                'DELETE FROM blocked_users WHERE user_id=$1', user_id
            )

    async def list_blocked_users(self, limit: int = 200) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT user_id, username, display_name, created_at '
                'FROM blocked_users ORDER BY created_at DESC LIMIT $1',
                limit
            )
            return [dict(r) for r in rows]

    async def find_blocked_users(self, pattern: str, limit: int = 200) -> list[dict]:
        like = f"%{pattern}%"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, username, display_name, created_at
                FROM blocked_users
                WHERE CAST(user_id AS TEXT) LIKE $1 
                   OR username LIKE $1 
                   OR display_name LIKE $1
                ORDER BY created_at DESC LIMIT $2
            """, like, limit)
            return [dict(r) for r in rows]

    # ===== Auto Reply Log =====

    async def count_auto_replies(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT COUNT(*) FROM auto_reply_log WHERE user_id=$1', user_id
            ) or 0

    async def count_auto_replies_distinct(self, user_id: int, hours: int = 24) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT COUNT(DISTINCT dedupe_key)
                FROM auto_reply_log
                WHERE user_id=$1 AND created_at >= NOW() - $2 * INTERVAL '1 hour'
            """, user_id, hours) or 0

    async def log_auto_reply(self, user_id: int, username: str, display_name: str,
                              dedupe_key: str, src_chat_id: Optional[int],
                              src_msg_id: Optional[int]) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO auto_reply_log 
                    (user_id, dedupe_key, username, display_name, src_chat_id, src_msg_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id, dedupe_key) DO NOTHING
                RETURNING id
            """, user_id, dedupe_key, username, display_name, src_chat_id, src_msg_id) or 0

    async def update_auto_reply_log(self, log_id: int,
                                     bot_phone: Optional[str] = None,
                                     message_id: Optional[int] = None) -> None:
        if not log_id:
            return
        sets = []
        vals = []
        idx = 1
        if bot_phone is not None:
            sets.append(f"bot_phone=${idx}")
            vals.append(bot_phone)
            idx += 1
        if message_id is not None:
            sets.append(f"message_id=${idx}")
            vals.append(message_id)
            idx += 1
        if not sets:
            return
        vals.append(log_id)
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE auto_reply_log SET {', '.join(sets)} WHERE id=${idx}",
                *vals
            )

    async def list_auto_replies(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 500))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, user_id, username, display_name, 
                       bot_phone, message_id, created_at
                FROM auto_reply_log ORDER BY id DESC LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def list_auto_replies_for_user(self, user_id: int, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 500))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, user_id, username, display_name,
                       bot_phone, message_id, created_at
                FROM auto_reply_log WHERE user_id=$1 ORDER BY id DESC LIMIT $2
            """, user_id, limit)
            return [dict(r) for r in rows]

    async def clear_auto_replies(self, user_id: Optional[int] = None) -> int:
        async with self.pool.acquire() as conn:
            if user_id:
                return await conn.execute(
                    'DELETE FROM auto_reply_log WHERE user_id=$1', user_id
                )
            else:
                return await conn.execute('TRUNCATE TABLE auto_reply_log')

    # ===== Stats =====

    async def get_stats(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            direct = await conn.fetchval(
                'SELECT COUNT(*) FROM direct_reply_messages'
            ) or 0
            blocked_text = await conn.fetchval(
                'SELECT COUNT(*) FROM blocked_reply_messages'
            ) or 0
            blocked_users = await conn.fetchval(
                'SELECT COUNT(*) FROM blocked_users'
            ) or 0
            groups = await conn.fetchval(
                'SELECT COUNT(*) FROM join_groups'
            ) or 0
            accounts = await conn.fetchval(
                'SELECT COUNT(*) FROM accounts WHERE is_active = true'
            ) or 0
            return {
                "direct": direct,
                "blocked_text": blocked_text,
                "blocked_users": blocked_users,
                "groups": groups,
                "accounts": accounts,
            }

    # ===== Account Management =====

    async def get_all_accounts(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM accounts ORDER BY created_at'
            )
            return [dict(r) for r in rows]

    async def get_active_accounts(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT * FROM accounts WHERE is_active = $1 ORDER BY created_at',
                True
            )
            return [dict(r) for r in rows]

    async def get_account(self, phone: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM accounts WHERE phone=$1', phone
            )
            return dict(row) if row else None

    async def add_account(self, phone: str, api_id: int, api_hash: str,
                          target_group_id: int, mode: str = 'both',
                          session_string: Optional[str] = None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO accounts (phone, api_id, api_hash, target_group_id, mode, session_string)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (phone) DO UPDATE SET
                    api_id = EXCLUDED.api_id,
                    api_hash = EXCLUDED.api_hash,
                    target_group_id = EXCLUDED.target_group_id,
                    mode = EXCLUDED.mode,
                    session_string = EXCLUDED.session_string,
                    is_active = true,
                    updated_at = NOW()
            """, phone, api_id, api_hash, target_group_id, mode, session_string)

    async def update_account_session(self, phone: str,
                                      session_string: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE accounts SET session_string=$1, updated_at=NOW() WHERE phone=$2',
                session_string, phone
            )

    async def update_account_status(self, phone: str, is_connected: bool,
                                     telegram_id: Optional[int] = None,
                                     display_name: Optional[str] = None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE accounts 
                SET is_connected=$1, telegram_id=$2, display_name=$3, updated_at=NOW()
                WHERE phone=$4
            """, is_connected, telegram_id, display_name, phone)

    async def set_account_active(self, phone: str, active: bool) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE accounts SET is_active=$1 WHERE phone=$2',
                active, phone
            )

    async def delete_account(self, phone: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM accounts WHERE phone=$1', phone)

    # ===== Backup & Restore =====

    async def export_json_gz(self, out_path: str):
        tables = {
            "direct_reply_messages": "message_text",
            "blocked_reply_messages": "message_text",
            "auto_reply_responses": "message_text",
            "join_groups": "group_link",
            "blocked_users": None,
            "accounts": None,
        }

        payload = {
            "meta": {
                "version": 2,
                "created_at": datetime.datetime.utcnow().isoformat() + "Z"
            },
            "tables": {}
        }

        async with self.pool.acquire() as conn:
            for table, col in tables.items():
                if col:
                    rows = await conn.fetch(f'SELECT {col} FROM {table}')
                    payload["tables"][table] = [r[col] for r in rows]
                else:
                    rows = await conn.fetch(f'SELECT * FROM {table}')
                    payload["tables"][table] = [dict(r) for r in rows]

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        data = orjson.dumps(payload)
        with gzip.open(out_path, "wb") as f:
            f.write(data)

        self.cfg.logger.info(f"Backup exported to {out_path}")

    async def import_json_gz(self, in_path: str):
        with gzip.open(in_path, "rb") as f:
            payload = orjson.loads(f.read())

        tables_data = payload.get("tables", {})

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if "direct_reply_messages" in tables_data:
                    for text in tables_data["direct_reply_messages"]:
                        await conn.execute(
                            'INSERT INTO direct_reply_messages(message_text) '
                            'VALUES($1) ON CONFLICT DO NOTHING',
                            text
                        )
                if "blocked_reply_messages" in tables_data:
                    for text in tables_data["blocked_reply_messages"]:
                        await conn.execute(
                            'INSERT INTO blocked_reply_messages(message_text) '
                            'VALUES($1) ON CONFLICT DO NOTHING',
                            text
                        )
                if "auto_reply_responses" in tables_data:
                    for text in tables_data["auto_reply_responses"]:
                        await conn.execute(
                            'INSERT INTO auto_reply_responses(message_text) '
                            'VALUES($1) ON CONFLICT DO NOTHING',
                            text
                        )
                if "join_groups" in tables_data:
                    for link in tables_data["join_groups"]:
                        await conn.execute(
                            'INSERT INTO join_groups(group_link) '
                            'VALUES($1) ON CONFLICT DO NOTHING',
                            link
                        )
                if "blocked_users" in tables_data:
                    for u in tables_data["blocked_users"]:
                        await conn.execute("""
                            INSERT INTO blocked_users 
                                (user_id, username, display_name, created_at)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT (user_id) DO UPDATE SET
                                username = EXCLUDED.username,
                                display_name = EXCLUDED.display_name
                        """,
                            u.get("user_id"),
                            u.get("username", "") or "",
                            u.get("display_name", "") or "",
                            u.get("created_at") or datetime.datetime.utcnow().isoformat()
                        )
                if "accounts" in tables_data:
                    for a in tables_data["accounts"]:
                        await conn.execute("""
                            INSERT INTO accounts 
                                (phone, api_id, api_hash, target_group_id, mode, session_string,
                                 is_active, display_name, telegram_id, created_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                            ON CONFLICT (phone) DO UPDATE SET
                                api_id = EXCLUDED.api_id,
                                api_hash = EXCLUDED.api_hash,
                                target_group_id = EXCLUDED.target_group_id,
                                mode = EXCLUDED.mode,
                                session_string = EXCLUDED.session_string,
                                is_active = EXCLUDED.is_active,
                                display_name = EXCLUDED.display_name,
                                telegram_id = EXCLUDED.telegram_id
                        """,
                            a.get("phone"), a.get("api_id"), a.get("api_hash"),
                            a.get("target_group_id"), a.get("mode", "both"),
                            a.get("session_string"), a.get("is_active", True),
                            a.get("display_name"), a.get("telegram_id"),
                            a.get("created_at") or datetime.datetime.utcnow().isoformat()
                        )

        self.cfg.logger.info(f"Restore completed from {in_path}")
