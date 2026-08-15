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

Private keys and personas can live outside this folder. Set `ENV_FILE` to
that `.env`, plus `PERSONA_DIR` / `DATA_DIR`. Do not commit a private
`companion-private/` tree; it is gitignored if you keep one here.

Do not run two processes with the same Discord token.

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
```

Older names `persona.md` and `persona.json` still work.

### Adding extras

Drop a `.md` file, then `/reload-docs` (or restart). Two equivalent hooks:

**Folders you create** (the example does not ship these; make them if you want):

| You create | Host treats those files as |
|---|---|
| `docs/always/` | Always in the prompt |
| `docs/presence/` | Loaded only while `/presence` is on |
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
| `/presence` | Toggle the presence module (whatever you put in that slot) |
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
