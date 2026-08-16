"""Tests for narrow compatibility patches around Discord voice receive."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
import io
import logging
from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

from discord.ext.voice_recv.router import MultiDataEvent, PacketDecoder, PacketRouter
from discord.opus import OpusError

from disco_proxy_soul.discord_app.voice_compat import (
    _validate_pinned_receive_shape,
    install_corrupt_frame_guard,
    install_voice_receive_compatibility,
)
from disco_proxy_soul.discord_app.bot import configure_application_logging


class VoiceCompatibilityTests(unittest.TestCase):
    def test_version_shape_guard_fails_closed(self) -> None:
        decoder_globals = PacketDecoder.__init__.__globals__
        jitter = decoder_globals.get("JitterBuffer")
        try:
            decoder_globals["JitterBuffer"] = None
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                _validate_pinned_receive_shape()
        finally:
            decoder_globals["JitterBuffer"] = jitter

    def test_version_shape_is_revalidated_after_install_marker(self) -> None:
        install_voice_receive_compatibility()
        original = PacketDecoder.push_packet
        try:
            PacketDecoder.push_packet = None
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                install_voice_receive_compatibility()
        finally:
            PacketDecoder.push_packet = original

    def test_waiter_registration_is_atomic_across_threads(self) -> None:
        router, waiter, decoder = self._decoder()
        barrier = threading.Barrier(3)

        def register() -> None:
            barrier.wait()
            waiter.register(decoder)

        workers = [threading.Thread(target=register) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()

        self.assertEqual(
            sum(item is decoder for item in waiter.items),
            1,
        )
        self.assertEqual(router._dps_waiter_duplicates_prevented, 1)

    def test_corrupt_frame_does_not_stop_next_packet(self) -> None:
        install_corrupt_frame_guard()
        install_corrupt_frame_guard()

        error = OpusError.__new__(OpusError)
        Exception.__init__(error, "corrupt")
        writes: list[str] = []
        end = SimpleNamespace(done=False)
        end.is_set = lambda: end.done
        waiter = SimpleNamespace(items=[])
        waiter.wait = lambda: setattr(end, "done", True)
        resets: list[str] = []
        bad_decoder = SimpleNamespace(
            pop_data=lambda: (_ for _ in ()).throw(error),
            reset=lambda: resets.append("bad"),
        )
        good_decoder = SimpleNamespace(
            pop_data=lambda: SimpleNamespace(source="speaker"),
            reset=lambda: resets.append("good"),
        )
        waiter.items = [bad_decoder, good_decoder]
        router = SimpleNamespace(
            _end_thread=end,
            waiter=waiter,
            _lock=nullcontext(),
            sink=SimpleNamespace(
                write=lambda source, data: writes.append(source)
            ),
        )

        PacketRouter._do_run(router)

        self.assertEqual(writes, ["speaker"])
        self.assertEqual(router._dps_corrupt_frame_count, 1)
        self.assertEqual(router._dps_opus_resets, 1)
        self.assertEqual(resets, ["bad"])

    def test_production_application_logger_emits_compat_summary_once(self) -> None:
        install_voice_receive_compatibility()
        app_logger = logging.getLogger("disco_proxy_soul")
        saved_handlers = list(app_logger.handlers)
        saved_level = app_logger.level
        saved_propagate = app_logger.propagate
        output = io.StringIO()
        app_logger.handlers.clear()
        try:
            configure_application_logging("logger-secret", stream=output)
            configure_application_logging("logger-secret", stream=output)
            end = SimpleNamespace(done=False)
            end.is_set = lambda: end.done
            waiter = SimpleNamespace(items=[])
            waiter.wait = lambda: setattr(end, "done", True)
            router = SimpleNamespace(
                _end_thread=end,
                waiter=waiter,
                _lock=nullcontext(),
                sink=SimpleNamespace(write=lambda source, data: None),
                _dps_compat_forced_full_releases=12,
                _dps_waiter_duplicates_prevented=3,
                _dps_opus_resets=1,
            )
            PacketRouter._do_run(router)
            console = output.getvalue()
            self.assertEqual(
                console.count("Discord receive compatibility counters"), 1
            )
            self.assertIn("full_releases=12", console)
            self.assertIn("duplicates_prevented=3", console)
            self.assertNotIn("logger-secret", console)
        finally:
            for handler in app_logger.handlers:
                handler.close()
            app_logger.handlers[:] = saved_handlers
            app_logger.setLevel(saved_level)
            app_logger.propagate = saved_propagate

    @staticmethod
    def _packet(sequence: int, timestamp: int | None = None):
        class Packet:
            def __init__(self, seq, ts):
                self.ssrc = 42
                self.sequence = seq
                self.timestamp = ts

            def __lt__(self, other):
                if self.ssrc != other.ssrc:
                    raise TypeError("ssrc mismatch")
                return (
                    self.sequence < other.sequence
                    and self.timestamp < other.timestamp
                )

        return Packet(sequence, sequence * 960 if timestamp is None else timestamp)

    def _decoder(self):
        install_corrupt_frame_guard()
        waiter = MultiDataEvent()
        router = SimpleNamespace(
            waiter=waiter,
            sink=SimpleNamespace(wants_opus=lambda: True),
        )
        decoder = PacketDecoder(router, 42)
        decoder._buffer.flush = MagicMock(
            side_effect=AssertionError("compat path must never flush")
        )
        return router, waiter, decoder

    def _push(self, waiter, decoder, packets):
        for packet in packets:
            decoder.push_packet(packet)
            self.assertLessEqual(
                sum(item is decoder for item in waiter.items),
                1,
            )

    def _drain_ready(self, waiter, decoder):
        popped = []
        for _ in range(50):
            if decoder not in waiter.items:
                break
            packet = decoder._get_next_packet(0)
            decoder._flag_ready_state()
            if packet is not None:
                popped.append(packet)
            self.assertLessEqual(
                sum(item is decoder for item in waiter.items),
                1,
            )
        else:
            self.fail("decoder readiness did not settle")
        return popped

    def _assert_conserved(self, decoder, pushed, popped):
        remaining = list(decoder._buffer._buffer)
        self.assertEqual(
            sorted(id(packet) for packet in pushed),
            sorted(id(packet) for packet in popped + remaining),
        )
        decoder._buffer.flush.assert_not_called()

    def test_installed_jitter_contiguous_and_gap_do_not_flush(self) -> None:
        router, waiter, decoder = self._decoder()
        contiguous = [self._packet(seq) for seq in (1, 2, 3)]
        self._push(waiter, decoder, contiguous)
        popped = self._drain_ready(waiter, decoder)
        self.assertEqual([packet.sequence for packet in popped], [1, 2])
        self._assert_conserved(decoder, contiguous, popped)
        self.assertGreater(router._dps_waiter_duplicates_prevented, 0)

        router, waiter, decoder = self._decoder()
        gapped = [self._packet(seq) for seq in (1, 3, 4)]
        self._push(waiter, decoder, gapped)
        popped = self._drain_ready(waiter, decoder)
        self.assertEqual([packet.sequence for packet in popped], [1])
        self.assertEqual(len(decoder._buffer._buffer), 2)
        tail = [self._packet(seq) for seq in range(5, 13)]
        self._push(waiter, decoder, tail)
        popped.extend(self._drain_ready(waiter, decoder))
        self._assert_conserved(decoder, gapped + tail, popped)
        self.assertGreaterEqual(router._dps_compat_forced_full_releases, 1)

    def test_installed_jitter_duplicate_and_late_reorder_are_retained(self) -> None:
        for sequences in ((1, 2, 2, 3), (10, 12, 11)):
            with self.subTest(sequences=sequences):
                _, waiter, decoder = self._decoder()
                packets = [self._packet(seq) for seq in sequences]
                self._push(waiter, decoder, packets)
                popped = self._drain_ready(waiter, decoder)
                self._assert_conserved(decoder, packets, popped)
                self.assertLessEqual(
                    sum(item is decoder for item in waiter.items), 1
                )

    def test_installed_jitter_wrap_and_anomalous_timestamps_do_not_amplify(self) -> None:
        router, waiter, decoder = self._decoder()
        wrapped = [
            self._packet(65535, 1_060),
            self._packet(0, 2_020),
        ]
        decoder._buffer._prefill = 0
        decoder._buffer._last_tx_seq = 65534
        self._push(waiter, decoder, wrapped)
        popped = []
        tail = [self._packet(seq, 2_980 + seq * 960) for seq in range(1, 9)]
        self._push(waiter, decoder, tail)
        popped.extend(self._drain_ready(waiter, decoder))
        self._assert_conserved(decoder, wrapped + tail, popped)
        self.assertGreaterEqual(router._dps_compat_forced_full_releases, 1)

        _, waiter, decoder = self._decoder()
        anomalous = [
            self._packet(20, 5_000),
            self._packet(21, 5_000),
            self._packet(22, 4_000),
        ]
        self._push(waiter, decoder, anomalous)
        popped = self._drain_ready(waiter, decoder)
        self._assert_conserved(decoder, anomalous, popped)


if __name__ == "__main__":
    unittest.main()
