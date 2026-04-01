import asyncio
import random
import sqlite3
import time
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@register("helloworld", "Trae", "群聊复读插件", "1.0.0")
class RepeaterPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}
        self._db: Optional[sqlite3.Connection] = None
        self._db_lock = asyncio.Lock()
        self._plugin_data_dir = get_astrbot_data_path() / "plugin_data" / "helloworld"
        self._db_path = self._plugin_data_dir / "repeater.db"

    async def initialize(self):
        self._plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_db()
        logger.info(f"[helloworld] sqlite 已初始化: {self._db_path}")

    async def terminate(self):
        if self._db:
            self._db.close()
            self._db = None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("repeater_status")
    async def repeater_status(self, event: AstrMessageEvent):
        session_key = self._get_session_key(event)
        enabled = await self._is_session_enabled(session_key)
        state = await self._get_session_state(session_key)
        total_repeats = await self._get_repeat_count(session_key)
        threshold = self._get_config_int("repeat_threshold", 3)
        probability = self._get_config_float("repeat_probability", 1.0)
        cooldown = self._get_config_int("cooldown_seconds", 60)
        mode = self._get_scope_mode()
        yield event.plain_result(
            "\n".join(
                [
                    f"复读开关: {'开启' if enabled else '关闭'}",
                    f"作用范围: {mode}",
                    f"触发阈值: {threshold}",
                    f"触发概率: {probability}",
                    f"冷却时间: {cooldown}秒",
                    f"当前连续文本: {state['message_text'] if state else '无'}",
                    f"当前连续次数: {state['repeat_count'] if state else 0}",
                    f"本会话累计复读次数: {total_repeats}",
                ]
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("repeater_on")
    async def repeater_on(self, event: AstrMessageEvent):
        session_key = self._get_session_key(event)
        await self._set_session_enabled(session_key, True)
        yield event.plain_result("当前会话已开启复读。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("repeater_off")
    async def repeater_off(self, event: AstrMessageEvent):
        session_key = self._get_session_key(event)
        await self._set_session_enabled(session_key, False)
        yield event.plain_result("当前会话已关闭复读。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("repeater_reset")
    async def repeater_reset(self, event: AstrMessageEvent):
        session_key = self._get_session_key(event)
        await self._reset_session_state(session_key)
        yield event.plain_result("当前会话的复读状态已重置。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self._should_handle_event(event):
            return

        session_key = self._get_session_key(event)
        if not await self._is_session_enabled(session_key):
            return

        normalized = self._normalize_message(event.message_str)
        if not normalized:
            await self._clear_session_tracking(session_key)
            return

        repeated_message = await self._record_message(session_key, normalized, event)
        if not repeated_message:
            return

        yield event.plain_result(repeated_message)

    def _init_db(self):
        if not self._db:
            return
        cursor = self._db.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS session_state (
                session_key TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                message_text TEXT NOT NULL DEFAULT '',
                repeat_count INTEGER NOT NULL DEFAULT 0,
                last_message_at INTEGER NOT NULL DEFAULT 0,
                last_repeat_at INTEGER NOT NULL DEFAULT 0,
                repeated_message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS session_switch (
                session_key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS repeat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                is_repeated INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_repeat_log_session_created_at ON repeat_log(session_key, created_at)"
        )
        self._db.commit()

    def _should_handle_event(self, event: AstrMessageEvent) -> bool:
        if self._is_command_message(event.message_str):
            return False
        if getattr(event, "is_self", False):
            return False

        scope_mode = self._get_scope_mode()
        group_id = getattr(event.message_obj, "group_id", "") or ""
        is_group = bool(group_id)

        if scope_mode == "group" and not is_group:
            return False
        if scope_mode == "private" and is_group:
            return False

        return True

    def _get_scope_mode(self) -> str:
        mode = str(self.config.get("scope_mode", "group") or "group").lower()
        if mode not in {"group", "private", "all"}:
            return "group"
        return mode

    def _normalize_message(self, message: str) -> str:
        if not message:
            return ""
        text = " ".join(message.split())
        if not text:
            return ""
        if len(text) < self._get_config_int("min_length", 1):
            return ""
        if self._get_config_bool("ignore_pure_digits", False) and text.isdigit():
            return ""
        blacklist = self._get_config_list("ignore_prefixes", ["/", "!", "."])
        if any(text.startswith(prefix) for prefix in blacklist if prefix):
            return ""
        return text[: self._get_config_int("max_message_length", 200)]

    def _is_command_message(self, message: str) -> bool:
        if not message:
            return False
        prefixes = self._get_config_list("command_prefixes", ["/", "!", "."])
        stripped = message.lstrip()
        return any(stripped.startswith(prefix) for prefix in prefixes if prefix)

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        scope_type, scope_id = self._extract_scope(event)
        return f"{scope_type}:{scope_id}"

    def _extract_scope(self, event: AstrMessageEvent) -> tuple[str, str]:
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        if group_id:
            return "group", group_id
        session_id = str(getattr(event.message_obj, "session_id", "") or event.unified_msg_origin)
        return "private", session_id

    async def _record_message(self, session_key: str, normalized: str, event: AstrMessageEvent) -> Optional[str]:
        if not self._db:
            return None

        scope_type, scope_id = self._extract_scope(event)
        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or ""
        now = int(time.time())
        threshold = max(2, self._get_config_int("repeat_threshold", 3))
        cooldown = max(0, self._get_config_int("cooldown_seconds", 60))
        probability = min(1.0, max(0.0, self._get_config_float("repeat_probability", 1.0)))

        async with self._db_lock:
            state = self._db.execute(
                "SELECT * FROM session_state WHERE session_key = ?",
                (session_key,),
            ).fetchone()

            if state and state["message_text"] == normalized:
                repeat_count = state["repeat_count"] + 1
                last_repeat_at = state["last_repeat_at"]
                repeated_message = state["repeated_message"]
            else:
                repeat_count = 1
                last_repeat_at = 0
                repeated_message = ""

            should_repeat = (
                repeat_count >= threshold
                and repeated_message != normalized
                and now - last_repeat_at >= cooldown
                and random.random() <= probability
            )

            self._db.execute(
                """
                INSERT INTO session_state (
                    session_key, scope_type, scope_id, message_text, repeat_count,
                    last_message_at, last_repeat_at, repeated_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    scope_type = excluded.scope_type,
                    scope_id = excluded.scope_id,
                    message_text = excluded.message_text,
                    repeat_count = excluded.repeat_count,
                    last_message_at = excluded.last_message_at,
                    last_repeat_at = excluded.last_repeat_at,
                    repeated_message = excluded.repeated_message
                """,
                (
                    session_key,
                    scope_type,
                    scope_id,
                    normalized,
                    repeat_count,
                    now,
                    now if should_repeat else last_repeat_at,
                    normalized if should_repeat else repeated_message,
                ),
            )
            self._db.execute(
                """
                INSERT INTO repeat_log (
                    session_key, scope_type, scope_id, sender_id, sender_name,
                    raw_text, normalized_text, is_repeated, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_key,
                    scope_type,
                    scope_id,
                    sender_id,
                    sender_name,
                    event.message_str or "",
                    normalized,
                    1 if should_repeat else 0,
                    now,
                ),
            )
            self._db.commit()

        if should_repeat:
            logger.info(f"[helloworld] 在 {session_key} 触发复读: {normalized}")
            return normalized
        return None

    async def _clear_session_tracking(self, session_key: str):
        if not self._db:
            return
        async with self._db_lock:
            self._db.execute(
                """
                INSERT INTO session_state (
                    session_key, scope_type, scope_id, message_text, repeat_count,
                    last_message_at, last_repeat_at, repeated_message
                ) VALUES (?, ?, ?, '', 0, 0, 0, '')
                ON CONFLICT(session_key) DO UPDATE SET
                    message_text = '',
                    repeat_count = 0,
                    last_message_at = 0,
                    repeated_message = ''
                """,
                (session_key, session_key.split(":", 1)[0], session_key.split(":", 1)[1]),
            )
            self._db.commit()

    async def _is_session_enabled(self, session_key: str) -> bool:
        if not self._db:
            return True
        async with self._db_lock:
            row = self._db.execute(
                "SELECT enabled FROM session_switch WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        if row is None:
            return self._get_config_bool("default_enabled", True)
        return bool(row["enabled"])

    async def _set_session_enabled(self, session_key: str, enabled: bool):
        if not self._db:
            return
        now = int(time.time())
        async with self._db_lock:
            self._db.execute(
                """
                INSERT INTO session_switch (session_key, enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (session_key, 1 if enabled else 0, now),
            )
            self._db.commit()

    async def _reset_session_state(self, session_key: str):
        if not self._db:
            return
        async with self._db_lock:
            self._db.execute("DELETE FROM session_state WHERE session_key = ?", (session_key,))
            self._db.commit()

    async def _get_session_state(self, session_key: str) -> Optional[sqlite3.Row]:
        if not self._db:
            return None
        async with self._db_lock:
            return self._db.execute(
                "SELECT * FROM session_state WHERE session_key = ?",
                (session_key,),
            ).fetchone()

    async def _get_repeat_count(self, session_key: str) -> int:
        if not self._db:
            return 0
        async with self._db_lock:
            row = self._db.execute(
                "SELECT COUNT(1) AS total FROM repeat_log WHERE session_key = ? AND is_repeated = 1",
                (session_key,),
            ).fetchone()
        return int(row["total"] if row else 0)

    def _get_config_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_config_float(self, key: str, default: float) -> float:
        value = self.config.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _get_config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _get_config_list(self, key: str, default: list[str]) -> list[str]:
        value = self.config.get(key, default)
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            return [value]
        return list(default)
