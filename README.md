# disco-proxy-soul

A Discord host for a flexible, persistent, memory-backed companion.

Discord is the room. The persona is a folder of files. The model is whatever
provider you configure (Grok, Claude, or any OpenAI-compatible API).

This repository is the reusable host plus a public-safe example persona.
Private companions live in their own persona directory, outside this repo.

**Never set this up before?** Start at [SETUP.md](SETUP.md). It assumes no
coding background. The rest of this file is the short version.

## What it does

- Talks in a watch channel, DMs, mentions, and replies
- Keeps a rolling conversation window and compresses older turns into memories
- Recalls relevant memories after a silence gap (or via `/recall`)
- Holds durable facts, a journal, and pinned exchanges
- Optional outreach: the companion can speak first after quiet-hour and
  budget checks, if a cheap model judges there is something worth saying
- `/model` lists whatever providers you have keys for
- Experimental single-speaker Discord voice can stream live PCM to Gladia and
  post stable transcripts without saving raw audio

## Requirements

- Python 3.11+
- A Discord application (bot token, Message Content Intent)
- An API key for at least one provider (`XAI_API_KEY`, `ANTHROPIC_API_KEY`,
  or `OPENAI_API_KEY`)

## Run

```
python -m venv .venv
# Windows:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # or: cp .env.example .env
```

Fill in `.env`:

```
DISCORD_TOKEN=...
XAI_API_KEY=...          # or ANTHROPIC_API_KEY / OPENAI_API_KEY
PERSONA_ID=example
PERSONA_DIR=personas/example
WATCH_CHANNEL_ID=        # optional; without it, DMs / mentions / replies only
```

Then:

```
python -m disco_proxy_soul
```

Do not run two processes with the same Discord token.

## Persona packages

A persona is a directory, not code.

```
personas/example/
  persona.md           # identity — required
  persona.json         # companion_name, partner_name, always-on docs
  voice.md             # optional writing voice
  facts.seed.json      # seed facts (copied into data/ on first run)
  memory_policy.json   # journal threshold, what to remember
  docs/                # optional reference markdown
```

Point `PERSONA_DIR` at any folder, including one that never lives in git.

Data files are written to `DATA_DIR` as `{persona_id}_history.json`,
`{persona_id}_memories.json`, and so on.

## Commands

| Command | Does |
|---|---|
| `/status` | Memory window, chunk count, journal, model |
| `/history-status` | Rolling history counts by channel |
| `/memories` | Last stored memory chunks |
| `/journal` | Recent journal entries |
| `/saved` | Pinned exchanges |
| `/recall` | Search memories and load them into context |
| `/reflect` | Ask the companion to read facts / journal / memories / docs |
| `/docs` | List or view reference docs |
| `/reload-docs` | Reload the persona package from disk |
| `/presence` | Toggle extra (non-always-on) docs into context |
| `/moment` | Save a moment in your words |
| `/prune` | Compress the current window into a session brief |
| `/redist` | Re-distill the memory archive |
| `/export` | Download data files |
| `/clear` | Clear recent history (memories stay) |
| `/model` | Switch the response model |
| `/reach` | Outreach status |
| `/reach-reset` | Reset outreach counters |
| `/voice-record` | Join your voice channel and record decoded PCM per speaker |
| `/voice-stop` | Stop recording, disconnect, and report local WAV paths |
| `/voice-chat start` | Stream only the starter's live voice to Gladia |
| `/voice-chat status` | Show audio queue, timing, drop, and transcript counters |
| `/voice-chat stop` | Stop Gladia, voice receive, and the Discord connection |

## Experimental live voice transcription

This branch contains the first production-shaped slice of voice chat. It
transcribes only the person who runs `/voice-chat start`; every other speaker
is ignored. Stable final transcripts are posted to the command channel, while
partial guesses are terminal diagnostics only. It does **not** invoke the
companion, write conversation history, generate speech, or save raw audio yet.

