#!/usr/bin/env python3
"""
Telegram daily bot check-in helper for a single MTProto account.

This script uses Telethon with a persistent named session file. On first run it
prompts in the terminal for phone/code/2FA if needed; later runs reuse the
session silently.

Use only with accounts and bots you are authorized to interact with, and keep
request pacing conservative to avoid accidental bursts or operational mistakes.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    load_dotenv = None  # type: ignore[assignment]

try:
    import socks  # PySocks
except ImportError:  # pragma: no cover - handled by proxy validation
    socks = None  # type: ignore[assignment]


LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    target_bots: tuple[str, ...]
    command: str
    initial_delay_min_seconds: int
    initial_delay_max_seconds: int
    between_bot_min_seconds: int
    between_bot_max_seconds: int
    proxy_enabled: bool
    proxy_host: str
    proxy_port: int
    proxy_username: str | None
    proxy_password: str | None


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)


def _read_env_file() -> None:
    if load_dotenv is not None:
        load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _split_bots(raw: str) -> tuple[str, ...]:
    bots = tuple(bot.strip() for bot in raw.split(",") if bot.strip())
    if not bots:
        raise ValueError("TARGET_BOTS must contain at least one bot username")
    return bots


def load_settings() -> Settings:
    _read_env_file()

    api_id = _env_int("TELEGRAM_API_ID", 0)
    api_hash = _env("TELEGRAM_API_HASH")
    if api_id <= 0 or not api_hash:
        raise ValueError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH. Get them from https://my.telegram.org/apps"
        )

    settings = Settings(
        api_id=api_id,
        api_hash=api_hash,
        session_name=_env("SESSION_NAME", "primary_account"),
        target_bots=_split_bots(_env("TARGET_BOTS", "@BotA,@BotB")),
        command=_env("CHECKIN_COMMAND", "/checkin"),
        initial_delay_min_seconds=_env_int("INITIAL_DELAY_MIN_SECONDS", 60),
        initial_delay_max_seconds=_env_int("INITIAL_DELAY_MAX_SECONDS", 900),
        between_bot_min_seconds=_env_int("BETWEEN_BOT_MIN_SECONDS", 20),
        between_bot_max_seconds=_env_int("BETWEEN_BOT_MAX_SECONDS", 75),
        proxy_enabled=_env_bool("PROXY_ENABLED", False),
        proxy_host=_env("PROXY_HOST", "127.0.0.1"),
        proxy_port=_env_int("PROXY_PORT", 1080),
        proxy_username=_env("PROXY_USERNAME") or None,
        proxy_password=_env("PROXY_PASSWORD") or None,
    )

    _validate_delay_window(
        "INITIAL_DELAY", settings.initial_delay_min_seconds, settings.initial_delay_max_seconds
    )
    _validate_delay_window(
        "BETWEEN_BOT_DELAY", settings.between_bot_min_seconds, settings.between_bot_max_seconds
    )
    return settings


def _validate_delay_window(label: str, minimum: int, maximum: int) -> None:
    if minimum < 0 or maximum < 0:
        raise ValueError(f"{label} values must be non-negative")
    if minimum > maximum:
        raise ValueError(f"{label} minimum cannot be greater than maximum")


def build_proxy(settings: Settings):
    """Return a Telethon-compatible PySocks proxy tuple or None."""
    if not settings.proxy_enabled:
        return None
    if socks is None:
        raise RuntimeError("PROXY_ENABLED=true requires PySocks. Install requirements.txt.")
    return (
        socks.SOCKS5,
        settings.proxy_host,
        settings.proxy_port,
        True,  # rdns: resolve DNS through proxy
        settings.proxy_username,
        settings.proxy_password,
    )


def ensure_secure_session_permissions(session_name: str) -> None:
    """Best-effort lock-down of Telethon session SQLite file on POSIX hosts."""
    session_path = Path(f"{session_name}.session")
    if session_path.exists() and os.name == "posix":
        session_path.chmod(0o600)


async def authorize_if_needed(client: TelegramClient) -> None:
    if await client.is_user_authorized():
        logging.info("Existing Telethon session is authorized; no login prompt needed.")
        return

    logging.info("No authorized session found. Starting first-run Telegram login flow.")
    phone = input("Telegram phone number, including country code: ").strip()
    if not phone:
        raise RuntimeError("Phone number is required for first-run login")

    await client.send_code_request(phone)
    code = input("Telegram login code: ").strip()

    try:
        await client.sign_in(phone=phone, code=code)
    except PhoneCodeInvalidError as exc:
        raise RuntimeError("Telegram login code was invalid") from exc
    except PhoneCodeExpiredError as exc:
        raise RuntimeError("Telegram login code expired; rerun the script") from exc
    except SessionPasswordNeededError:
        password = getpass.getpass("Telegram 2FA password: ")
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise RuntimeError("Telegram 2FA password was invalid") from exc

    logging.info("Telegram login completed and session saved.")


async def send_checkin_to_bot(
    client: TelegramClient,
    bot_username: str,
    command: str,
) -> bool:
    logging.info("Targeting bot %s with command %r", bot_username, command)
    try:
        entity = await client.get_entity(bot_username)
        await client.send_message(entity, command)
        logging.info("Successfully sent command to %s", bot_username)
        return True
    except FloodWaitError as exc:
        logging.warning(
            "Telegram requested a wait of %s seconds while contacting %s; continuing with next bot.",
            exc.seconds,
            bot_username,
        )
        return False
    except Exception:
        logging.exception("Failed to process bot %s", bot_username)
        return False


async def sleep_jitter(label: str, minimum: int, maximum: int) -> None:
    seconds = random.randint(minimum, maximum)
    if seconds <= 0:
        logging.info("%s delay disabled", label)
        return
    logging.info("%s sleep: %s seconds", label, seconds)
    await asyncio.sleep(seconds)


async def run_checkins(settings: Settings) -> int:
    await sleep_jitter(
        "Initial randomized start",
        settings.initial_delay_min_seconds,
        settings.initial_delay_max_seconds,
    )

    proxy = build_proxy(settings)
    if proxy:
        logging.info(
            "SOCKS5 proxy enabled: %s:%s username=%s",
            settings.proxy_host,
            settings.proxy_port,
            "yes" if settings.proxy_username else "no",
        )
    else:
        logging.info("SOCKS5 proxy disabled")

    client = TelegramClient(
        settings.session_name,
        settings.api_id,
        settings.api_hash,
        proxy=proxy,
        connection_retries=3,
        request_retries=2,
        timeout=30,
    )

    successes = 0
    failures = 0

    async with client:
        await authorize_if_needed(client)
        ensure_secure_session_permissions(settings.session_name)

        total = len(settings.target_bots)
        for index, bot_username in enumerate(settings.target_bots, start=1):
            ok = await send_checkin_to_bot(client, bot_username, settings.command)
            successes += int(ok)
            failures += int(not ok)

            if index < total:
                await sleep_jitter(
                    "Between-bot human-paced",
                    settings.between_bot_min_seconds,
                    settings.between_bot_max_seconds,
                )

    logging.info("Check-in run finished: successes=%s failures=%s", successes, failures)
    return 0 if failures == 0 else 1


async def async_main() -> int:
    setup_logging()
    try:
        settings = load_settings()
        return await run_checkins(settings)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user")
        return 130
    except Exception:
        logging.exception("Fatal error")
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
