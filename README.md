# MayaBot

A fictional, warm, witty Telegram companion. Maya speaks Hindi, Hinglish and English, remembers recent conversation, learns your nickname and language, and works in private chats, groups and supergroups.

Maya is a **virtual character**. She never claims to be a real person. If someone asks directly whether she is real, she says she is a virtual character made for entertainment.

## Features

- Private chats, groups, supergroups (replies when @mentioned, replied to, or called "Maya")
- Remembers conversation history per chat, trimmed to a configurable window
- Remembers nickname ("mera naam Raj hai", "call me Ria", or `/nickname`)
- Remembers preferred language (auto-detect, or `/lang hi|hinglish|en|auto`)
- Realistic typing indicator and variable reply delay
- Long replies split into multiple Telegram messages
- OpenRouter with configurable model, retries with backoff, graceful fallbacks
- Per-user rate limiting, input sanitizing, prompt-injection heuristics
- Async throughout, structured logging, secrets kept out of logs

## Commands

| Command | Purpose |
|---|---|
| `/start` | Intro |
| `/help` | Command list |
| `/reset` | Clear this chat's memory |
| `/stats` | Your stats (admins also see global stats) |
| `/ping` | Latency check |
| `/nickname <name>` | Set what Maya calls you |
| `/lang <hi\|hinglish\|en\|auto>` | Set reply language |

## Setup

### 1. Create the Telegram bot

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`, follow the prompts, and copy the token.
3. For group use, send `/setprivacy` → choose your bot → **Disable**. Without this, Maya cannot see ordinary group messages, so she loses group context.
4. Optional: `/setdescription` and `/setuserpic` to make her look nice.

### 2. Get an OpenRouter key

Create a key at https://openrouter.ai/keys. Pick any chat model slug from https://openrouter.ai/models and set it as `OPENROUTER_MODEL`.

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY`. Keep `.env` private and never commit it.

### 4. Install and run on Linux

```bash
cd MayaBot
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python bot.py
```

Run it as a service so it survives reboots (`/etc/systemd/system/mayabot.service`):

```ini
[Unit]
Description=MayaBot Telegram companion
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/MayaBot
ExecStart=/home/YOUR_USER/MayaBot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mayabot
sudo journalctl -u mayabot -f
```

### 5. Install and run on Termux (Android)

```bash
pkg update && pkg upgrade -y
pkg install -y python git tmux
termux-setup-storage            # optional: access shared storage

cd ~
git clone <your-repo-or-copy-the-folder> MayaBot
cd MayaBot
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
nano .env                        # add your tokens, then Ctrl+O, Enter, Ctrl+X
```

Keep it running while the screen is off:

```bash
termux-wake-lock                 # stop Android from sleeping Termux
tmux new -s maya
source .venv/bin/activate
python bot.py
# detach with Ctrl+B then D; reattach later with: tmux attach -t maya
```

Background logging without tmux: `nohup python bot.py > mayabot.log 2>&1 &`

If `pip install` fails on a native dependency, run `pkg install -y clang libffi openssl` and retry.

### 6. Test it

- Private chat: send `/start`, then "hi Maya, kaise ho?"
- Group: @mention the bot, reply to one of its messages, or start with "Maya, ..."
- Check `/ping` and `/stats`

## Project layout

| File | Responsibility |
|---|---|
| `bot.py` | Entry point, wiring, lifecycle |
| `config.py` | `.env` loading and validation |
| `ai.py` | OpenRouter client, retries, error mapping |
| `database.py` | SQLite schema and queries |
| `memory.py` | Persona prompt, history window, nickname/language memory |
| `handlers.py` | Telegram commands and message flow |
| `utils.py` | Sanitizing, language detection, splitting, rate limiting, logging |

## Tuning

- `MAX_HISTORY_MESSAGES`: memory window per chat (default 40). Lower it to save space and tokens.
- `OPENROUTER_MAX_TOKENS`: caps reply length (default 500).
- `TYPING_MIN_DELAY` / `TYPING_MAX_DELAY`: how long Maya "types" before answering.
- `RATE_LIMIT_PER_MINUTE`: per-user cap to protect your OpenRouter budget.
- Persona: edit `PERSONA` in `memory.py` to change her personality.

## Security notes

- API keys live only in `.env` and are sent only in the Authorization header.
- httpx logging is kept at WARNING so the bot token never appears in logs.
- All SQL uses parameters. The only dynamic identifier (preference column) is checked against a whitelist.
- Inputs are capped in length and stripped of control and invisible characters.
- Injection heuristics add a note to the system prompt. They are not a security boundary, so keep the persona rules and content limits as the primary defense.
- Use `ADMIN_IDS` for anyone who should see global stats.

## Troubleshooting

- **Bot silent in groups:** check `/setprivacy` is Disabled, and that you @mention it or reply to it.
- **"Configuration error":** a required variable in `.env` is missing or malformed.
- **Frequent "hang" replies:** check the OpenRouter model name and your credits, and look at the logs for HTTP 401, 402, or 404.
- **Termux stops when screen locks:** run `termux-wake-lock` and use tmux.