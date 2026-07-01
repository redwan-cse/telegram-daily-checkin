#!/usr/bin/env python3
"""
Telegram daily bot check-in helper using Telethon MTProto sessions.

Production-oriented features:
- Per-account, per-bot command configuration from YAML.
- Encrypted Telethon StringSession storage in SQLite.
- Optional SOCKS5 proxy support through Telethon/PySocks.
- Conservative randomized execution delays.
- Per-bot failure isolation.
- Optional Telegram Bot API execution report delivery.

Use only with Telegram accounts and bots you are authorized to operate.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import random
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

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
class BotAction:
    type: str
    value: str
    wait_seconds: int = 5


@dataclass(frozen=True)
class BotCheckin:
    username: str
    actions: tuple[BotAction, ...]


@dataclass(frozen=True)
class AccountConfig:
    account_id: str
    phone: str | None
    api_id: int
    api_hash: str
    bots: tuple[BotCheckin, ...]


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool
    host: str
    port: int
    username: str | None
    password: str | None


@dataclass(frozen=True)
class DelayConfig:
    initial_min_seconds: int
    initial_max_seconds: int
    between_bot_min_seconds: int
    between_bot_max_seconds: int
    between_account_min_seconds: int
    between_account_max_seconds: int


@dataclass(frozen=True)
class ReportConfig:
    enabled: bool
    bot_token: str | None
    chat_id: str | None
    label: str


@dataclass(frozen=True)
class Settings:
    config_path: Path
    session_db_path: Path
    encryption_key: str
    accounts: tuple[AccountConfig, ...]
    proxy: ProxyConfig
    delays: DelayConfig
    report: ReportConfig
    log_file: Path | None


@dataclass(frozen=True)
class BotResult:
    account_id: str
    bot_username: str
    action_summary: str
    ok: bool
    message: str
    started_at: str
    finished_at: str


def _read_env_file() -> None:
    if load_dotenv is not None:
        load_dotenv()


def setup_logging(log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        handlers=handlers,
        force=True,
    )


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


def _validate_delay_window(label: str, minimum: int, maximum: int) -> None:
    if minimum < 0 or maximum < 0:
        raise ValueError(f"{label} values must be non-negative")
    if minimum > maximum:
        raise ValueError(f"{label} minimum cannot be greater than maximum")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml and edit it."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("Config YAML root must be a mapping")
    return _expand_env(data)


def _required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_accounts(config: dict[str, Any]) -> tuple[AccountConfig, ...]:
    global_api_id = int(_env("TELEGRAM_API_ID", "0") or "0")
    global_api_hash = _env("TELEGRAM_API_HASH")
    accounts_raw = config.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ValueError("config.yaml must define at least one account under accounts:")

    accounts: list[AccountConfig] = []
    for index, account_raw in enumerate(accounts_raw, start=1):
        if not isinstance(account_raw, dict):
            raise ValueError(f"accounts[{index}] must be a mapping")
        account_id = _required_str(account_raw.get("id"), f"accounts[{index}].id")
        api_id = int(account_raw.get("api_id") or global_api_id)
        api_hash = _optional_str(account_raw.get("api_hash")) or global_api_hash
        if api_id <= 0 or not api_hash:
            raise ValueError(
                f"Account {account_id}: set api_id/api_hash in config or TELEGRAM_API_ID/TELEGRAM_API_HASH"
            )

        bots_raw = account_raw.get("bots")
        if not isinstance(bots_raw, list) or not bots_raw:
            raise ValueError(f"Account {account_id}: define at least one bot")
        bots: list[BotCheckin] = []
        for bot_index, bot_raw in enumerate(bots_raw, start=1):
            if isinstance(bot_raw, str):
                username = bot_raw.strip()
                actions = (BotAction(type="send_command", value="/checkin"),)
            elif isinstance(bot_raw, dict):
                username = _required_str(
                    bot_raw.get("username"), f"{account_id}.bots[{bot_index}].username"
                )
                actions_raw = bot_raw.get("actions")
                if actions_raw is None:
                    # Backward-compatible shorthand. If command starts with '/', treat it as a command;
                    # otherwise send it as a plain message such as "📅 Daily Check-in".
                    command = _required_str(
                        bot_raw.get("command", "/checkin"), f"{account_id}.bots[{bot_index}].command"
                    )
                    action_type = "send_command" if command.startswith("/") else "send_message"
                    actions = (BotAction(type=action_type, value=command),)
                else:
                    if not isinstance(actions_raw, list) or not actions_raw:
                        raise ValueError(f"Account {account_id}: bot {username} actions must be a non-empty list")
                    parsed_actions: list[BotAction] = []
                    for action_index, action_raw in enumerate(actions_raw, start=1):
                        if not isinstance(action_raw, dict):
                            raise ValueError(
                                f"Account {account_id}: bot {username} action {action_index} must be a mapping"
                            )
                        action_type = _required_str(
                            action_raw.get("type"),
                            f"{account_id}.{username}.actions[{action_index}].type",
                        )
                        if action_type not in {"send_command", "send_message", "click_button", "manual_ui_required"}:
                            raise ValueError(
                                f"Account {account_id}: unsupported action type {action_type!r} for {username}"
                            )
                        value = _required_str(
                            action_raw.get("value", action_raw.get("text", action_raw.get("button_text", ""))),
                            f"{account_id}.{username}.actions[{action_index}].value",
                        )
                        wait_seconds = int(action_raw.get("wait_seconds", 5))
                        parsed_actions.append(
                            BotAction(type=action_type, value=value, wait_seconds=wait_seconds)
                        )
                    actions = tuple(parsed_actions)
            else:
                raise ValueError(f"Account {account_id}: bot entry {bot_index} must be string or mapping")
            if not username.startswith("@"):
                username = f"@{username}"
            bots.append(BotCheckin(username=username, actions=actions))

        accounts.append(
            AccountConfig(
                account_id=account_id,
                phone=_optional_str(account_raw.get("phone")),
                api_id=api_id,
                api_hash=api_hash,
                bots=tuple(bots),
            )
        )
    return tuple(accounts)


def load_settings() -> Settings:
    _read_env_file()
    config_path = Path(_env("CHECKIN_CONFIG_PATH", "config.yaml"))
    config = _load_yaml(config_path)

    data_dir = Path(_env("DATA_DIR", str(config.get("data_dir", "data"))))
    logs_dir = Path(_env("LOGS_DIR", str(config.get("logs_dir", "logs"))))
    session_db_path = Path(_env("SESSION_DB_PATH", str(data_dir / "sessions.sqlite3")))
    encryption_key = _env("SESSION_ENCRYPTION_KEY")
    if not encryption_key:
        raise ValueError(
            "SESSION_ENCRYPTION_KEY is required. Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )

    proxy_raw = config.get("proxy", {}) if isinstance(config.get("proxy", {}), dict) else {}
    delays_raw = config.get("delays", {}) if isinstance(config.get("delays", {}), dict) else {}
    report_raw = config.get("report", {}) if isinstance(config.get("report", {}), dict) else {}

    delays = DelayConfig(
        initial_min_seconds=int(delays_raw.get("initial_min_seconds", _env_int("INITIAL_DELAY_MIN_SECONDS", 60))),
        initial_max_seconds=int(delays_raw.get("initial_max_seconds", _env_int("INITIAL_DELAY_MAX_SECONDS", 900))),
        between_bot_min_seconds=int(delays_raw.get("between_bot_min_seconds", _env_int("BETWEEN_BOT_MIN_SECONDS", 20))),
        between_bot_max_seconds=int(delays_raw.get("between_bot_max_seconds", _env_int("BETWEEN_BOT_MAX_SECONDS", 75))),
        between_account_min_seconds=int(delays_raw.get("between_account_min_seconds", _env_int("BETWEEN_ACCOUNT_MIN_SECONDS", 30))),
        between_account_max_seconds=int(delays_raw.get("between_account_max_seconds", _env_int("BETWEEN_ACCOUNT_MAX_SECONDS", 120))),
    )
    _validate_delay_window("INITIAL_DELAY", delays.initial_min_seconds, delays.initial_max_seconds)
    _validate_delay_window("BETWEEN_BOT_DELAY", delays.between_bot_min_seconds, delays.between_bot_max_seconds)
    _validate_delay_window("BETWEEN_ACCOUNT_DELAY", delays.between_account_min_seconds, delays.between_account_max_seconds)

    report_enabled = bool(report_raw.get("enabled", _env_bool("REPORT_ENABLED", False)))
    report = ReportConfig(
        enabled=report_enabled,
        bot_token=_optional_str(report_raw.get("bot_token")) or _env("REPORT_BOT_TOKEN") or None,
        chat_id=_optional_str(report_raw.get("chat_id")) or _env("REPORT_CHAT_ID") or None,
        label=str(report_raw.get("label") or _env("REPORT_LABEL", "telegram-daily-checkin")),
    )
    if report.enabled and (not report.bot_token or not report.chat_id):
        raise ValueError("Report delivery enabled but REPORT_BOT_TOKEN/report.bot_token or REPORT_CHAT_ID/report.chat_id is missing")

    log_file_raw = _optional_str(config.get("log_file")) or _env("LOG_FILE", str(logs_dir / "checkin.log"))

    return Settings(
        config_path=config_path,
        session_db_path=session_db_path,
        encryption_key=encryption_key,
        accounts=_parse_accounts(config),
        proxy=ProxyConfig(
            enabled=bool(proxy_raw.get("enabled", _env_bool("PROXY_ENABLED", False))),
            host=str(proxy_raw.get("host", _env("PROXY_HOST", "127.0.0.1"))),
            port=int(proxy_raw.get("port", _env_int("PROXY_PORT", 1080))),
            username=_optional_str(proxy_raw.get("username")) or _env("PROXY_USERNAME") or None,
            password=_optional_str(proxy_raw.get("password")) or _env("PROXY_PASSWORD") or None,
        ),
        delays=delays,
        report=report,
        log_file=Path(log_file_raw) if log_file_raw else None,
    )


class EncryptedSessionStore:
    def __init__(self, db_path: Path, encryption_key: str) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(encryption_key.encode("utf-8"))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists account_sessions (
                    account_id text primary key,
                    encrypted_session text not null,
                    updated_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists execution_runs (
                    id integer primary key autoincrement,
                    started_at text not null,
                    finished_at text,
                    results_json text not null
                )
                """
            )
        if os.name == "posix":
            self.db_path.chmod(0o600)

    def get_session(self, account_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "select encrypted_session from account_sessions where account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            return ""
        try:
            return self.fernet.decrypt(row[0].encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                f"Could not decrypt stored session for {account_id}. Check SESSION_ENCRYPTION_KEY."
            ) from exc

    def save_session(self, account_id: str, session_string: str) -> None:
        encrypted = self.fernet.encrypt(session_string.encode("utf-8")).decode("utf-8")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into account_sessions(account_id, encrypted_session, updated_at)
                values (?, ?, ?)
                on conflict(account_id) do update set
                    encrypted_session = excluded.encrypted_session,
                    updated_at = excluded.updated_at
                """,
                (account_id, encrypted, now),
            )

    def save_run(self, started_at: str, finished_at: str, results: list[BotResult]) -> None:
        payload = json.dumps([result.__dict__ for result in results], ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "insert into execution_runs(started_at, finished_at, results_json) values (?, ?, ?)",
                (started_at, finished_at, payload),
            )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_proxy(proxy_config: ProxyConfig):
    if not proxy_config.enabled:
        return None
    if socks is None:
        raise RuntimeError("Proxy is enabled but PySocks is unavailable. Install requirements.txt.")
    return (
        socks.SOCKS5,
        proxy_config.host,
        proxy_config.port,
        True,
        proxy_config.username,
        proxy_config.password,
    )


async def authorize_if_needed(client: TelegramClient, account: AccountConfig) -> None:
    if await client.is_user_authorized():
        logging.info("Account %s session is authorized; no login prompt needed.", account.account_id)
        return

    logging.info("Account %s needs first-run Telegram login.", account.account_id)
    phone = account.phone or input(f"Telegram phone for account {account.account_id}: ").strip()
    if not phone:
        raise RuntimeError(f"Phone number is required for account {account.account_id}")

    await client.send_code_request(phone)
    code = input(f"Telegram login code for {account.account_id}: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except PhoneCodeInvalidError as exc:
        raise RuntimeError(f"Account {account.account_id}: Telegram login code was invalid") from exc
    except PhoneCodeExpiredError as exc:
        raise RuntimeError(f"Account {account.account_id}: Telegram login code expired") from exc
    except SessionPasswordNeededError:
        password = getpass.getpass(f"Telegram 2FA password for {account.account_id}: ")
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise RuntimeError(f"Account {account.account_id}: Telegram 2FA password was invalid") from exc

    logging.info("Account %s login completed; encrypted session will be saved.", account.account_id)


def summarize_actions(bot: BotCheckin) -> str:
    return ", ".join(f"{action.type}:{action.value}" for action in bot.actions)


async def click_latest_button_by_text(client: TelegramClient, entity: Any, button_text: str) -> None:
    messages = await client.get_messages(entity, limit=5)
    for message in messages:
        if not getattr(message, "buttons", None):
            continue
        try:
            await message.click(text=button_text)
            return
        except ValueError:
            continue
    raise RuntimeError(f"Button {button_text!r} not found in the latest bot messages")


async def execute_bot_action(client: TelegramClient, entity: Any, account_id: str, bot: BotCheckin, action: BotAction) -> str:
    if action.type in {"send_command", "send_message"}:
        await client.send_message(entity, action.value)
        return f"{action.type} sent"
    if action.type == "click_button":
        if action.wait_seconds > 0:
            logging.info(
                "Account %s waiting %s seconds before clicking button %r on %s",
                account_id,
                action.wait_seconds,
                action.value,
                bot.username,
            )
            await asyncio.sleep(action.wait_seconds)
        await click_latest_button_by_text(client, entity, action.value)
        return f"button clicked: {action.value}"
    if action.type == "manual_ui_required":
        raise RuntimeError(
            "manual_ui_required: this bot needs native Telegram app UI automation; "
            "the Docker MTProto runner cannot click desktop/mobile UI. Use a separate host GUI runner."
        )
    raise RuntimeError(f"Unsupported action type: {action.type}")


async def send_checkin_to_bot(
    client: TelegramClient,
    account_id: str,
    bot: BotCheckin,
) -> BotResult:
    started_at = utc_now()
    action_summary = summarize_actions(bot)
    logging.info("Account %s targeting %s with actions: %s", account_id, bot.username, action_summary)
    try:
        entity = await client.get_entity(bot.username)
        action_messages: list[str] = []
        for action in bot.actions:
            action_messages.append(await execute_bot_action(client, entity, account_id, bot, action))
        message = "; ".join(action_messages) or "completed"
        logging.info("Account %s completed actions for %s", account_id, bot.username)
        ok = True
    except FloodWaitError as exc:
        message = f"flood_wait_{exc.seconds}s"
        logging.warning("Account %s flood-wait for %s seconds on %s", account_id, exc.seconds, bot.username)
        ok = False
    except Exception as exc:
        message = f"error: {type(exc).__name__}: {exc}"
        logging.exception("Account %s failed to process bot %s", account_id, bot.username)
        ok = False
    return BotResult(
        account_id=account_id,
        bot_username=bot.username,
        action_summary=action_summary,
        ok=ok,
        message=message,
        started_at=started_at,
        finished_at=utc_now(),
    )


async def sleep_jitter(label: str, minimum: int, maximum: int) -> None:
    seconds = random.randint(minimum, maximum)
    if seconds <= 0:
        logging.info("%s delay disabled", label)
        return
    logging.info("%s sleep: %s seconds", label, seconds)
    await asyncio.sleep(seconds)


async def run_account(
    account: AccountConfig,
    settings: Settings,
    store: EncryptedSessionStore,
) -> list[BotResult]:
    proxy = build_proxy(settings.proxy)
    session_string = store.get_session(account.account_id)
    client = TelegramClient(
        StringSession(session_string),
        account.api_id,
        account.api_hash,
        proxy=proxy,
        connection_retries=3,
        request_retries=2,
        timeout=30,
    )
    results: list[BotResult] = []
    async with client:
        await authorize_if_needed(client, account)
        store.save_session(account.account_id, client.session.save())
        for index, bot in enumerate(account.bots, start=1):
            results.append(await send_checkin_to_bot(client, account.account_id, bot))
            if index < len(account.bots):
                await sleep_jitter(
                    "Between-bot human-paced",
                    settings.delays.between_bot_min_seconds,
                    settings.delays.between_bot_max_seconds,
                )
        store.save_session(account.account_id, client.session.save())
    return results


def format_execution_report(label: str, started_at: str, finished_at: str, results: list[BotResult]) -> str:
    ok_count = sum(1 for result in results if result.ok)
    fail_count = len(results) - ok_count
    lines = [
        f"✅ {label} execution report" if fail_count == 0 else f"⚠️ {label} execution report",
        f"Started: {started_at}",
        f"Finished: {finished_at}",
        f"Success: {ok_count}",
        f"Failed: {fail_count}",
        "",
    ]
    for result in results:
        icon = "✅" if result.ok else "❌"
        lines.append(
            f"{icon} {result.account_id} -> {result.bot_username} `{result.action_summary}`: {result.message}"
        )
    return "\n".join(lines)


def send_telegram_report(report: ReportConfig, text: str) -> None:
    if not report.enabled:
        return
    assert report.bot_token and report.chat_id
    url = f"https://api.telegram.org/bot{report.bot_token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": report.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()
    logging.info("Execution report sent to Telegram chat %s", report.chat_id)


async def run_checkins(settings: Settings) -> int:
    started_at = utc_now()
    store = EncryptedSessionStore(settings.session_db_path, settings.encryption_key)
    await sleep_jitter(
        "Initial randomized start",
        settings.delays.initial_min_seconds,
        settings.delays.initial_max_seconds,
    )

    if settings.proxy.enabled:
        logging.info(
            "SOCKS5 proxy enabled: %s:%s username=%s",
            settings.proxy.host,
            settings.proxy.port,
            "yes" if settings.proxy.username else "no",
        )
    else:
        logging.info("SOCKS5 proxy disabled")

    all_results: list[BotResult] = []
    for index, account in enumerate(settings.accounts, start=1):
        try:
            all_results.extend(await run_account(account, settings, store))
        except Exception as exc:
            logging.exception("Account %s failed before completing its bot queue", account.account_id)
            all_results.append(
                BotResult(
                    account_id=account.account_id,
                    bot_username="<account>",
                    action_summary="<account-run>",
                    ok=False,
                    message=f"account_error: {type(exc).__name__}: {exc}",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
            )
        if index < len(settings.accounts):
            await sleep_jitter(
                "Between-account human-paced",
                settings.delays.between_account_min_seconds,
                settings.delays.between_account_max_seconds,
            )

    finished_at = utc_now()
    store.save_run(started_at, finished_at, all_results)
    report_text = format_execution_report(settings.report.label, started_at, finished_at, all_results)
    logging.info("\n%s", report_text)
    try:
        send_telegram_report(settings.report, report_text)
    except Exception:
        logging.exception("Failed to send Telegram execution report")

    return 0 if all(result.ok for result in all_results) else 1


async def async_main() -> int:
    _read_env_file()
    try:
        settings = load_settings()
        setup_logging(settings.log_file)
        return await run_checkins(settings)
    except KeyboardInterrupt:
        setup_logging(None)
        logging.warning("Interrupted by user")
        return 130
    except Exception:
        setup_logging(None)
        logging.exception("Fatal error")
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
