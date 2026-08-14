"""Tests for the outreach cheap gate. No model calls."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from disco_proxy_soul.outreach import OutreachState


def _state(**overrides) -> OutreachState:
    tmp = tempfile.TemporaryDirectory()
    state = OutreachState(
        Path(tmp.name) / "out.json",
        timezone="America/Chicago",
        enabled=True,
        max_per_day=2,
        min_silence_h=2.0,
        no_cooldown_h=2.0,
        sleep_cooldown_h=2.0,
        quiet_start=22,
        quiet_end=5,
        watch_channel_id=123,
    )
    state._tmpdir = tmp  # type: ignore[attr-defined]
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class OutreachTests(unittest.TestCase):
    def test_disabled_and_no_channel(self) -> None:
        state = _state(enabled=False)
        self.assertEqual(state.cheap_block(), "disabled")
        state = _state(watch_channel_id=0)
        self.assertEqual(state.cheap_block(), "no watch channel")

    def test_quiet_hours_wrap_midnight(self) -> None:
        state = _state()
        night = datetime(2026, 8, 14, 23, 0, tzinfo=ZoneInfo("America/Chicago"))
        noon = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
        self.assertTrue(state._in_quiet_hours(night))
        self.assertFalse(state._in_quiet_hours(noon))

    def test_daily_max_and_silence(self) -> None:
        state = _state()
        state.data["date"] = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
        state.data["count"] = 2
        self.assertEqual(state.cheap_block(), "daily max")
        state.data["count"] = 0
        state.data["last_activity"] = datetime.now(ZoneInfo("America/Chicago")).isoformat()
        self.assertEqual(state.cheap_block(), "silence gate")

    def test_eligible_when_quiet_long_enough(self) -> None:
        state = _state(quiet_start=0, quiet_end=0)
        state.data["last_activity"] = "2020-01-01T00:00:00-06:00"
        state.data["count"] = 0
        self.assertIsNone(state.cheap_block())


if __name__ == "__main__":
    unittest.main()
