# Telegram Daily Check-In

A lightweight Python/Telethon automation helper for sending daily actions from one or more Telegram MTProto accounts to multiple Telegram bots.

> Use this only for Telegram accounts and bots you are authorized to operate. Timing jitter is conservative operational pacing to avoid accidental bursts.

## Answer to the design question

`CHECKIN_COMMAND` is **not** treated as one global command. Each bot has its own action list in `config.yaml`.

Supported action types:

| Action type | Use when | Example |
|---|---|---|
| `send_command` | Bot expects slash command | `/daily` |
| `send_message` | Bot expects plain text | `📅 Daily Check-in` |
| `click_button` | Bot sends an inline/reply keyboard button in chat | click button text `📅 Daily Check-in` |
| `manual_ui_required` | Bot truly needs Telegram Desktop/mobile app UI clicking | report for separate GUI runner |

Important distinction:

- **Telethon can send messages/commands and click bot chat buttons** via MTProto.
- **Docker cannot click the native Telegram app UI**. True desktop/mobile UI workflows need a separate host GUI runner, e.g. Telegram Desktop + computer-use/desktop automation. This container marks those bots as `manual_ui_required` so the daily report does not silently pretend they succeeded.

## Per-bot command/action examples

```yaml
accounts:
  - id: primary_account
    phone: "+8801XXXXXXXXX"
    api_id: ${TELEGRAM_API_ID}
    api_hash: "${TELEGRAM_API_HASH}"
    bots:
      - username: "@BotDailyCommand"
        actions:
          - type: send_command
            value: "/daily"

      - username: "@BotTextCheckin"
        actions:
          - type: send_message
            value: "📅 Daily Check-in"

      - username: "@BotButtonCheckin"
        actions:
          - type: send_command
            value: "/start"
          - type: click_button
            value: "📅 Daily Check-in"
            wait_seconds: 5

      - username: "@BotNeedsNativeApp"
        actions:
          - type: manual_ui_required
            value: "Needs Telegram Desktop/mobile UI click"
```

## Session storage

Sessions are stored per account ID in encrypted SQLite, not as raw `.session` files:

```text
./data/sessions.sqlite3
```

Encryption uses `cryptography.Fernet` with:

```env
SESSION_ENCRYPTION_KEY=...
```

Generate a key:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Back up both the DB and the encryption key. One without the other is not enough.

## Features

- Per-account, per-bot action configuration
- First-run interactive Telethon login per account
- Subsequent runs silently reuse encrypted sessions
- Encrypted SQLite session storage
- Optional native SOCKS5 proxy via Telethon/PySocks
- Random initial delay, default 1-15 minutes
- Random delay between bots, default 20-75 seconds
- Random delay between accounts, default 30-120 seconds
- Per-bot failure isolation
- Persistent file logging
- Optional Telegram execution report to a Hermes/OpenClaw Telegram bot/chat
- Dockerfile and Docker Compose deployment

## Required pip packages

```bash
pip install -r requirements.txt
```

Packages:

- `telethon`
- `PySocks`
- `python-dotenv`
- `PyYAML`
- `cryptography`

## Setup

```bash
git clone https://github.com/redwan-cse/telegram-daily-checkin.git
cd telegram-daily-checkin
cp .env.example .env
cp config.example.yaml config.yaml
chmod 600 .env config.yaml
mkdir -p data logs
chmod 700 data logs
```

Create Telegram API credentials at:

```text
https://my.telegram.org/apps
```

Edit `.env`:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace_with_your_api_hash
SESSION_ENCRYPTION_KEY=replace_with_generated_fernet_key

