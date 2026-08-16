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
   **Read Message History**, **Add Reactions**, and **Connect**
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

To enable the experimental single-speaker live voice commands, also create a
Gladia API key and set:

```env
GLADIA_API_KEY=paste-the-gladia-key
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
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
VOICE_BARGE_IN_ENABLED=false
VOICE_BARGE_IN_MIN_SPEECH_MS=160
```

The key is used server-side and must remain in the private `.env` file.
The Gladia stop deadline allows final transcripts to drain during shutdown.
The reconnect settings bound same-session recovery after an unexpected socket
closure. Ambiguous audio is never replayed.
The rotation threshold starts a fresh Gladia session at two hours fifty
minutes, before the provider's three-hour limit.
The session also stops automatically if its starter leaves or changes voice channels.

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

`/moment` saves a highlight in your words. `/moments` shows those plus
ones the host scored. `/journal` is theirs — they write it; it can be empty.

Do not run this twice at once with the same bot token. They will answer
everything twice.

### Optional — test live voice chat

The bot needs **Connect** permission for the private voice channel, plus **View
Channels**, **Send Messages**, and **Read Message History** in the text channel
where transcripts should appear. It also needs **Speak** when outbound speech
is enabled.

1. Add `GLADIA_API_KEY` and `VOICE_ENABLED=true` to the private `.env` file.
   To hear replies, also set `VOICE_TTS_ENABLED=true`, `ELEVENLABS_API_KEY`,
   and `ELEVENLABS_VOICE_ID`. To enable intentional interruption, set
   `VOICE_BARGE_IN_ENABLED=true`; say the companion's name followed by `wait`,
   `stop`, `pause`, or `hold on` while they are speaking.
2. Restart the bot, then join a private voice channel.
3. Run `/voice-chat start`. A public notice explains the Gladia transcription,
   companion cognition, and optional ElevenLabs synthesis. No raw or generated
   audio is saved.
4. Speak normally. Partial guesses stay in the bot terminal. Accepted final
   turns reach companion cognition/history exactly once; the response appears
   as Discord text and, when enabled, is spoken in the voice channel.
5. Run `/voice-chat status` to inspect queue drops and timing, then
   `/voice-chat stop` to close the session and disconnect.

Only the command starter is transcribed. Diagnostic recording and live
transcription cannot run together.

### Optional — make a diagnostic recording

The `feature/voice-live-chat` branch can record decoded Discord audio before
any speech-to-text or AI response is connected.

1. Re-run `pip install -r requirements.txt` after switching to this branch.
2. Join a server voice channel.
3. Run `/voice-record`. The bot announces publicly that recording has begun.
4. Say: "Atlas voice test. Travis and Lila, one two three. Peter picked a
   purple packet. Pause. This is the final sentence."
5. Run `/voice-stop`.
6. Open the reported folder under `data\voice-captures` and play your WAV.

Listen for normal speed and pitch, intelligible words, clean pauses, and no
static, robotic doubling, clicks, or missing syllables. Each human speaker
gets a separate WAV. The files remain on this computer and `data` is ignored
by Git.

To test the recording with Gladia Live V2, add `GLADIA_API_KEY` to your private
environment file and run:

```powershell
python -m disco_proxy_soul.adapters.gladia_live "data\voice-captures\<capture>\<speaker>.wav" --env-file ".env"
```

This sends that speaker's recording to Gladia, so obtain their permission
first. The replay runs at real-time speed, prints partial and final transcripts,
and automatically converts Discord's duplicated stereo audio to mono.

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
