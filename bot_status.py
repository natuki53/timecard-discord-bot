"""Write a small, fixed-schema Discord bot heartbeat."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

StatusValue = list[dict[str, object]]
StatusProvider = Callable[[], StatusValue | Awaitable[StatusValue]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_latency(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    latency = float(value)
    if not math.isfinite(latency) or latency < 0:
        return None
    return min(60_000, round(latency))


def _normalize_dependencies(value: Any) -> StatusValue:
    if not isinstance(value, list):
        return []

    dependencies: StatusValue = []
    for item in value:
        if not isinstance(item, dict):
            continue
        dependency_id = item.get("id")
        connected = item.get("connected")
        if (
            not isinstance(dependency_id, str)
            or not dependency_id
            or len(dependency_id) > 64
            or not isinstance(connected, bool)
        ):
            continue
        dependencies.append({"id": dependency_id, "connected": connected})
    return dependencies


class BotStatusReporter:
    """Publish status without exposing Discord guild or user information."""

    def __init__(
        self,
        *,
        bot_id: str,
        discord_connected: Callable[[], bool],
        gateway_latency_ms: Callable[[], int | float | None],
        dependencies: StatusProvider | None = None,
        output_path: str | Path | None = None,
        interval_seconds: float = 10,
    ) -> None:
        configured_path = output_path or os.getenv("BOT_STATUS_PATH")
        self.output_path = Path(configured_path) if configured_path else None
        self.bot_id = bot_id
        self.discord_connected = discord_connected
        self.gateway_latency_ms = gateway_latency_ms
        self.dependencies = dependencies
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.process_started_at = _utc_now()
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self.output_path is not None

    async def build_payload(self, heartbeat_at: datetime | None = None) -> dict[str, object]:
        raw_connected = self.discord_connected()
        connected = raw_connected if isinstance(raw_connected, bool) else False
        latency = _normalize_latency(self.gateway_latency_ms()) if connected else None
        dependency_values: Any = []
        if self.dependencies is not None:
            dependency_values = self.dependencies()
            if inspect.isawaitable(dependency_values):
                dependency_values = await dependency_values

        return {
            "version": 1,
            "botId": self.bot_id,
            "processStartedAt": _isoformat(self.process_started_at),
            "heartbeatAt": _isoformat(heartbeat_at or _utc_now()),
            "discordConnected": connected,
            "gatewayLatencyMs": latency,
            "dependencies": _normalize_dependencies(dependency_values),
        }

    async def publish(self, heartbeat_at: datetime | None = None) -> dict[str, object] | None:
        if self.output_path is None:
            return None

        payload = await self.build_payload(heartbeat_at)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.output_path.parent,
                prefix=f".{self.output_path.name}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)

            temporary_path.chmod(0o644)
            temporary_path.replace(self.output_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return payload

    def start(self) -> None:
        if not self.enabled or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.create_task(
            self._run(),
            name=f"{self.bot_id}-status-heartbeat",
        )

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.publish()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Bot status heartbeat could not be written")
            await asyncio.sleep(self.interval_seconds)
