"""
═══════════════════════════════════════════════════════════════════════════════
Data Models — Enums, Dataclasses, TypedDicts
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class AccountStatus(str, Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    BANNED = "banned"
    FLOOD = "flood"


class BotMode(str, Enum):
    FORWARD = "forward"
    REPLY = "reply"
    BOTH = "both"
    SELF = "self"


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskType(str, Enum):
    JOIN_GROUPS = "join_groups"
    SEND_MESSAGE = "send_message"
    BACKUP = "backup"
    RESTORE = "restore"


@dataclass
class AccountInfo:
    id: int
    phone: str
    api_id: int
    api_hash: str
    target_group_id: int
    mode: str = "both"
    status: AccountStatus = AccountStatus.PENDING
    session_string: Optional[str] = None
    created_at: Optional[datetime.datetime] = None
    last_error: Optional[str] = None
    last_connected: Optional[datetime.datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "phone": self.phone,
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "target_group_id": self.target_group_id,
            "mode": self.mode,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_error": self.last_error,
            "last_connected": self.last_connected.isoformat() if self.last_connected else None,
        }


@dataclass
class BotStats:
    direct: int = 0
    blocked_text: int = 0
    blocked_users: int = 0
    groups: int = 0
    accounts: int = 0
    active_accounts: int = 0
    pending_tasks: int = 0
    keywords: int = 0
    excluded_groups: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "direct": self.direct,
            "blocked_text": self.blocked_text,
            "blocked_users": self.blocked_users,
            "groups": self.groups,
            "accounts": self.accounts,
            "active_accounts": self.active_accounts,
            "pending_tasks": self.pending_tasks,
            "keywords": self.keywords,
            "excluded_groups": self.excluded_groups,
        }


@dataclass
class TaskInfo:
    id: int
    task_type: str
    payload: Dict[str, Any]
    status: TaskStatus
    created_at: datetime.datetime
    processed_at: Optional[datetime.datetime] = None


@dataclass
class FilterConfig:
    name: str
    is_active: bool = True
    threshold: Optional[int] = None


@dataclass
class SessionInfo:
    phone: str
    session_string: str
    updated_at: Optional[datetime.datetime] = None
