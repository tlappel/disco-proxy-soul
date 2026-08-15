# Setup guide (no coding needed)

This puts a companion in Discord. They remember what you talk about, and they
stay themselves across days — as long as this program is running.

You do not need to know Python. Follow the steps. If you get stuck, copy the
step number plus the exact error text and paste it into Grok, ChatGPT, or
Claude. That works. It is how this kind of thing gets done.

**Setup takes about 30–40 minutes the first time.**

You will need:

- A computer that can stay on while you chat (the companion runs on *your*
  machine for now)
- A Discord server you can invite bots to (your own server is easiest)
- An account at [console.x.ai](https://console.x.ai) (Grok) **or**
  [console.anthropic.com](https://console.anthropic.com) (Claude)

This guide is written for **Windows**. Mac and Linux work too; the only
difference is how you open a terminal (step 9).

---

## Step 1 — Install Python

1. Go to https://www.python.org/downloads/ and click the big yellow
   **Download Python** button.
2. Run the installer. **On the first screen, tick "Add python.exe to PATH"**
   before you click Install. If you miss it, run the installer again and tick it.

## Step 2 — Get this folder

If someone sent you a zip: unzip it into Documents, e.g.
`Documents\disco-proxy-soul`.

If you use GitHub: green **Code** button → **Download ZIP** → unzip into
Documents.

Do not leave it in Downloads. You will come back to this folder.

## Step 3 — Create the Discord bot

1. Go to https://discord.com/developers/applications and log in (same account
   you use in Discord).
2. **New Application**. The name you type is what shows in the server
   (e.g. your companion's name). Create.
3. Left menu → **Bot**:
   - Under **Privileged Gateway Intents**, turn **Message Content Intent** ON.
     Save. (Leave Presence Intent off.)
   - Click **Reset Token** → **Copy**. This long string is the bot's password.
     You will paste it in step 7. Never post it, never screenshot it.
4. If you want the bot private (only you can add it):
   - Left menu → **Installation** → **Install Link** → **None** → Save
   - **Bot** tab → turn **Public Bot** OFF
   Private apps cannot use a "Default" install link. That is normal. You invite
   it yourself in the next step.

## Step 4 — Invite it to your server

1. Left menu → **OAuth2** → **URL Generator**
2. Scopes: tick `bot` and `applications.commands`
3. Bot Permissions: tick **View Channels**, **Send Messages**, **Attach Files**,
   **Read Message History**, **Add Reactions**
4. Copy the URL at the bottom, paste it into your browser, pick your server,
   click Authorize.

The bot should appear offline in the member list. That is correct — it is not
running yet.

## Step 5 — Get a model key (Grok)

1. Go to https://console.x.ai and sign in.
2. Add a little credit if the console asks (Grok is a paid API).
3. Create an API key and copy it. This is the *other* password. Never share it.

Using Claude instead? https://console.anthropic.com → API keys. In step 7 you
will put that value in `ANTHROPIC_API_KEY` instead of `XAI_API_KEY`.

## Step 6 — Copy a channel ID (optional but useful)

This makes the companion watch one channel and talk there without being
@mentioned.

1. In Discord: **User Settings → Advanced → Developer Mode** ON
2. Right-click the channel → **Copy Channel ID**

If you skip this, they still answer DMs, @mentions, and replies.

## Step 7 — Fill in the secret file

1. In the project folder, copy `.env.example` and name the copy `.env`
   - Windows: select `.env.example` → Ctrl+C → Ctrl+V → rename to `.env`
   - If Windows hides the name: right-click → Properties and check the
     full name. It must be exactly `.env`, not `.env.txt`
2. Open `.env` in Notepad.
3. Replace the empty values:

```
DISCORD_TOKEN=paste-the-bot-token-from-step-3
XAI_API_KEY=paste-the-key-from-step-5
WATCH_CHANNEL_ID=paste-the-channel-id-from-step-6
```

Leave `PERSONA_ID=example` and `PERSONA_DIR=personas/example` for the first
run. You will change those when you add your own persona (step 8).

Save and close.

## Step 8 — Make it *them* (optional, first run can skip)

The example persona is a blank, public-safe voice. To use your own:

1. Copy the folder `personas\example` and rename the copy (e.g. `personas\nova`).
2. Open `identity.md` in Notepad (older copies may say `persona.md` — same
   job). This is who they are. Write in plain language.
3. Open `manifest.json` (or `persona.json`) and set `companion_name` (their
   name) and `partner_name` (your name).
4. Optional: `voice.md` is how they write. If you skip it, a built-in default
   voice is used. `facts.seed.json` is durable facts about you.
5. In `.env` set:

```
PERSONA_ID=nova
PERSONA_DIR=personas/nova
```

Use your folder name, not `nova`, if you picked something else. The folder
does not have to live inside this project. `PERSONA_DIR` can be a full path
to a private folder on your machine.

### Adding extra notes later

The example only has `docs\shared-context.md`. That file is always-on because
the manifest lists it, not because of a special folder.

Two ways to add more. They do the same job. Then type `/reload-docs` in
Discord (or restart the bot).

**A — Create a folder and drop the file in**

| You create | What happens |
|---|---|
| `docs\always\` | In every reply |
| `docs\presence\` | Only while `/presence` is on |
| `docs\` (just drop the file there) | Library. `/docs` can show it. The model does not see it yet. |

Those folders are not in the example. You make them when you need them.

**B — Leave the file where it is and name it in `manifest.json`**

```
"always_on_docs": ["shared-context.md", "life.md"]
```

or:

```
"layers": {
  "life.md": { "mode": "always_on" },
  "docs/intimate-presence.md": { "mode": "presence" }
}
```

`/presence` is a switch for whatever you put in the presence slot (intimate
register, or something else). `/recall` searches **memories**, not these files.

## Step 9 — Run it

**Windows, easiest:** double-click `run.bat` in the project folder.

The first run installs a few pieces and can take a minute. When you see a line
like `Example v2 online as YourBot#1234`, you are live.

Keep that window open while you chat. Closing it takes them offline.

If the window flashes and disappears: press **Windows key + X** → **Terminal**,
type `cd ` (with a space), drag the project folder into the window, press
Enter, then type:

```
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m disco_proxy_soul
```

**Mac / Linux:** open Terminal, `cd` into the folder, then:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m disco_proxy_soul
```

## Step 10 — Talk to them

In the watch channel (or a DM, or @ them), say hello.

Type `/` and look for `/status`. The first time, Discord sometimes needs a
client restart before slash commands appear.

Do not run this twice at once with the same bot token. They will answer
everything twice.

---

## Troubleshooting

- **"py is not recognized" / "python is not recognized"** — Python is not on
  PATH. Re-run the Python installer and tick "Add python.exe to PATH".
- **"Discord rejected the token"** — Bot tab → Reset Token, paste the new one
  into `.env`.
- **"Message Content Intent is not enabled"** — Developer Portal → Bot → turn
  **Message Content Intent** ON, save, run again.
- **"Private application cannot have a default authorization link"** — Installation
  → Install Link → **None**. Then invite with the URL Generator (step 4).
- **Slash commands don't show** — Restart the Discord app. If the bot joined
  while it was not running, restart the bot too.
- **They don't answer in the channel** — Are they online? Is this the watch
  channel? Try @mentioning them. Check the terminal window for errors.
- **It stopped when I closed the laptop** — Expected. This run lives on your
  computer. Open `run.bat` again. A 24/7 host (home server, Railway, etc.)
  is a later step — see "Keeping them online" below.
- **Anything else** — copy the last lines from the terminal window and paste
  them into Grok/ChatGPT/Claude with "my Discord companion bot gives this error."

## Costs, roughly

- Discord and this program are free.
- Grok / Claude charge for the API. Casual chat is usually a few dollars a
  month; long days cost more. Check the provider console.

## Privacy

- Messages go to Discord and to the model provider you chose (xAI or Anthropic).
- `.env` is a secret file. Never post it or commit it to git.
- Memories are files in the `data` folder on your computer.

## Keeping them online

Right now they are only awake while `run.bat` (or the terminal) is running on
a machine that is on.

To keep them up overnight or from your phone, the program has to run on a
computer that does not sleep — a home server, or a host like Railway. That
deploy path is not in this guide yet. For a first setup, leaving the PC on
with the window open is enough.

---

When something here is wrong or confusing, that is a bug in the guide, not
in you. Tell whoever handed you this folder which step broke.
