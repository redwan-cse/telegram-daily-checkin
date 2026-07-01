# Telegram Daily Check-In

A lightweight Python/Telethon helper for sending a daily command from one Telegram MTProto account to a configurable list of Telegram bots.

> Use this only for accounts and bots you are authorized to interact with. The pacing/jitter features are intended to prevent accidental bursts and keep operations conservative, not to evade platform rules.

## Features

- Persistent Telethon session file, default: `primary_account.session`
- First run prompts for phone number, Telegram code, and 2FA password if required
- Subsequent runs reuse the session silently
- Configurable bot list and command, default command: `/checkin`
- Optional native SOCKS5 proxy via PySocks/Telethon
- Random initial delay for cron-triggered runs, default 1-15 minutes
- Random delay between bots, default 20-75 seconds
- Per-bot exception handling so one failed bot does not stop the queue
- Timestamped logging with Python's standard `logging` library

## Required pip packages

```bash
pip install -r requirements.txt
```

Packages:

- `telethon`
- `PySocks`
- `python-dotenv`

## Telegram API credentials

Create an app at:

```text
https://my.telegram.org/apps
```

Then copy:

- `api_id` -> `TELEGRAM_API_ID`
- `api_hash` -> `TELEGRAM_API_HASH`

## Installation

```bash
git clone https://github.com/redwan-cse/telegram-daily-checkin.git
cd telegram-daily-checkin
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace_with_your_api_hash
SESSION_NAME=primary_account
TARGET_BOTS=@BotA,@BotB
CHECKIN_COMMAND=/checkin

INITIAL_DELAY_MIN_SECONDS=60
INITIAL_DELAY_MAX_SECONDS=900
BETWEEN_BOT_MIN_SECONDS=20
BETWEEN_BOT_MAX_SECONDS=75

PROXY_ENABLED=false
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_USERNAME=
PROXY_PASSWORD=
```

## SOCKS5 proxy placeholders

Enable only when using a trusted proxy:

```env
PROXY_ENABLED=true
PROXY_HOST=proxy.example.com
PROXY_PORT=1080
PROXY_USERNAME=your_proxy_user
PROXY_PASSWORD=your_proxy_password
```

The script passes this into Telethon's native `proxy=` argument as a PySocks SOCKS5 tuple with remote DNS resolution enabled.

## First run

Run interactively once to create the session:

```bash
. .venv/bin/activate
python telegram_daily_checkin.py
```

If `primary_account.session` does not exist or is not authorized, the script prompts:

- Telegram phone number
- SMS/Telegram login code
- 2FA password, if enabled

After successful login, Telethon saves the session file. Later runs reuse it silently.

Session files contain account authorization material. Keep them private:

```bash
chmod 600 primary_account.session .env
```

## Daily crontab examples

The script already has an internal 1-15 minute randomized startup delay. A standard daily cron entry can therefore run at a fixed minute while the script spreads actual Telegram traffic:

```cron
# Run daily at 09:00 server time, then script waits a random 1-15 minutes.
0 9 * * * cd /path/to/telegram-daily-checkin && . .venv/bin/activate && python telegram_daily_checkin.py >> checkin.log 2>&1
```

If you also want cron-level randomness, run it every day at a broad window with shell sleep. Example: start from 09:00 and add 0-59 minutes of shell delay, then the script adds its own 1-15 minute delay:

```cron
0 9 * * * cd /path/to/telegram-daily-checkin && sleep $((RANDOM % 3600)) && . .venv/bin/activate && python telegram_daily_checkin.py >> checkin.log 2>&1
```

Alternative: choose several possible cron minutes and rely on the script's internal jitter:

```cron
7 9 * * * cd /path/to/telegram-daily-checkin && . .venv/bin/activate && python telegram_daily_checkin.py >> checkin.log 2>&1
```

## Exit codes

- `0`: all bot interactions succeeded
- `1`: one or more bot interactions failed, or fatal configuration/runtime error
- `130`: interrupted by user

## Operational notes

- Keep bot count small and delays conservative.
- Do not run overlapping cron jobs for the same session.
- Avoid committing `.env` or `*.session` files.
- If Telegram returns `FloodWaitError`, the script logs it and continues, but you should reduce frequency or bot count.
