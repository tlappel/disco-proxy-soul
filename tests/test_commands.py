"""Tests for user-visible Discord command disclosures."""

from __future__ import annotations

import unittest

from disco_proxy_soul.discord_app.commands import live_start_notice


class LiveVoiceNoticeTests(unittest.TestCase):
    def test_tts_notice_discloses_both_external_processors_before_start(self):
        notice = live_start_notice("Travis", tts_enabled=True)
        self.assertIn("Gladia", notice)
        self.assertIn("companion cognition", notice)
        self.assertIn("ElevenLabs", notice)
        self.assertIn("saves no raw WAV, PCM, or generated audio", notice)

    def test_text_only_notice_does_not_claim_elevenlabs_transmission(self):
        notice = live_start_notice("Travis", tts_enabled=False)
        self.assertIn("Discord text", notice)
        self.assertNotIn("ElevenLabs", notice)


if __name__ == "__main__":
    unittest.main()
