"""
═══════════════════════════════════════════════════════════════
  DATABASE CRUD OPERATIONS — Async Repository Pattern
═══════════════════════════════════════════════════════════════
"""

import json
import datetime
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy import select, update, delete, func, desc, asc, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Account, Group, Keyword, TriggerPhrase, BlockedUser,
    ExcludedGroup, BotSetting, AutoReplyLog, TaskQueue,
    SystemLog, AdminSession, MessageForwardLog,
    AccountStatus, TaskStatus, LogLevel,
)


# ═════════════════ ACCOUNT REPOSITORY ═════════════════

class AccountRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, phone: str, api_id: int, api_hash: str,
                     target_group_id: int, mode: str = "both") -> Account:
        account = Account(
            phone=phone, api_id=api_id, api_hash=api_hash,
            target_group_id=target_group_id, mode=mode,
            status=AccountStatus.PENDING.value,
        )
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        return account

    async def get_by_phone(self, phone: str) -> Optional[Account]:
        result = await self.session.execute(
            select(Account).where(Account.phone == phone)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, account_id: int) -> Optional[Account]:
        result = await self.session.execute(
            select(Account).where(Account.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> List[Account]:
        result = await self.session.execute(select(Account).order_by(Account.id))
        return result.scalars().all()

    async def get_active(self) -> List[Account]:
        result = await self.session.execute(
            select(Account).where(Account.status == AccountStatus.ACTIVE.value)
        )
        return result.scalars().all()

    async def update_status(self, phone: str, status: AccountStatus,
                            error: Optional[str] = None) -> bool:
        values = {"status": status.value}
        if error:
            values["last_error"] = error
        elif status == AccountStatus.ACTIVE:
            values["last_error"] = None
            values["last_connected"] = datetime.datetime.utcnow()
            values["connection_count"] = Account.connection_count + 1

        result = await self.session.execute(
            update(Account).where(Account.phone == phone).values(**values)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def update_session(self, phone: str, session_string: str) -> bool:
        result = await self.session.execute(
            update(Account).where(Account.phone == phone).values(
                session_string=session_string
            )
        )
        await self.session.commit()
        return result.rowcount > 0

    async def update_info(self, phone: str, **kwargs) -> bool:
        result = await self.session.execute(
            update(Account).where(Account.phone == phone).values(**kwargs)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def update_stats(self, phone: str, messages: int = 0, replies: int = 0) -> bool:
        result = await self.session.execute(
            update(Account).where(Account.phone == phone).values(
                total_messages=Account.total_messages + messages,
                total_replies=Account.total_replies + replies,
            )
        )
        await self.session.commit()
        return result.rowcount > 0

    async def delete(self, phone: str) -> bool:
        result = await self.session.execute(
            delete(Account).where(Account.phone == phone)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def count_by_status(self) -> Dict[str, int]:
        result = await self.session.execute(
            select(Account.status, func.count(Account.id)).group_by(Account.status)
        )
        return {status: count for status, count in result.all()}


# ═════════════════ GROUP REPOSITORY ═════════════════

class GroupRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, group_link: str, title: Optional[str] = None,
                     group_id: Optional[int] = None) -> Group:
        group = Group(group_link=group_link, title=title, group_id=group_id)
        self.session.add(group)
        await self.session.commit()
        await self.session.refresh(group)
        return group

    async def get_by_link(self, link: str) -> Optional[Group]:
        result = await self.session.execute(
            select(Group).where(Group.group_link == link)
        )
        return result.scalar_one_or_none()

    async def get_all(self, active_only: bool = False) -> List[Group]:
        query = select(Group).order_by(Group.id)
        if active_only:
            query = query.where(Group.is_active == True)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def update(self, group_id: int, **kwargs) -> bool:
        result = await self.session.execute(
            update(Group).where(Group.id == group_id).values(**kwargs)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def delete(self, group_id: int) -> bool:
        result = await self.session.execute(
            delete(Group).where(Group.id == group_id)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def count(self) -> int:
        result = await self.session.execute(select(func.count(Group.id)))
        return result.scalar() or 0


# ═════════════════ KEYWORD REPOSITORY ═════════════════

class KeywordRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, word: str, category: str = "general") -> Keyword:
        kw = Keyword(word=word, category=category)
        self.session.add(kw)
        await self.session.commit()
        await self.session.refresh(kw)
        return kw

    async def get_by_word(self, word: str) -> Optional[Keyword]:
        result = await self.session.execute(
            select(Keyword).where(Keyword.word == word)
        )
        return result.scalar_one_or_none()

    async def get_all(self, active_only: bool = False) -> List[Keyword]:
        query = select(Keyword).order_by(Keyword.word)
        if active_only:
            query = query.where(Keyword.is_active == True)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def update(self, word_id: int, **kwargs) -> bool:
        result = await self.session.execute(
            update(Keyword).where(Keyword.id == word_id).values(**kwargs)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def delete(self, word_id: int) -> bool:
        result = await self.session.execute(
            delete(Keyword).where(Keyword.id == word_id)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def increment_match(self, word_id: int) -> bool:
        result = await self.session.execute(
            update(Keyword).where(Keyword.id == word_id).values(
                match_count=Keyword.match_count + 1
            )
        )
        await self.session.commit()
        return result.rowcount > 0


# ═════════════════ TRIGGER PHRASE REPOSITORY ═════════════════

class TriggerPhraseRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, phrase: str, phrase_type: str = "direct") -> TriggerPhrase:
        tp = TriggerPhrase(phrase=phrase, phrase_type=phrase_type)
        self.session.add(tp)
        await self.session.commit()
        await self.session.refresh(tp)
        return tp

    async def get_all(self, phrase_type: Optional[str] = None,
                      active_only: bool = False) -> List[TriggerPhrase]:
        query = select(TriggerPhrase)
        if phrase_type:
            query = query.where(TriggerPhrase.phrase_type == phrase_type)
        if active_only:
            query = query.where(TriggerPhrase.is_active == True)
        query = query.order_by(TriggerPhrase.id)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def update(self, phrase_id: int, **kwargs) -> bool:
        result = await self.session.execute(
            update(TriggerPhrase).where(TriggerPhrase.id == phrase_id).values(**kwargs)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def delete(self, phrase_id: int) -> bool:
        result = await self.session.execute(
            delete(TriggerPhrase).where(TriggerPhrase.id == phrase_id)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def toggle(self, phrase_id: int) -> bool:
        phrase = await self.session.get(TriggerPhrase, phrase_id)
        if phrase:
            phrase.is_active = not phrase.is_active
            await self.session.commit()
            return True
        return False


# ═════════════════ BLOCKED USER REPOSITORY ═════════════════

class BlockedUserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: int, username: Optional[str] = None,
                     display_name: Optional[str] = None,
                     reason: Optional[str] = None,
                     blocked_by: Optional[int] = None) -> BlockedUser:
        bu = BlockedUser(
            user_id=user_id, username=username,
            display_name=display_name, reason=reason, blocked_by=blocked_by,
        )
        self.session.add(bu)
        await self.session.commit()
        await self.session.refresh(bu)
        return bu

    async def get_by_user_id(self, user_id: int) -> Optional[BlockedUser]:
        result = await self.session.execute(
            select(BlockedUser).where(BlockedUser.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_all(self, limit: int = 500) -> List[BlockedUser]:
        result = await self.session.execute(
            select(BlockedUser).order_by(desc(BlockedUser.created_at)).limit(limit)
        )
        return result.scalars().all()

    async def search(self, pattern: str, limit: int = 100) -> List[BlockedUser]:
        like_pattern = f"%{pattern}%"
        result = await self.session.execute(
            select(BlockedUser).where(
                or_(
                    func.cast(BlockedUser.user_id, String).like(like_pattern),
                    BlockedUser.username.ilike(like_pattern),
                    BlockedUser.display_name.ilike(like_pattern),
                )
            ).order_by(desc(BlockedUser.created_at)).limit(limit)
        )
        return result.scalars().all()

    async def delete(self, user_id: int) -> bool:
        result = await self.session.execute(
            delete(BlockedUser).where(BlockedUser.user_id == user_id)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def exists(self, user_id: int) -> bool:
        result = await self.session.execute(
            select(func.count(BlockedUser.id)).where(BlockedUser.user_id == user_id)
        )
        return (result.scalar() or 0) > 0


# ═════════════════ EXCLUDED GROUP REPOSITORY ═════════════════

class ExcludedGroupRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, group_id: int, reason: Optional[str] = None) -> ExcludedGroup:
        eg = ExcludedGroup(group_id=group_id, reason=reason)
        self.session.add(eg)
        await self.session.commit()
        await self.session.refresh(eg)
        return eg

    async def get_all(self) -> List[ExcludedGroup]:
        result = await self.session.execute(
            select(ExcludedGroup).order_by(desc(ExcludedGroup.created_at))
        )
        return result.scalars().all()

    async def get_all_ids(self) -> List[int]:
        result = await self.session.execute(select(ExcludedGroup.group_id))
        return [r[0] for r in result.all()]

    async def delete(self, group_id: int) -> bool:
        result = await self.session.execute(
            delete(ExcludedGroup).where(ExcludedGroup.group_id == group_id)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def exists(self, group_id: int) -> bool:
        result = await self.session.execute(
            select(func.count(ExcludedGroup.id)).where(ExcludedGroup.group_id == group_id)
        )
        return (result.scalar() or 0) > 0


# ═════════════════ BOT SETTINGS REPOSITORY ═════════════════

class BotSettingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, key: str, default: str = "") -> str:
        result = await self.session.execute(
            select(BotSetting).where(BotSetting.key == key)
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else default

    async def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(await self.get(key, str(default)))
        except ValueError:
            return default

    async def get_bool(self, key: str, default: bool = False) -> bool:
        val = await self.get(key, str(default).lower())
        return val.lower() in ("true", "1", "yes", "on")

    async def set(self, key: str, value: str, updated_by: Optional[int] = None) -> None:
        result = await self.session.execute(
            select(BotSetting).where(BotSetting.key == key)
        )
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = value
            setting.updated_by = updated_by
            setting.updated_at = datetime.datetime.utcnow()
        else:
            setting = BotSetting(key=key, value=value, updated_by=updated_by)
            self.session.add(setting)
        await self.session.commit()

    async def get_all(self) -> List[BotSetting]:
        result = await self.session.execute(select(BotSetting).order_by(BotSetting.key))
        return result.scalars().all()

    async def reset(self, key: str) -> bool:
        """Reset to default value"""
        defaults = {
            "fallback_group_id": "-1002353780992",
            "command_group_id": "-1002311800895",
            "rate_limit_max": "4",
            "rate_limit_window": "3600",
            "cb_failure_threshold": "5",
            "cb_recovery_timeout": "1800",
            "max_concurrent_dispatch": "20",
            "ttl_cache_seconds": "7200",
            "fuzzy_threshold": "80",
            "fuzzy_exact_threshold": "100",
            "join_delay_base": "30",
            "join_delay_random": "30",
            "max_message_length": "4000",
            "default_auto_reply": "ارسلت ذي في الجروب 😇\n\nابشر/ي اساعدك",
            "word_count_limit": "17",
            "filter_mention": "true",
            "filter_links": "true",
            "filter_digits": "true",
            "filter_private": "true",
            "filter_outgoing": "true",
            "filter_bots": "true",
            "filter_admins": "true",
            "auto_reply_enabled": "true",
            "forward_enabled": "true",
        }
        if key not in defaults:
            return False
        await self.set(key, defaults[key])
        return True


# ═════════════════ AUTO REPLY LOG REPOSITORY ═════════════════

class AutoReplyLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, user_id: int, dedupe_key: str,
                     username: Optional[str] = None,
                     display_name: Optional[str] = None,
                     bot_phone: Optional[str] = None,
                     message_id: Optional[int] = None,
                     source_chat_id: Optional[int] = None,
                     source_message_id: Optional[int] = None,
                     message_text: Optional[str] = None,
                     reply_text: Optional[str] = None) -> Optional[AutoReplyLog]:
        try:
            log = AutoReplyLog(
                user_id=user_id, dedupe_key=dedupe_key,
                username=username, display_name=display_name,
                bot_phone=bot_phone, message_id=message_id,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                message_text=message_text, reply_text=reply_text,
            )
            self.session.add(log)
            await self.session.commit()
            await self.session.refresh(log)
            return log
        except Exception:
            await self.session.rollback()
            return None

    async def count_by_user(self, user_id: int, hours: int = 24) -> int:
        since = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        result = await self.session.execute(
            select(func.count(AutoReplyLog.id)).where(
                and_(
                    AutoReplyLog.user_id == user_id,
                    AutoReplyLog.created_at >= since,
                )
            )
        )
        return result.scalar() or 0

    async def get_all(self, limit: int = 50) -> List[AutoReplyLog]:
        result = await self.session.execute(
            select(AutoReplyLog).order_by(desc(AutoReplyLog.id)).limit(limit)
        )
        return result.scalars().all()

    async def get_by_user(self, user_id: int, limit: int = 50) -> List[AutoReplyLog]:
        result = await self.session.execute(
            select(AutoReplyLog).where(AutoReplyLog.user_id == user_id)
            .order_by(desc(AutoReplyLog.id)).limit(limit)
        )
        return result.scalars().all()

    async def clear_all(self) -> int:
        result = await self.session.execute(
            select(func.count(AutoReplyLog.id))
        )
        count = result.scalar() or 0
        await self.session.execute(delete(AutoReplyLog))
        await self.session.commit()
        return count

    async def clear_by_user(self, user_id: int) -> int:
        result = await self.session.execute(
            delete(AutoReplyLog).where(AutoReplyLog.user_id == user_id)
        )
        await self.session.commit()
        return result.rowcount or 0


# ═════════════════ TASK QUEUE REPOSITORY ═════════════════

class TaskQueueRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, task_type: str, payload: dict,
                     priority: int = 5,
                     account_phone: Optional[str] = None) -> TaskQueue:
        task = TaskQueue(
            task_type=task_type,
            payload=json.dumps(payload),
            priority=priority,
            account_phone=account_phone,
            status=TaskStatus.PENDING.value,
        )
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def get_next_pending(self) -> Optional[TaskQueue]:
        result = await self.session.execute(
            select(TaskQueue).where(TaskQueue.status == TaskStatus.PENDING.value)
            .order_by(asc(TaskQueue.priority), asc(TaskQueue.id))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_status(self, task_id: int, status: TaskStatus,
                            error_message: Optional[str] = None) -> bool:
        values = {"status": status.value}
        if status == TaskStatus.PROCESSING:
            values["processed_at"] = datetime.datetime.utcnow()
        elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            values["completed_at"] = datetime.datetime.utcnow()
        if error_message:
            values["error_message"] = error_message

        result = await self.session.execute(
            update(TaskQueue).where(TaskQueue.id == task_id).values(**values)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def get_stats(self) -> Dict[str, int]:
        result = await self.session.execute(
            select(TaskQueue.status, func.count(TaskQueue.id)).group_by(TaskQueue.status)
        )
        return {status: count for status, count in result.all()}


# ═════════════════ SYSTEM LOG REPOSITORY ═════════════════

class SystemLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, message: str, level: str = LogLevel.INFO.value,
                     source: str = "system",
                     account_phone: Optional[str] = None,
                     metadata: Optional[dict] = None) -> SystemLog:
        log = SystemLog(
            message=message, level=level, source=source,
            account_phone=account_phone,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        self.session.add(log)
        await self.session.commit()
        await self.session.refresh(log)
        return log

    async def get_recent(self, limit: int = 100,
                         level: Optional[str] = None,
                         source: Optional[str] = None) -> List[SystemLog]:
        query = select(SystemLog).order_by(desc(SystemLog.created_at)).limit(limit)
        if level:
            query = query.where(SystemLog.level == level)
        if source:
            query = query.where(SystemLog.source == source)
        result = await self.session.execute(query)
        return result.scalars().all()

    async def clear_old(self, days: int = 30) -> int:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        result = await self.session.execute(
            delete(SystemLog).where(SystemLog.created_at < cutoff)
        )
        await self.session.commit()
        return result.rowcount or 0


# ═════════════════ ADMIN SESSION REPOSITORY ═════════════════

class AdminSessionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_user_id(self, user_id: int) -> Optional[AdminSession]:
        result = await self.session.execute(
            select(AdminSession).where(AdminSession.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def create_or_update(self, user_id: int, username: Optional[str] = None,
                                first_name: Optional[str] = None,
                                is_authenticated: bool = False) -> AdminSession:
        result = await self.session.execute(
            select(AdminSession).where(AdminSession.user_id == user_id)
        )
        session_obj = result.scalar_one_or_none()

        if session_obj:
            if username:
                session_obj.username = username
            if first_name:
                session_obj.first_name = first_name
            session_obj.is_authenticated = is_authenticated
            session_obj.last_activity = datetime.datetime.utcnow()
            if is_authenticated:
                session_obj.login_count = AdminSession.login_count + 1
        else:
            session_obj = AdminSession(
                user_id=user_id, username=username,
                first_name=first_name, is_authenticated=is_authenticated,
            )
            self.session.add(session_obj)

        await self.session.commit()
        await self.session.refresh(session_obj)
        return session_obj

    async def is_authenticated(self, user_id: int) -> bool:
        result = await self.session.execute(
            select(AdminSession.is_authenticated).where(AdminSession.user_id == user_id)
        )
        return result.scalar_one_or_none() or False

    async def get_all_sessions(self) -> List[AdminSession]:
        result = await self.session.execute(
            select(AdminSession).order_by(desc(AdminSession.last_activity))
        )
        return result.scalars().all()


# ═════════════════ FORWARD LOG REPOSITORY ═════════════════

class ForwardLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, source_chat_id: int, source_message_id: int,
                     sender_id: Optional[int], sender_username: Optional[str],
                     sender_name: Optional[str], message_text: Optional[str],
                     forwarded_by: str, target_group_id: int) -> MessageForwardLog:
        log = MessageForwardLog(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            sender_id=sender_id,
            sender_username=sender_username,
            sender_name=sender_name,
            message_text=message_text,
            forwarded_by=forwarded_by,
            target_group_id=target_group_id,
        )
        self.session.add(log)
        await self.session.commit()
        await self.session.refresh(log)
        return log

    async def get_recent(self, limit: int = 100) -> List[MessageForwardLog]:
        result = await self.session.execute(
            select(MessageForwardLog).order_by(desc(MessageForwardLog.created_at)).limit(limit)
        )
        return result.scalars().all()

    async def get_stats_today(self) -> int:
        today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self.session.execute(
            select(func.count(MessageForwardLog.id)).where(
                MessageForwardLog.created_at >= today
            )
        )
        return result.scalar() or 0


# ═════════════════ STATS SERVICE ═════════════════

class StatsService:
    """Aggregated statistics service"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_full_stats(self) -> Dict[str, Any]:
        # Account stats
        account_repo = AccountRepository(self.session)
        status_counts = await account_repo.count_by_status()

        # Group stats
        group_repo = GroupRepository(self.session)
        group_count = await group_repo.count()

        # Keyword stats
        kw_repo = KeywordRepository(self.session)
        keywords = await kw_repo.get_all()

        # Blocked users
        blocked_repo = BlockedUserRepository(self.session)
        blocked_users = await blocked_repo.get_all(limit=1000)

        # Reply logs
        reply_repo = AutoReplyLogRepository(self.session)
        today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Forward logs today
        forward_repo = ForwardLogRepository(self.session)
        forwards_today = await forward_repo.get_stats_today()

        # Task stats
        task_repo = TaskQueueRepository(self.session)
        task_stats = await task_repo.get_stats()

        return {
            "accounts": {
                "total": sum(status_counts.values()),
                "by_status": status_counts,
                "active": status_counts.get(AccountStatus.ACTIVE.value, 0),
            },
            "groups": {
                "total": group_count,
            },
            "keywords": {
                "total": len(keywords),
                "active": len([k for k in keywords if k.is_active]),
            },
            "blocked_users": len(blocked_users),
            "forwards_today": forwards_today,
            "tasks": task_stats,
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }
