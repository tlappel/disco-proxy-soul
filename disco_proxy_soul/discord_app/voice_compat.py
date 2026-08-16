"""Narrow compatibility guards for the pinned Discord receive extension."""

from __future__ import annotations

import logging
import threading
from typing import Any

from discord.opus import OpusError
from discord.ext.voice_recv.router import MultiDataEvent, PacketDecoder, PacketRouter


log = logging.getLogger(__name__)
_WAITER_LOCK_CREATION = threading.Lock()


def _compat_waiter_lock(waiter: Any) -> threading.RLock:
    lock = getattr(waiter, "_dps_compat_lock", None)
    if lock is not None:
        return lock
    with _WAITER_LOCK_CREATION:
        lock = getattr(waiter, "_dps_compat_lock", None)
        if lock is None:
            lock = threading.RLock()
            setattr(waiter, "_dps_compat_lock", lock)
    return lock


def _increment_router_counter(item: Any, name: str, amount: int = 1) -> None:
    router = getattr(item, "router", None)
    if router is not None:
        setattr(router, name, getattr(router, name, 0) + amount)


def _validate_pinned_receive_shape() -> None:
    """Fail closed if the pinned extension no longer has the patched contract."""

    required_decoder = (
        "_flag_ready_state",
        "_get_next_packet",
        "push_packet",
        "pop_data",
        "reset",
    )
    required_waiter = ("register", "unregister", "_check_ready")
    if not all(callable(getattr(PacketDecoder, name, None)) for name in required_decoder):
        raise RuntimeError(
            "Unsupported discord-ext-voice-recv PacketDecoder shape; "
            "voice compatibility guard was not installed"
        )
    if not all(callable(getattr(MultiDataEvent, name, None)) for name in required_waiter):
        raise RuntimeError(
            "Unsupported discord-ext-voice-recv waiter shape; "
            "voice compatibility guard was not installed"
        )
    try:
        waiter = MultiDataEvent()
    except Exception as exc:
        raise RuntimeError(
            "discord-ext-voice-recv waiter could not be validated"
        ) from exc
    if not (
        isinstance(getattr(waiter, "_items", None), list)
        and callable(getattr(getattr(waiter, "_ready", None), "set", None))
        and callable(getattr(getattr(waiter, "_ready", None), "clear", None))
    ):
        raise RuntimeError(
            "Unsupported discord-ext-voice-recv waiter storage shape; "
            "voice compatibility guard was not installed"
        )
    jitter_factory = getattr(PacketDecoder.__init__, "__globals__", {}).get(
        "JitterBuffer"
    )
    if not callable(jitter_factory):
        raise RuntimeError(
            "Unsupported discord-ext-voice-recv jitter buffer; "
            "voice compatibility guard was not installed"
        )
    try:
        jitter = jitter_factory()
    except Exception as exc:
        raise RuntimeError(
            "discord-ext-voice-recv jitter buffer could not be validated"
        ) from exc
    if not (
        hasattr(jitter, "_has_item")
        and callable(getattr(jitter._has_item, "is_set", None))
        and hasattr(jitter, "_buffer")
        and hasattr(jitter, "maxsize")
        and callable(getattr(jitter, "pop", None))
    ):
        raise RuntimeError(
            "Unsupported discord-ext-voice-recv jitter readiness shape; "
            "voice compatibility guard was not installed"
        )