Add these values to the private `.env` file:

```env
GLADIA_API_KEY=your-key
VOICE_ENABLED=true
VOICE_ENDPOINTING_SECONDS=0.1
VOICE_QUEUE_SECONDS=2.0
VOICE_GLADIA_STOP_SECONDS=15.0
VOICE_MIN_SPEECH_MS=120
```

Join a private voice channel and run `/voice-chat start`. The public notice
states that the starter's audio is sent to Gladia and that final transcripts
appear in the channel. Use `/voice-chat status` to inspect transport health and
`/voice-chat stop` to close the receiver, Gladia session, and connection.

Live mode sends mono PCM16 at 48 kHz on a paced 20 ms clock. It supplies timed
silence for endpointing, bounds both sides of the receive-thread handoff,
reports drops and RTP discontinuities, and never writes a WAV or PCM file.
Fatal receive or Gladia failures stop and release the session automatically;
shutdown remains cleanup-safe if its command task is cancelled.
`VOICE_MIN_SPEECH_MS` is accepted now but is reserved for the Phase 3
independent speech-evidence gate.
`VOICE_GLADIA_STOP_SECONDS` gives Gladia Live V2 time to drain final
transcripts and end its session; it is separate from Discord cleanup timing.
If the starter leaves or moves to another voice channel, the live session
stops automatically so timed silence cannot continue consuming transcription.
The process installs an INFO-level console handler only for
`disco_proxy_soul`; terminal transport and receive-compatibility counters stay
visible without enabling Discord debug logs. These summaries are numeric and
credential-redacted.

The bot needs **View Channels**, **Send Messages**, **Read Message History**,
and **Connect** in both the text and voice channels. It does not need **Speak**
until outbound TTS is implemented.

### Diagnostic recording and replay

The `feature/voice-live-chat` branch first proves Discord/DAVE audio receive
with an optional local recording path. Diagnostic recording cannot run at the
same time as live transcription.

1. Reinstall this branch's requirements: `pip install -r requirements.txt`.
2. Join a server voice channel and run `/voice-record`.
3. Say: "Atlas voice test. Travis and Lila, one two three. Peter picked a
   purple packet. Pause. This is the final sentence."
4. Run `/voice-stop`.
5. Listen to the per-speaker WAV files under `data/voice-captures/`.

After confirming the recording sounds right, replay it through Gladia Live V2:

```powershell
python -m disco_proxy_soul.adapters.gladia_live "data\voice-captures\<capture>\<speaker>.wav" --env-file ".env"
```

The selected environment file must contain `GLADIA_API_KEY`. This command
sends the recording to Gladia for transcription; get the recorded speaker's
permission before running it. Discord's duplicated stereo capture is downmixed
to mono automatically so Gladia does not transcribe each channel separately.

The diagnostic start message is public so everyone in the channel knows
recording has begun. Files remain local and `data/` is gitignored. The pinned
voice-receive dependency includes an unreleased minimal DAVE decryption patch.
Its exact commit is also shape-guarded by `discord_app/voice_compat.py` for
jitter readiness, unique waiter registration, non-flushing gap recovery, and
per-decoder Opus reset. Remove that compatibility patch only after an upstream
pin provides all four behaviors and the installed-object tests pass unchanged.

## Reactions

On a companion message:

| React | Does |
|---|---|
| 📌 | Pin the exchange and store it as a max-significance memory |
| ❌ | Remove that exchange from recent history |
| 👍 | Expand on that message |
| anything else | Respond to the reaction in character |

## Providers

Set the keys you have. `/model` only lists configured providers.

```
MODEL_PROVIDER=xai
MODEL_PRIMARY=grok-4.6
MODEL_CHEAP=grok-4.3
```

Or `anthropic:claude-opus-4-6`, `openai:gpt-4.1`, etc.

## Test

```
python -m unittest discover -s tests -v
```

## License

AGPL-3.0. See `LICENSE`.
