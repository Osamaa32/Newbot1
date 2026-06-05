"""
═══════════════════════════════════════════════════════════════
  FASTAPI SERVER — REST API + WebSocket for Dashboard
═══════════════════════════════════════════════════════════════
"""

import json
import asyncio
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.settings import get_settings
from shared.models import (
    AccountStatus, init_engine, SessionLocal,
    create_tables, seed_defaults,
)
from shared.database import (
    AccountRepository, GroupRepository, KeywordRepository,
    TriggerPhraseRepository, BlockedUserRepository,
    ExcludedGroupRepository, BotSettingRepository,
    StatsService, SystemLogRepository, AutoReplyLogRepository,
    AdminSessionRepository,
)

logger = logging.getLogger(__name__)

# ═════════════════ Pydantic Models ═════════════════

class AccountCreate(BaseModel):
    phone: str
    api_id: int
    api_hash: str
    target_group_id: int
    mode: str = "both"

class AccountResponse(BaseModel):
    phone: str
    status: str
    mode: str
    target_group_id: int
    display_name: Optional[str] = None
    is_connected: bool = False

class GroupCreate(BaseModel):
    group_link: str
    title: Optional[str] = None

class KeywordCreate(BaseModel):
    word: str
    category: str = "general"

class SettingUpdate(BaseModel):
    key: str
    value: str

class MessageSend(BaseModel):
    phone: str
    message: str

class BulkMessage(BaseModel):
    message: str

class BlockUser(BaseModel):
    user_id: int
    reason: Optional[str] = None

class LoginRequest(BaseModel):
    password: str


# ═════════════════ WebSocket Manager ═════════════════

class WebSocketManager:
    """Manage WebSocket connections for real-time updates"""

    def __init__(self):
        self.connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.connections:
                self.connections.remove(websocket)

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        text = json.dumps(message, default=str)
        disconnected = []
        for conn in self.connections:
            try:
                await conn.send_text(text)
            except Exception:
                disconnected.append(conn)

        async with self._lock:
            for conn in disconnected:
                if conn in self.connections:
                    self.connections.remove(conn)

    async def send_stats(self, stats: dict):
        await self.broadcast({"type": "stats", "data": stats})

    async def send_account_update(self, account: dict):
        await self.broadcast({"type": "account_update", "data": account})


ws_manager = WebSocketManager()


# ═════════════════ API Server ═════════════════

