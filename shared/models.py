"""
═══════════════════════════════════════════════════════════════
  DATABASE MODELS — SQLAlchemy Async ORM
═══════════════════════════════════════════════════════════════
"""

import enum
import datetime
from typing import Optional, List
from sqlalchemy import (
    String, Integer, BigInteger, Boolean, Text, DateTime,
    ForeignKey, UniqueConstraint, Index, select, delete, update, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs


class Base(AsyncAttrs, DeclarativeBase):
    pass


# ═════════════════ ENUMS ═════════════════

class AccountStatus(str, enum.Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    BANNED = "banned"
    FLOOD = "flood"


class AccountMode(str, enum.Enum):
    FORWARD = "forward"
    REPLY = "reply"
    BOTH = "both"
    SELF = "self"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class LogLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ═════════════════ TABLES ═════════════════

class Account(Base):
    """Telegram accounts managed by the system"""
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    api_id: Mapped[int] = mapped_column(Integer, nullable=False)
    api_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="both")
    status: Mapped[str] = mapped_column(String(20), default=AccountStatus.PENDING.value)
    session_string: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )
    last_connected: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    connection_count: Mapped[int] = mapped_column(Integer, default=0)
    total_messages: Mapped[int] = mapped_column(BigInteger, default=0)
    total_replies: Mapped[int] = mapped_column(BigInteger, default=0)

    # Relationships
    logs: Mapped[List["SystemLog"]] = relationship(back_populates="account", lazy="selectin")
    tasks: Mapped[List["TaskQueue"]] = relationship(back_populates="account", lazy="selectin")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "phone": self.phone,
            "api_id": self.api_id,
            "api_hash": "***" if self.api_hash else None,
            "target_group_id": self.target_group_id,
            "mode": self.mode,
            "status": self.status,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
            "telegram_id": self.telegram_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_connected": self.last_connected.isoformat() if self.last_connected else None,
            "last_error": self.last_error,
            "connection_count": self.connection_count,
            "total_messages": self.total_messages,
            "total_replies": self.total_replies,
        }


class Group(Base):
    """Groups/channels to join and monitor"""
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_link: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    member_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "group_link": self.group_link,
            "title": self.title,
            "group_id": self.group_id,
            "member_count": self.member_count,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Keyword(Base):
    """Keywords to monitor in messages"""
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    word: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), default="general")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "word": self.word,
            "category": self.category,
            "is_active": self.is_active,
            "match_count": self.match_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TriggerPhrase(Base):
    """Direct reply trigger phrases"""
    __tablename__ = "trigger_phrases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phrase: Mapped[str] = mapped_column(String(500), nullable=False)
    phrase_type: Mapped[str] = mapped_column(String(20), default="direct")  # direct, blocked, auto
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("phrase", "phrase_type", name="uq_phrase_type"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "phrase": self.phrase,
            "phrase_type": self.phrase_type,
            "is_active": self.is_active,
            "use_count": self.use_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BlockedUser(Base):
    """Blocked users (blacklist)"""
    __tablename__ = "blocked_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, unique=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    blocked_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ExcludedGroup(Base):
    """Excluded groups (ignored by the bot)"""
    __tablename__ = "excluded_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BotSetting(Base):
    """Dynamic bot settings"""
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )
    updated_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "description": self.description,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }


class AutoReplyLog(Base):
    """Log of auto-replies sent"""
    __tablename__ = "auto_reply_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    bot_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reply_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("idx_auto_reply_user", "user_id"),
        Index("idx_auto_reply_created", "created_at"),
        UniqueConstraint("user_id", "dedupe_key", name="uq_auto_user_dedupe"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "bot_phone": self.bot_phone,
            "message_id": self.message_id,
            "source_chat_id": self.source_chat_id,
            "source_message_id": self.source_message_id,
            "message_text": self.message_text,
            "reply_text": self.reply_text,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TaskQueue(Base):
    """Background task queue"""
    __tablename__ = "task_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.PENDING.value)
    priority: Mapped[int] = mapped_column(Integer, default=5)  # 1=highest, 10=lowest
    account_phone: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("accounts.phone"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    processed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    account: Mapped[Optional["Account"]] = relationship(back_populates="tasks", lazy="selectin")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "status": self.status,
            "priority": self.priority,
            "account_phone": self.account_phone,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
        }


class SystemLog(Base):
    """System activity logs"""
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(20), default=LogLevel.INFO.value)
    source: Mapped[str] = mapped_column(String(50), default="system")  # system, account, bot, api
    message: Mapped[str] = mapped_column(Text, nullable=False)
    account_phone: Mapped[Optional[str]] = mapped_column(String(32), ForeignKey("accounts.phone"), nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    account: Mapped[Optional["Account"]] = relationship(back_populates="logs", lazy="selectin")

    __table_args__ = (
        Index("idx_logs_level", "level"),
        Index("idx_logs_source", "source"),
        Index("idx_logs_created", "created_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "level": self.level,
            "source": self.source,
            "message": self.message,
            "account_phone": self.account_phone,
            "metadata_json": self.metadata_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdminSession(Base):
    """Admin authentication sessions"""
    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_method: Mapped[str] = mapped_column(String(20), default="password")  # password, owner_id
    last_activity: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    login_count: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "first_name": self.first_name,
            "is_authenticated": self.is_authenticated,
            "auth_method": self.auth_method,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "login_count": self.login_count,
        }


class MessageForwardLog(Base):
    """Log of forwarded messages"""
    __tablename__ = "forward_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sender_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    sender_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    forwarded_by: Mapped[str] = mapped_column(String(32), nullable=False)  # phone number
    target_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("idx_forward_sender", "sender_id"),
        Index("idx_forward_created", "created_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_chat_id": self.source_chat_id,
            "source_message_id": self.source_message_id,
            "sender_id": self.sender_id,
            "sender_username": self.sender_username,
            "sender_name": self.sender_name,
            "message_text": self.message_text,
            "forwarded_by": self.forwarded_by,
            "target_group_id": self.target_group_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ─── Database Manager ───

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

engine = None
SessionLocal = None


def init_engine(database_url: str, pool_size: int = 5):
    """Initialize async database engine"""
    global engine, SessionLocal
    engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


async def create_tables():
    """Create all tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables():
    """Drop all tables (USE WITH CAUTION)"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def get_db():
    """Get database session"""
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def seed_defaults(db_session):
    """Seed default data"""
    # Default settings
    default_settings = [
        ("fallback_group_id", "-1002353780992", "Fallback group for failed forwards"),
        ("command_group_id", "-1002311800895", "Group where commands are accepted"),
        ("rate_limit_max", "4", "Max auto-replies per user per window"),
        ("rate_limit_window", "3600", "Rate limit window in seconds"),
        ("cb_failure_threshold", "5", "Failures before circuit breaker opens"),
        ("cb_recovery_timeout", "1800", "Seconds before circuit breaker resets"),
        ("max_concurrent_dispatch", "20", "Max concurrent message processing"),
        ("ttl_cache_seconds", "7200", "TTL for forward/reply dedupe cache"),
        ("fuzzy_threshold", "80", "Fuzzy match threshold for find commands"),
        ("fuzzy_exact_threshold", "100", "Exact match threshold"),
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
        ("auto_reply_enabled", "true", "Enable auto-reply feature"),
        ("forward_enabled", "true", "Enable message forwarding"),
        ("monitor_all_groups", "false", "Monitor all joined groups"),
    ]

    for key, value, desc in default_settings:
        result = await db_session.execute(select(BotSetting).where(BotSetting.key == key))
        if result.scalar_one_or_none() is None:
            db_session.add(BotSetting(key=key, value=value, description=desc))

    # Default keywords
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
        result = await db_session.execute(select(Keyword).where(Keyword.word == kw))
        if result.scalar_one_or_none() is None:
            db_session.add(Keyword(word=kw, category="general"))

    await db_session.commit()