def _install_jitter_readiness_guard() -> None:
    _validate_pinned_receive_shape()
    if getattr(PacketDecoder, "_dps_jitter_readiness_guard", False):
        return

    def idempotent_register(self: MultiDataEvent[Any], item: Any) -> None:
        with _compat_waiter_lock(self):
            matches = sum(existing is item for existing in self._items)
            if matches:
                # Collapse any stale duplicates left by the unpatched method
                # and prevent this call from adding another occurrence.
                kept = False
                unique: list[Any] = []
                for existing in self._items:
                    if existing is item:
                        if kept:
                            continue
                        kept = True
                    unique.append(existing)
                self._items[:] = unique
                _increment_router_counter(
                    item,
                    "_dps_waiter_duplicates_prevented",
                    matches,
                )
            else:
                self._items.append(item)
            self._ready.set()

    def unregister_all(self: MultiDataEvent[Any], item: Any) -> None:
        with _compat_waiter_lock(self):
            self._items[:] = [
                existing for existing in self._items if existing is not item
            ]
            self._check_ready()

    def flag_actual_readiness(self: PacketDecoder) -> None:
        ready_event = getattr(self._buffer, "_has_item", None)
        if ready_event is None or not callable(getattr(ready_event, "is_set", None)):
            raise RuntimeError(
                "discord-ext-voice-recv jitter readiness changed during runtime"
            )
        if ready_event.is_set():
            self.router.waiter.register(self)
        else:
            self.router.waiter.unregister(self)

    def get_one_ready_packet(self: PacketDecoder, timeout: float) -> Any | None:
        buffer = self._buffer
        full_release = (
            len(buffer._buffer) >= buffer.maxsize
            and buffer._has_item.is_set()
        )
        packet = buffer.pop(timeout=timeout)
        if packet is None:
            # A sequence gap is not permission to flush the jitter buffer.
            # Its own full-buffer readiness rule will eventually release one
            # oldest packet and retain the remainder for ordered processing.
            return None
        if full_release:
            _increment_router_counter(
                self, "_dps_compat_forced_full_releases"
            )
        if not packet:
            packet = self._make_fakepacket()
        return packet

    MultiDataEvent.register = idempotent_register
    MultiDataEvent.unregister = unregister_all
    PacketDecoder._flag_ready_state = flag_actual_readiness
    PacketDecoder._get_next_packet = get_one_ready_packet
    PacketDecoder._dps_jitter_readiness_guard = True


def install_voice_receive_compatibility() -> None:
    """Install pinned jitter/waiter readiness and corrupt-frame resilience.

    discord-ext-voice-recv PR #58 adds inbound DAVE decryption, while the
    complementary PR #57 catches occasional corrupt/early frames. The pinned
    fork contains #58 only, so keep this small guard here until upstream ships
    both fixes together.
    """
    _install_jitter_readiness_guard()
    if getattr(PacketRouter, "_dps_corrupt_frame_guard", False):
        return

    def _dave_safe_run(self: PacketRouter) -> None:
        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in self.waiter.items:
                    try:
                        data = decoder.pop_data()
                    except OpusError:
                        count = getattr(self, "_dps_corrupt_frame_count", 0) + 1
                        self._dps_corrupt_frame_count = count
                        try:
                            decoder.reset()
                        except Exception as reset_exc:
                            log.error(
                                "Discord voice decoder reset failed with %s",
                                type(reset_exc).__name__,
                            )
                        else:
                            self._dps_opus_resets = (
                                getattr(self, "_dps_opus_resets", 0) + 1
                            )
                        if count == 1 or count % 100 == 0:
                            log.warning(
                                "Dropped corrupt Discord voice frame %s; "
                                "receive router is still running",
                                count,
                            )
                        continue
                    if data is not None:
                        self.sink.write(data.source, data)
        log.info(
            "Discord receive compatibility counters: full_releases=%s "
            "duplicates_prevented=%s opus_resets=%s",
            getattr(self, "_dps_compat_forced_full_releases", 0),
            getattr(self, "_dps_waiter_duplicates_prevented", 0),
            getattr(self, "_dps_opus_resets", 0),
        )

    PacketRouter._do_run = _dave_safe_run
    PacketRouter._dps_corrupt_frame_guard = True


def install_corrupt_frame_guard() -> None:
    """Backward-compatible name for the broader receive compatibility patch."""

    install_voice_receive_compatibility()