def create_api_app(db_pool, engine_manager, settings):
    """Create FastAPI application"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Lifespan events"""
        logger.info("API Server starting...")
        yield
        logger.info("API Server shutting down...")

    app = FastAPI(
        title="Telegram Control System API",
        description="REST API for managing Telegram multi-account bot",
        version="6.0.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─── Dependencies ───

    async def get_db():
        async with db_pool() as db:
            yield db

    # ─── Health Check ───

    @app.get("/health")
    async def health_check():
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "accounts_active": len(engine_manager.accounts),
            "version": "6.0.0",
        }

    # ─── Statistics ───

    @app.get("/api/stats")
    async def get_stats(db=Depends(get_db)):
        service = StatsService(db)
        stats = await service.get_full_stats()
        return stats

    @app.get("/api/engine/stats")
    async def get_engine_stats():
        return engine_manager.get_stats()

    # ─── Accounts ───

    @app.get("/api/accounts")
    async def list_accounts(db=Depends(get_db)):
        repo = AccountRepository(db)
        accounts = await repo.get_all()
        return {"accounts": [a.to_dict() for a in accounts]}

    @app.get("/api/accounts/status")
    async def accounts_status():
        status_list = await engine_manager.get_all_status()
        return {"accounts": status_list}

    @app.post("/api/accounts")
    async def create_account(data: AccountCreate):
        success, msg = await engine_manager.add_account(
            phone=data.phone,
            api_id=data.api_id,
            api_hash=data.api_hash,
            target_group_id=data.target_group_id,
            mode=data.mode,
        )
        if not success:
            raise HTTPException(status_code=400, detail=msg)
        return {"success": True, "message": msg}

    @app.post("/api/accounts/{phone}/start")
    async def start_account(phone: str):
        success, msg = await engine_manager.start_account(phone)
        return {"success": success, "message": msg}

    @app.post("/api/accounts/{phone}/stop")
    async def stop_account(phone: str):
        success, msg = await engine_manager.stop_account(phone)
        return {"success": success, "message": msg}

    @app.post("/api/accounts/{phone}/restart")
    async def restart_account(phone: str):
        success, msg = await engine_manager.restart_account(phone)
        return {"success": success, "message": msg}

    @app.delete("/api/accounts/{phone}")
    async def delete_account(phone: str):
        success, msg = await engine_manager.remove_account(phone)
        return {"success": success, "message": msg}

    @app.post("/api/accounts/{phone}/mode")
    async def set_account_mode(phone: str, mode: str):
        success, msg = await engine_manager.update_account_mode(phone, mode)
        return {"success": success, "message": msg}

    # ─── Groups ───

    @app.get("/api/groups")
    async def list_groups(db=Depends(get_db)):
        repo = GroupRepository(db)
        groups = await repo.get_all()
        return {"groups": [g.to_dict() for g in groups]}

    @app.post("/api/groups")
    async def add_group(data: GroupCreate, db=Depends(get_db)):
        repo = GroupRepository(db)
        group = await repo.create(data.group_link, data.title)
        return {"success": True, "group": group.to_dict()}

    @app.delete("/api/groups/{group_id}")
    async def delete_group(group_id: int, db=Depends(get_db)):
        repo = GroupRepository(db)
        success = await repo.delete(group_id)
        return {"success": success}

    # ─── Keywords ───

    @app.get("/api/keywords")
    async def list_keywords(db=Depends(get_db)):
        repo = KeywordRepository(db)
        keywords = await repo.get_all()
        return {"keywords": [k.to_dict() for k in keywords]}

    @app.post("/api/keywords")
    async def add_keyword(data: KeywordCreate, db=Depends(get_db)):
        repo = KeywordRepository(db)
        kw = await repo.create(data.word, data.category)
        await engine_manager._refresh_config()
        return {"success": True, "keyword": kw.to_dict()}

    @app.delete("/api/keywords/{keyword_id}")
    async def delete_keyword(keyword_id: int, db=Depends(get_db)):
        repo = KeywordRepository(db)
        success = await repo.delete(keyword_id)
        await engine_manager._refresh_config()
        return {"success": success}

    # ─── Settings ───

    @app.get("/api/settings")
    async def list_settings(db=Depends(get_db)):
        repo = BotSettingRepository(db)
        settings_list = await repo.get_all()
        return {"settings": [s.to_dict() for s in settings_list]}

    @app.put("/api/settings")
    async def update_setting(data: SettingUpdate, db=Depends(get_db)):
        repo = BotSettingRepository(db)
        await repo.set(data.key, data.value)
        await engine_manager._refresh_config()
        return {"success": True}

    # ─── Blocked Users ───

    @app.get("/api/blocked")
    async def list_blocked(db=Depends(get_db)):
        repo = BlockedUserRepository(db)
        users = await repo.get_all()
        return {"blocked_users": [u.to_dict() for u in users]}

    @app.post("/api/blocked")
    async def block_user(data: BlockUser, db=Depends(get_db)):
        repo = BlockedUserRepository(db)
        await repo.create(data.user_id, reason=data.reason)
        await engine_manager._refresh_config()
        return {"success": True}

    @app.delete("/api/blocked/{user_id}")
    async def unblock_user(user_id: int, db=Depends(get_db)):
        repo = BlockedUserRepository(db)
        success = await repo.delete(user_id)
        await engine_manager._refresh_config()
        return {"success": success}

    # ─── Logs ───

    @app.get("/api/logs")
    async def get_logs(limit: int = 100, db=Depends(get_db)):
        repo = SystemLogRepository(db)
        logs = await repo.get_recent(limit=limit)
        return {"logs": [l.to_dict() for l in logs]}

    @app.get("/api/reply-logs")
    async def get_reply_logs(limit: int = 50, db=Depends(get_db)):
        repo = AutoReplyLogRepository(db)
        logs = await repo.get_all(limit=limit)
        return {"logs": [l.to_dict() for l in logs]}

    # ─── Messages ───

    @app.post("/api/messages/send")
    async def send_message(data: MessageSend):
        engine = engine_manager.accounts.get(data.phone)
        if not engine:
            raise HTTPException(status_code=404, detail="Account not found or not active")
        from engine.utils import safe_send
        await safe_send(engine.client, engine.target_group_id, data.message)
        return {"success": True}

    @app.post("/api/messages/broadcast")
    async def broadcast_message(data: BulkMessage):
        results = await engine_manager.broadcast_message(data.message)
        return {"results": {k: {"success": v[0], "message": v[1]} for k, v in results.items()}}

    # ─── Join Groups ───

    @app.post("/api/accounts/{phone}/join")
    async def join_groups(phone: str, request: dict, db=Depends(get_db)):
        group_links = request.get("groups", [])
        if not group_links:
            raise HTTPException(status_code=400, detail="No groups provided")

        results = await engine_manager.join_groups(
            phone=phone,
            group_links=group_links,
        )
        return {"results": [{"group": g, "success": s, "message": m} for g, s, m in results]}

    # ─── WebSocket ───

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            # Send initial stats
            from shared.database import StatsService
            async with db_pool() as db:
                service = StatsService(db)
                stats = await service.get_full_stats()
            await websocket.send_text(json.dumps({"type": "stats", "data": stats}))

            # Keep connection alive
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)

                    # Handle commands
                    if msg.get("action") == "get_stats":
                        async with db_pool() as db:
                            service = StatsService(db)
                            stats = await service.get_full_stats()
                        await websocket.send_text(
                            json.dumps({"type": "stats", "data": stats})
                        )

                    elif msg.get("action") == "ping":
                        await websocket.send_text(json.dumps({"type": "pong"}))

                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.error(f"WebSocket error: {e}")
                    break

        except WebSocketDisconnect:
            pass
        finally:
            await ws_manager.disconnect(websocket)

    # ─── Auto-refresh WebSocket data ───

    @app.on_event("startup")
    async def startup_websocket_refresh():
        """Periodically broadcast stats to WebSocket clients"""
        async def refresh_loop():
            while True:
                try:
                    await asyncio.sleep(30)
                    if ws_manager.connections:
                        async with db_pool() as db:
                            service = StatsService(db)
                            stats = await service.get_full_stats()
                        await ws_manager.send_stats(stats)
                except Exception as e:
                    logger.error(f"WebSocket refresh error: {e}")

        asyncio.create_task(refresh_loop())

    return app
