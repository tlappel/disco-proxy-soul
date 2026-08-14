"""Outreach gate — cheapest checks first, model gate is the caller's job."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .memory.store import load_json, save_json

DEFAULT_STATE: dict[str, str | int] = {
    "date": "",
    "count": 0,
    "last_outreach": "",
    "last_activity": "",
    "last_gate_check": "",
    "last_gate_result": "",
    "last_gate_reason": "",
    "next_gate_check": "",
    "activity_at_gate": "",
    "loop_started": "",
    "last_loop_tick": "",
    "last_loop_error": "",
}


class OutreachState:
    def __init__(
        self,
        path: Path,
        *,
        timezone: str,
        enabled: bool,
        max_per_day: int,
        min_silence_h: float,
        no_cooldown_h: float,
        sleep_cooldown_h: float,
        quiet_start: int,
        quiet_end: int,
        watch_channel_id: int,
    ) -> None:
        self.path = path
        self.tz = ZoneInfo(timezone)
        self.enabled = enabled
        self.max_per_day = max_per_day
        self.min_silence_h = min_silence_h
        self.no_cooldown_h = no_cooldown_h
        self.sleep_cooldown_h = sleep_cooldown_h
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.watch_channel_id = watch_channel_id
        loaded = load_json(str(path), dict(DEFAULT_STATE))
        self.data: dict = loaded if isinstance(loaded, dict) else dict(DEFAULT_STATE)

    def persist(self) -> None:
        save_json(str(self.path), self.data)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def note_activity(self, clear_gate: bool = True) -> None:
        self.data["last_activity"] = self.now().isoformat()
        if clear_gate:
            self._clear_gate()
        self.persist()

    def reset_day(self) -> None:
        self.data["date"] = self.now().strftime("%Y-%m-%d")
        self.data["count"] = 0
        self.data["last_outreach"] = ""
        self.data["last_activity"] = ""
        self._clear_gate()
        self.persist()

    def record_loop_tick(self) -> None:
        self.data["last_loop_tick"] = self.now().isoformat()
        self.persist()

    def record_loop_error(self, error: Exception) -> None:
        now = self.now()
        self.data["last_loop_error"] = f"{now.isoformat()} {type(error).__name__}: {error}"
        self.persist()

    def record_loop_started(self) -> None:
        self.data["loop_started"] = self.now().isoformat()
        self.data["last_loop_error"] = ""
        self.persist()

    def record_gate(self, result: str, reason: str = "") -> None:
        now = self.now()
        self.data["last_gate_check"] = now.isoformat()
        self.data["last_gate_result"] = result
        self.data["last_gate_reason"] = reason
        self.data["activity_at_gate"] = self.data.get("last_activity", "")
        if result == "no":
            hours = self._cooldown_hours(reason)
            self.data["next_gate_check"] = (now + timedelta(hours=hours)).isoformat()
        else:
            self.data["next_gate_check"] = ""
        self.persist()

    def mark_sent(self) -> None:
        now = self.now()
        today = now.strftime("%Y-%m-%d")
        if self.data.get("date") != today:
            self.data["date"] = today
            self.data["count"] = 0
        self.data["count"] = int(self.data.get("count") or 0) + 1
        self.data["last_outreach"] = now.isoformat()
        self.note_activity(clear_gate=False)

    def cheap_block(self) -> str | None:
        """Return a skip reason, or None if the model gate may run."""
        now = self.now()
        if not self.enabled:
            return "disabled"
        if not self.watch_channel_id:
            return "no watch channel"
        if self._in_quiet_hours(now):
            return "quiet hours"
        today = now.strftime("%Y-%m-%d")
        if self.data.get("date") != today:
            self.data["date"] = today
            self.data["count"] = 0
            self.persist()
        if int(self.data.get("count") or 0) >= self.max_per_day:
            return "daily max"
        if self.hours_since_activity(now) < self.min_silence_h:
            return "silence gate"
        if self._gate_cooldown_active(now):
            return "gate cooldown"
        return None

    def hours_since_activity(self, now: datetime | None = None) -> float:
        now = now or self.now()
        last = self.data.get("last_activity") or self.data.get("last_outreach")
        if not last:
            return 999.0
        try:
            return (now - datetime.fromisoformat(str(last))).total_seconds() / 3600
        except (ValueError, TypeError):
            return 999.0

    def status(self) -> dict[str, str]:
        now = self.now()
        today = now.strftime("%Y-%m-%d")
        count = self.data.get("count", 0) if self.data.get("date") == today else 0
        silence_h = self.hours_since_activity(now)
        block = self.cheap_block()
        if block == "disabled":
            status, reason, next_time = "blocked", "outreach disabled", "enable outreach"
        elif block == "no watch channel":
            status, reason, next_time = "blocked", "watch channel not configured", "set WATCH_CHANNEL_ID"
        elif block == "quiet hours":
            status, reason, next_time = "blocked", "quiet hours", f"{self.quiet_end:02d}:00 {self.tz.key}"
        elif block == "daily max":
            nxt = datetime(now.year, now.month, now.day, tzinfo=self.tz) + timedelta(days=1)
            status, reason, next_time = "paused", "daily max reached", nxt.strftime("%Y-%m-%d %H:%M")
        elif block == "silence gate":
            wait = self.min_silence_h - silence_h
            status, reason, next_time = "waiting", "silence gate not met", f"in about {max(0.0, wait):.1f}h"
        elif block == "gate cooldown":
            nxt = self._parse_dt(str(self.data.get("next_gate_check") or ""))
            status = "waiting"
            reason = f"gate cooldown after last no: {self.data.get('last_gate_reason') or 'no reason'}"
            next_time = nxt.strftime("%Y-%m-%d %H:%M") if nxt else "later"
        else:
            status, reason, next_time = "eligible", "ready for model gate", "next outreach loop tick"
        return {
            "status": status,
            "reason": reason,
            "next_time": next_time,
            "count": str(count),
            "silence_h": f"{silence_h:.1f}",
        }

    def _clear_gate(self) -> None:
        self.data["last_gate_check"] = ""
        self.data["last_gate_result"] = ""
        self.data["last_gate_reason"] = ""
        self.data["next_gate_check"] = ""
        self.data["activity_at_gate"] = ""

    def _cooldown_hours(self, reason: str) -> float:
        lowered = (reason or "").lower()
        if any(word in lowered for word in ("bed", "sleep", "asleep", "tired", "night")):
            return self.sleep_cooldown_h
        return self.no_cooldown_h

    def _in_quiet_hours(self, now: datetime) -> bool:
        if self.quiet_start > self.quiet_end:
            return now.hour >= self.quiet_start or now.hour < self.quiet_end
        return self.quiet_start <= now.hour < self.quiet_end

    def _gate_cooldown_active(self, now: datetime) -> bool:
        nxt = self._parse_dt(str(self.data.get("next_gate_check") or ""))
        last_activity = self.data.get("last_activity", "")
        activity_at_gate = self.data.get("activity_at_gate", "")
        changed = bool(last_activity and last_activity != activity_at_gate)
        return bool(nxt and now < nxt and not changed)

    @staticmethod
    def _parse_dt(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def format_dt(value: str) -> str:
        if not value:
            return "never"
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return str(value)[:16]
