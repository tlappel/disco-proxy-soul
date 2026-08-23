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
- Holds durable facts, host/partner moments, her journal, and pinned exchanges
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
PARTNER_USER_ID=         # your stable Discord user ID; enables private continuity
ACTIVE_CHANNEL_IDS=      # optional comma-separated additional active channels
```

Then:

```
python -m disco_proxy_soul
```

Private keys and personas can live outside this folder. Set `ENV_FILE` to
that `.env`, plus `PERSONA_DIR` / `DATA_DIR`. Do not commit a private
`companion-private/` tree; it is gitignored if you keep one here.

Do not run two processes with the same Discord token.

### Channels and cross-surface continuity

`WATCH_CHANNEL_ID` remains the outreach destination and an active channel.
`ACTIVE_CHANNEL_IDS` adds other deliberately selected channels where ordinary
human messages can receive a reply without a mention. DMs, mentions, and direct
replies continue to work elsewhere. This is an application policy layered on
top of Discord's normal View Channel and Send Messages permissions.

Set `PARTNER_USER_ID` to the stable Discord ID of the person allowed to carry
private continuity between text, DM, and live voice. New rolling-history turns
store timestamp, guild, channel, surface, stable author, trigger, and source
correlation provenance. The current channel keeps its full rolling history;
the configured partner also receives a bounded, labeled view of recent turns
from other rooms plus relationship-scoped durable recall.

Legacy history and memory records have no trustworthy owner. They still load
and remain available only in their original channel; they are never silently
promoted into cross-surface continuity.

Channel modes are explicit:

| Configuration | Behavior |
|---|---|
| `WATCH_CHANNEL_ID`, `ACTIVE_CHANNEL_IDS` | Private partner rooms; ordinary partner messages receive replies |
| `SOCIAL_CHANNEL_IDS` | Public projection; direct address works, optional local ambient attention may join |
| `ADDRESSED_CHANNEL_IDS` | Public projection; mention, reply, or clear name-address only |
| `IGNORED_CHANNEL_IDS` | No response |
| Unlisted server channel | Addressed behavior |

A channel ID may appear in only one mode. DMs from the configured partner are
private. Public turns never receive private identity, facts, recall, cross-room
recents, relationship docs, presence docs, journal context, journal tools, or
private/legacy channel history. Joined public exchanges retain a bounded local
history but are never compressed into durable guest memory.

Create one or more `.md` files under `docs/public/` for the companion's explicit
shared-room self. Private identity and always-on documents are not assumed safe
for community use. If no public layer exists, addressed turns use only the
minimal defensive guest prompt and ambient attention stays disabled.

Social ambient processing is off by default. With
`SOCIAL_AMBIENT_ENABLED=true`, the host requires loopback Ollama, confirms the
configured local attention model exists, and successfully posts a public
notice before retaining or processing ambient text in that channel. The local
gate sees only bounded human text—never attachments, bot messages, DMs, or
private rooms—and returns `speak`, `wait`, or `ignore`. The RAM-only buffer is
not history or durable memory. When the gate chooses `speak`, a bounded public
excerpt reaches `MODEL_SOCIAL`, which defaults to the canonical primary model.
Notice failure, local-model failure, stale bursts, cooldown, or depleted
discretionary budget all fail closed to silence. Mentions and replies remain
available.

Before enabling a social channel, verify the local model without Discord:

```bash
ollama pull qwen3:1.7b
python -m disco_proxy_soul.adapters.ollama_attention
```

The probe runs nine small `ignore`, `wait`, and `speak` examples and reports
each local decision, confidence, tokens, and model duration. It exits nonzero
when the safe boundary does not match: silence examples must never produce
`speak`, and genuine openings must produce `speak`. Exact `ignore` versus
`wait` labels remain visible diagnostics but currently have the same silent
runtime behavior.

For an unengaged room, a deterministic final guard requires the latest message
to contain a whole-room invitation, a request for another perspective, or the
configured companion name before a local-model `speak` decision is honored.
Once the companion is already engaged, the local model may continue the active
exchange without that extra opening phrase.

The discretionary budget refills gradually. As it drains, the confidence
threshold and cooldown rise; when empty, the room becomes addressed-only rather
than muting the companion entirely. `/social-status` reports local gate calls,
decisions, cancellations, tokens, latency, suppressions, and current balance.
Direct public summons use a separate per-user replenishing allowance. Sustained
mention spam receives an hourglass reaction without invoking cognition; private
partner rooms do not consume that allowance.

Cross-room recents default to twelve messages, 4,000 characters, and two hours.
Tune those ceilings with `CROSS_SURFACE_RECENT_MESSAGES`,
`CROSS_SURFACE_RECENT_CHARS`, and `CROSS_SURFACE_RECENT_MINUTES`.

## Persona packages

A persona is a folder of files, not code. The host reads that folder. A
private companion can live anywhere; point `PERSONA_DIR` at it. Do not put
private identity files in this repo.

What ships in `personas/example/`:

```
personas/example/
  identity.md          # who they are — required
  manifest.json        # names, optional character card, which docs are always-on
  voice.md             # how they write (host default if you omit this)
  facts.seed.json      # durable facts about you (copied into data/ on first run)
  docs/
    shared-context.md  # listed as always-on in the manifest
    public/
      community.md     # explicit public/shared-room projection
