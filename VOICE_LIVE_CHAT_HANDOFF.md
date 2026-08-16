# Voice Live Chat — coder handoff

Checkpoint date: 2026-08-15

Branch: `feature/voice-live-chat`

Current boundary: stable single-speaker live transcription; no cognition or TTS

## What works now

The Discord bot can join a private voice channel and transcribe only the member
who runs `/voice-chat start`. Discord's decoded PCM is bridged safely from the
receive thread, downmixed to mono PCM16 at 48 kHz, paced in 20 ms frames, and
streamed to one reusable Gladia Live V2 session. Partial transcripts remain
terminal diagnostics; finals are posted in Discord text. Live mode saves no raw
audio.

The lifecycle is manager-owned and deliberately defensive: one live session per
guild, diagnostic/live mutual exclusion, cancellation-safe startup and shutdown,
starter-leave auto-stop, bounded queues, redacted errors, exact Discord client
ownership, and quarantine of stubborn disconnect/Gladia-stop tasks rather than
false success or unbounded task growth.

## Verified acceptance result

Final private-room test status:

```text
Audio queues: loop 0/100, thread ring 0/100
Drops: thread 0, loop 0, clock 1
Gladia frames sent: 1911; finals: 4; partials: 17
Inserted silence: 23.08s; RTP gaps: 4.62s
RTP discontinuities: 6; playout reanchors: 4; late samples: 0
Transport completion: normal
Last warning: none
```

The intended sentence survived end to end. Gladia rendered it across nearby
finals as approximately:

```text
This is Travis testing the repaired live voice.
connection
Then quick brown fox jumps over the lazy dog.
```

The speaker intentionally said "Then," not "The." Gladia also produced a
short noise-like final (`Thank you.`), demonstrating why Phase 3 needs an
independent speech/artifact gate and final-turn assembly.

Regression verification at handoff: full suite `158/158`; focused voice,
compatibility, and Gladia suite `127/127`; targeted race/clock/logger suite
`20/20`; stress selection `10/10`.

## Important implementation facts

- `disco_proxy_soul/adapters/gladia_live.py` owns Gladia Live V2 transport.
- `disco_proxy_soul/discord_app/voice_sink.py` owns the bounded synchronous
  receive-thread bridge.
- `disco_proxy_soul/discord_app/voice_session.py` owns session lifecycle,
  pacing, RTP alignment, transcript consumption, and guild serialization.
- `disco_proxy_soul/discord_app/voice_compat.py` contains a guarded compatibility
  layer for the exact pinned receive fork. It fixes jitter readiness, duplicate
  waiter registration, flush/discard amplification, and corrupt Opus recovery.
- `CompanionApp.respond()` remains the only owner of cognition, persona, recall,
  history, and memory effects. Do not create another cognition path.
- `VOICE_GLADIA_STOP_SECONDS` is separate from short Discord cleanup deadlines
  so Gladia can drain final transcripts and end normally.
- `VOICE_MIN_SPEECH_MS` exists but is reserved and unused until Phase 3.

Do not casually remove the compatibility layer or upgrade the Discord voice
dependency. First prove the pinned installed-object compatibility tests against
the replacement version.

## Next slice: Phase 3, text response only

Implement a narrow final-turn coordinator:

1. Buffer nearby Gladia final utterances for the starter.
2. Correlate finals with independent local speech evidence from the PCM timeline.
3. Reject short/noise artifacts conservatively without rejecting valid concise
   human turns.
4. Combine finals separated by endpointing but belonging to one natural turn.
5. Serialize accepted turns per guild and call the existing
   `CompanionApp.respond()` exactly once.
6. Post Naomi's response as Discord text and preserve normal history/memory
   behavior exactly once.

Stop after text cognition works. Do **not** add TTS in the same slice.

Required tests include artifact rejection, the split `live voice` + `connection`
case, valid one-word turns, duplicate-final suppression, concurrent finals,
stop/cancellation during cognition, exactly-once history effects, and no response
from partial transcripts.

## Hard boundaries

- Do not read, stage, or commit `companion-private/`.
- Do not stage or modify `character-voice-bot/`; it is untracked donor material.
- Do not log `GLADIA_API_KEY` or Gladia's tokenized WebSocket URL.
- Do not persist live PCM/WAV files.
- Do not call cognition from Discord's receive thread or Gladia's receiver task.
- Do not implement TTS, playback, barge-in, reconnect, or multi-speaker behavior
  in the Phase 3 text-only slice.
- Preserve public consent notices and starter-only transcription.

## Starting commands

From the repository root, inspect before editing:

```powershell
git status --short
python -m unittest discover -s tests -v
```

Read `VOICE_LIVE_CHAT_PLAN.md`, then inspect the existing app response/history
path before designing the coordinator. Integrate before inventing.
