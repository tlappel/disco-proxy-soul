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