REPORT_ENABLED=true
REPORT_BOT_TOKEN=your_hermes_or_openclaw_bot_token
REPORT_CHAT_ID=your_telegram_chat_id
REPORT_LABEL=telegram-daily-checkin
```

Edit `config.yaml` for accounts and per-bot actions.

## Docker deployment

Build:

```bash
docker compose build
```

First interactive run for Telegram login:

```bash
docker compose run --rm -it telegram-daily-checkin
```

If an account has no encrypted session, the script prompts for:

- phone number if not configured;
- Telegram SMS/app login code;
- Telegram 2FA password if enabled.

After login, the encrypted session is saved in:

```text
./data/sessions.sqlite3
```

Normal one-shot execution:

```bash
docker compose run --rm telegram-daily-checkin
```

Only the first login run needs `-it`; scheduled one-shot runs do not.

The service is intentionally one-shot. Cron/systemd should trigger it daily.

## Persistent volumes

`docker-compose.yml` mounts:

| Host path | Container path | Purpose |
|---|---|---|
| `./data` | `/app/data` | encrypted SQLite sessions and execution history |
| `./logs` | `/app/logs` | persistent logs |
| `./config.yaml` | `/app/config.yaml` | per-account/per-bot action config |

## Telegram execution reports

Enable in `.env`:

```env
REPORT_ENABLED=true
REPORT_BOT_TOKEN=123456:telegram_bot_token
REPORT_CHAT_ID=123456789
REPORT_LABEL=telegram-daily-checkin
```

Example report:

```text
✅ telegram-daily-checkin execution report
Started: 2026-07-01T00:00:00+00:00
Finished: 2026-07-01T00:03:22+00:00
Success: 2
Failed: 1

✅ primary_account -> @BotA `send_command:/daily`: send_command sent
✅ primary_account -> @BotB `send_message:📅 Daily Check-in`: send_message sent
❌ primary_account -> @BotNeedsNativeApp `manual_ui_required:Needs Telegram Desktop/mobile UI click`: error: RuntimeError: manual_ui_required...
```

Use your Hermes/OpenClaw Telegram bot token/chat ID to receive daily success/failure reports.

## Cron examples

The script already includes an internal 1-15 minute randomized initial delay.

Daily Docker run:

```cron
0 9 * * * cd /path/to/telegram-daily-checkin && docker compose run --rm telegram-daily-checkin >> logs/cron.log 2>&1
```

Cron-level extra randomness plus script-level jitter:

```cron
0 9 * * * cd /path/to/telegram-daily-checkin && sleep $((RANDOM % 3600)) && docker compose run --rm telegram-daily-checkin >> logs/cron.log 2>&1
```

Host Python alternative:

```cron
0 9 * * * cd /path/to/telegram-daily-checkin && . .venv/bin/activate && python telegram_daily_checkin.py >> logs/cron.log 2>&1
```

## SOCKS5 proxy

Set in `.env` or `config.yaml`:

```yaml
proxy:
  enabled: true
  host: proxy.example.com
  port: 1080
  username: your_proxy_user
  password: your_proxy_password
```

The script passes this to Telethon's native `proxy=` argument as a PySocks SOCKS5 tuple with remote DNS enabled.

## Hermes/OpenClaw / Manifest routing note

For this repository's maintenance and future automation work, Redwan's current Hermes setup has been hitting model-provider rate limits when most work is routed through the main Codex GPT-5.5 API. To reduce main API usage, future implementation, review, and deployment-assistance tasks for this project should prefer the custom API credits routed through Manifest/OpenClaw where appropriate, reserving the main API for final review or sensitive decisions.

This note is about the AI-assistant maintenance workflow. The Telegram check-in container itself does not call Manifest or any LLM API.

## Security notes

- Never commit `.env`, `config.yaml`, `data/`, `logs/`, or raw `*.session` files.
- Back up both `data/sessions.sqlite3` and `SESSION_ENCRYPTION_KEY`.
- Keep account counts and bot counts conservative.
- Do not overlap cron runs for the same accounts.
- If Telegram returns `FloodWaitError`, reduce run frequency, bot count, or command volume.

## Exit codes

- `0`: all account/bot interactions succeeded
- `1`: one or more account/bot interactions failed or configuration/runtime error
- `130`: interrupted by user
