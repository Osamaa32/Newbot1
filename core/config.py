import os
import re
import sys
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv


class AppConfig:
    """ Configuration — loaded once, read everywhere. """

    def __init__(self):
        load_dotenv()
        self.logger = self._build_logger()

        # Group settings
        self.EXCLUDED_GROUPS: set[int] = {
            -1002272546210, -1002405645012, -1002353780992, -1002311800895
        }
        self.FALLBACK_GROUP_ID: int = int(os.getenv("FALLBACK_GROUP_ID", "-1002353780992"))
        self.COMMAND_GROUP_ID: int = int(os.getenv("COMMAND_GROUP_ID", "-1002311800895"))
        self.COMMAND_BOT_INDEX: int = int(os.getenv("COMMAND_BOT_INDEX", "2"))

        # Rate limiting
        self.RATE_LIMIT_THRESHOLD: int = int(os.getenv("RATE_LIMIT_THRESHOLD", "4"))
        self.RATE_LIMIT_WINDOW_HOURS: int = int(os.getenv("RATE_LIMIT_WINDOW_HOURS", "24"))

        # All available commands (including account management)
        self.COMMANDS: set[str] = {
            "help", "unblock",
            "add", "del", "list", "find",
            "blkadd", "blkdel", "blklist", "blkfind",
            "autoadd", "autodel", "autolist", "autofind",
            "groupadd", "groupdel", "groupupdate", "grouplist", "joingroups",
            "stopjoin", "groupcount", "usergroups", "usergroups_notin",
            "dbbackup", "dbrestore",
            "blkuser_add", "blkuser_del", "blkuser_list", "blkuser_find",
            "autoreplies_count", "autoreplies_list", "autoreplies_clear",
            "stats",
            # Account management
            "addaccount", "accounts", "delaccount",
            "startaccount", "stopaccount", "reconnect",
        }

        # Trigger keywords (deduplicated)
        self.KEYWORDS: list[str] = list(dict.fromkeys([
            "ابي مساعده", "يسوي", "يحل", "خصوصي", "شاطر", "تحل", "تسوي", "يعرف",
            "تعرف", "واجب", "بروجكت", "فاهم", "سكليف", "بحث", "مشروع", "يساعد",
            "اسايمنت", "ابغى مساعده", "ابغا مساعده", "محتاج مساعده", "حد يساعدني",
            "احد يساعدني", "ابي حد يحضر عني", "ابغا حد يحضر عني", "يحضر عني",
            "يحظر", "يحضر", "عندي اختبار", "احد عنده خصوصي", "احد يعرف مختص",
            "س ك ل ي ف", "case study", "كيس ستدي", "بوربوينت", "بووربوينت",
            "عذر طبي", "اجازة مرضية",
        ]))
        self.KW_RE: re.Pattern = re.compile(
            "|".join(map(re.escape, self.KEYWORDS)),
            re.IGNORECASE
        )

        self.LINK_RE: re.Pattern = re.compile(
            r"(https://t\.me/(?:c/)?(?:\d+|[A-Za-z0-9_]+)/?\d*)(?:\?comment=\d+)?"
        )

    @staticmethod
    def _build_logger(name: str = "telegram-bot") -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        if logger.handlers:
            return logger

        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

        fh = RotatingFileHandler("bot.log", maxBytes=10**8, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        logger.addHandler(sh)

        return logger
