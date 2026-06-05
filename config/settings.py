"""
═══════════════════════════════════════════════════════════════
  APPLICATION SETTINGS — Pydantic Settings with .env support
═══════════════════════════════════════════════════════════════
"""

import os
import secrets
from typing import Optional, List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All application settings loaded from environment / .env"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── Control Bot ───
    BOT_TOKEN: str = Field(..., description="Telegram bot token from @BotFather")
    ADMIN_PASSWORD: str = Field(..., description="Password for first-time admin authentication")
    OWNER_ID: Optional[int] = Field(None, description="Telegram user ID of the owner")

    # ─── Database ───
    DATABASE_URL: str = Field(..., description="PostgreSQL connection string")

    # ─── Redis ───
    REDIS_URL: Optional[str] = Field(None, description="Redis connection string")

    # ─── API Server ───
    API_PORT: int = Field(8000, description="FastAPI server port")
    API_HOST: str = Field("0.0.0.0", description="FastAPI server host")

    # ─── Dashboard ───
    DASHBOARD_PORT: int = Field(3000, description="Dashboard React app port")
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_hex(32), description="JWT secret key")

    # ─── Health Check ───
    HEALTH_PORT: int = Field(8080, description="Health check server port")

    # ─── Logging ───
    LOG_LEVEL: str = Field("INFO", description="Logging level")

    # ─── Environment ───
    ENVIRONMENT: str = Field("development", description="Environment: development / staging / production")

    # ─── Auto-Start Accounts ───
    AUTO_START_ACCOUNTS: bool = Field(True, description="Auto-start saved accounts on boot")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def fix_postgres_url(cls, v):
        """Ensure URL uses postgresql+asyncpg:// for SQLAlchemy async"""
        if not v:
            return v
        # Railway gives postgres:// — convert to postgresql+asyncpg://
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://") and "+asyncpg" not in v:
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in ("production", "prod", "railway")

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT.lower() in ("development", "dev")

    @property
    def db_pool_size(self) -> int:
        return 10 if self.is_production else 5


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create settings singleton"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from .env"""
    global _settings
    _settings = Settings()
    return _settings