```

Older names `persona.md` and `persona.json` still work.

### Adding extras

Drop a `.md` file, then `/reload-docs` (or restart). Two equivalent hooks:

**Folders you create** (the example does not ship these; make them if you want):

| You create | Host treats those files as |
|---|---|
| `docs/always/` | Always in the prompt |
| `docs/presence/` | Loaded only while `/presence` is on |
| `docs/public/` | Used only for public addressed/social Discord turns |
| `docs/` (loose file) | Library. Listed in `/docs`, not sent to the model |

**Or the manifest**, if you would rather leave the file where it is:

```json
{
  "always_on_docs": ["shared-context.md"],
  "layers": {
    "life.md": { "mode": "always_on" },
    "docs/intimate-presence.md": { "mode": "presence" }
  }
}
```

If both are set, the manifest wins.

`/presence` is a toggle for whatever you put in the presence slot. Intimate
register is one use; it does not have to be. `/recall` searches **memories**,
not these files.

| Slot | In the prompt? |
|---|---|
| identity, voice, character card | Every turn |
| always-on docs | Every turn |
| presence docs | Only while `/presence` is on |
| library docs | No — visible in `/docs` until you promote them |

Data files are written to `DATA_DIR` as `{persona_id}_history.json`,
`{persona_id}_memories.json`, `{persona_id}_moments.md` (host + your
highlights), `{persona_id}_journal.md` (hers), and so on. An older
`{persona_id}_journal.md` from before this split is renamed to moments
on first startup.

## Commands

| Command | Does |
|---|---|
| `/status` | Memory window, chunk count, moments, journal, model |
| `/history-status` | Rolling history counts by channel |
| `/memories` | Last stored memory chunks |
| `/moments` | Host and partner highlights |
| `/journal` | Companion's own journal |
| `/saved` | Pinned exchanges |
| `/recall` | Search memories and load them into context |
| `/reflect` | Ask the companion to read facts / journal / moments / memories / docs |
| `/docs` | List or view reference docs |
| `/reload-docs` | Reload the persona package from disk |
| `/presence` | Toggle the presence module (whatever you put in that slot) |
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
| `/voice-chat start` | Start single-speaker live chat through Gladia |
| `/voice-chat status` | Show audio queue, timing, drop, and transcript counters |
| `/voice-chat stop` | Stop Gladia, voice receive, and the Discord connection |

## Experimental live voice chat

This branch contains a production-shaped single-human voice loop. It listens
only to the person who runs `/voice-chat start`; every other speaker is
ignored. Stable finals are independently checked against local speech evidence
and assembled into natural turns. Accepted turns use the normal companion
cognition/history path exactly once. Naomi's canonical response is posted as
Discord text and can optionally be spoken through ElevenLabs. Partial guesses
remain terminal diagnostics and never reach cognition or history. Live mode
saves no raw or generated audio.

Add these values to the private `.env` file:

```env
GLADIA_API_KEY=your-key
VOICE_ENABLED=true
VOICE_ENDPOINTING_SECONDS=0.1
VOICE_QUEUE_SECONDS=2.0
VOICE_GLADIA_STOP_SECONDS=15.0
VOICE_GLADIA_RECONNECT_ATTEMPTS=3
VOICE_GLADIA_RECONNECT_INITIAL_DELAY_SECONDS=0.5
VOICE_GLADIA_RECONNECT_MAX_DELAY_SECONDS=5.0
VOICE_GLADIA_RECONNECT_CONNECT_TIMEOUT_SECONDS=10.0
VOICE_GLADIA_ROTATE_SECONDS=10200
VOICE_MIN_SPEECH_MS=120
VOICE_TURN_DEBOUNCE_SECONDS=1.5
VOICE_TTS_ENABLED=false
ELEVENLABS_API_KEY=your-key
ELEVENLABS_VOICE_ID=your-voice-id
VOICE_BARGE_IN_ENABLED=false
VOICE_BARGE_IN_MIN_SPEECH_MS=160
```

Join a private voice channel and run `/voice-chat start`. The public notice
states that the starter's audio is sent to Gladia, accepted turns reach
companion cognition, and response text is sent to ElevenLabs when speech is
enabled. Use `/voice-chat status` to inspect transport health and
`/voice-chat stop` to close receive, transcription, and playback.

Live mode sends mono PCM16 at 48 kHz on a paced 20 ms clock. It supplies timed
silence for endpointing, bounds both sides of the receive-thread handoff,
reports drops and RTP discontinuities, and never writes a WAV or PCM file.
Fatal receive or Gladia failures stop and release the session automatically;
shutdown remains cleanup-safe if its command task is cancelled.
An unexpectedly closed Gladia WebSocket reconnects to the same temporary URL
within the configured retry budget. A PCM frame whose delivery became
ambiguous is counted and dropped rather than replayed; exhausted retries stop
the session normally through the existing failure path.
Before Gladia's three-hour hard limit, the voice owner ends the current
provider session normally and starts a fresh one. Transcript timestamps from
each replacement are offset onto the existing local audio timeline, so speech
evidence, latency, and playback correlation remain continuous.
`VOICE_MIN_SPEECH_MS` controls the independent local speech-evidence gate;
`VOICE_TURN_DEBOUNCE_SECONDS` controls nearby-final assembly.
When `VOICE_BARGE_IN_ENABLED=true`, locally corroborated speech containing the
companion's name plus `wait`, `stop`, `pause`, or `hold on` intentionally stops
the current playback. Other overlapping speech remains queued behind it.
`VOICE_GLADIA_STOP_SECONDS` gives Gladia Live V2 time to drain final
transcripts and end its session; it is separate from Discord cleanup timing.
If the starter leaves or moves to another voice channel, the live session
stops automatically so timed silence cannot continue consuming transcription.
The process installs an INFO-level console handler only for
`disco_proxy_soul`; terminal transport and receive-compatibility counters stay
visible without enabling Discord debug logs. These summaries are numeric and
credential-redacted.

The bot needs **View Channels**, **Send Messages**, **Read Message History**,
and **Connect** in both the text and voice channels. Enable **Speak** when
`VOICE_TTS_ENABLED=true`. The public start notice discloses Gladia processing,
companion cognition, and ElevenLabs synthesis before the bot connects.

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
