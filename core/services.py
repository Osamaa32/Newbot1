from typing import Optional

try:
    import ahocorasick
except ImportError:
    ahocorasick = None

from telethon import events, types
from telethon.tl.types import (
    ChannelParticipantAdmin, ChannelParticipantCreator
)

from core.config import AppConfig
from core.utils import FastUtils
from core.db import PostgresDB


class TriggerMatcher:
    """ Aho-Corasick matcher — O(text_length) instead of O(triggers × texts). """

    __slots__ = ("automaton", "blocked_set", "_ready")

    def __init__(self):
        self.automaton = None
        self.blocked_set = set()
        self._ready = False

    def rebuild(self, triggers: list[str], blocked: list[str]):
        if ahocorasick is None:
            self.cfg.logger.warning("pyahocorasick not installed, falling back")
            return

        self.automaton = ahocorasick.Automaton()
        self.blocked_set = set(FastUtils.normalize_text(b) for b in blocked)
        self._ready = False

        for trig in triggers:
            norm = FastUtils.normalize_text(trig)
            if norm:
                self.automaton.add_word(norm, trig)

        self.automaton.make_automaton()
        self._ready = True

    def should_trigger(self, text: str) -> bool:
        if not self._ready or not text:
            return False

        norm = FastUtils.normalize_text(text)
        if not norm:
            return False
        if norm in self.blocked_set:
            return False

        return any(True for _ in self.automaton.iter(norm))


class MessageFormatter:
    """ Fast message formatting for forwards. """

    __slots__ = ("cfg",)

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def message_key(self, ev: events.NewMessage.Event) -> tuple:
        text = ev.message.message or ""
        m = self.cfg.LINK_RE.search(text)
        return ("link", m.group(1)) if m else ("id", ev.chat_id, ev.message.id)

    async def build_forward_text(self, ev: events.NewMessage.Event) -> str:
        text = ev.message.message or "—"

        sender = await ev.get_sender()
        if sender:
            username = getattr(sender, "username", None)
            fn = getattr(sender, "first_name", "") or ""
            ln = getattr(sender, "last_name", "") or ""
            disp = f"{fn} {ln}".strip() or f"مستخدم (ID: {sender.id})"

            if username:
                sender_line = (
                    f"👤 **المرسل:** [@{username}](https://t.me/{username})"
                )
                if disp != f"مستخدم (ID: {sender.id})":
                    sender_line += f" ({disp})"
            else:
                sender_line = f"👤 **المرسل:** {disp}"
            dm_line = (
                f"🔗 **مراسلة:** [اضغط هنا](tg://user?id={sender.id})"
            )
        else:
            sender_line = "👤 **المرسل:** غير معروف"
            dm_line = "🔗 **مراسلة:** غير متاحة"

        chat = ev.chat or await ev.get_chat()
        chat_username = getattr(chat, "username", None) if chat else None
        chat_title = getattr(chat, "title", None) if chat else None

        if chat_username:
            group_line = f"📍 **المجموعة:** @{chat_username}"
        elif chat_title:
            group_line = f"📍 **المجموعة:** {chat_title}"
        else:
            group_line = "📍 **المجموعة:** خاصة"

        # Message link
        link_line = "📜 **الرابط:** غير متاح"
        if chat and str(ev.chat_id).startswith("-100"):
            raw_id = str(ev.chat_id)[4:]
            link_line = (
                f"📜 **الرابط:** [اضغط هنا]"
                f"(https://t.me/c/{raw_id}/{ev.message.id})"
            )
        elif chat_username:
            link_line = (
                f"📜 **الرابط:** [اضغط هنا]"
                f"(https://t.me/{chat_username}/{ev.message.id})"
            )

        return f"`{text}`\n\n{sender_line}\n{dm_line}\n{group_line}\n{link_line}"


class GroupRepo:
    """ Fast group link repository. """

    __slots__ = ("db",)

    def __init__(self, db: PostgresDB):
        self.db = db

    async def add(self, link: str) -> None:
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO join_groups(group_link) VALUES($1) '
                'ON CONFLICT DO NOTHING',
                link
            )

    async def delete(self, link: str) -> None:
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                'DELETE FROM join_groups WHERE group_link=$1', link
            )

    async def update(self, old_link: str, new_link: str) -> None:
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                'UPDATE join_groups SET group_link=$1 WHERE group_link=$2',
                new_link, old_link
            )

    async def all(self) -> list[str]:
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch('SELECT group_link FROM join_groups')
            return [r['group_link'] for r in rows]
