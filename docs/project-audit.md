# Project audit — 2026-10-08

Scope: all original tracked files, application configuration/session/action/
report paths, Docker/Compose, scheduler scripts, and documented setup. The
audit used source review, an independent review, offline reproductions,
regression tests, and Docker validation. Live Telegram actions were not run.

## Addressed

| Finding | Change | Regression coverage |
| --- | --- | --- |
| Button lookup could claim a click when Telethon returned `None` for unmatched text | Check exact keyboard text before clicking; continue newest-to-oldest search | Older matching keyboard, fully absent button, next-bot failure isolation |
| Duplicate IDs reuse an account's encrypted session namespace | Reject duplicate IDs before connecting | Duplicate account configuration |
| Telethon context manager starts login before the configured login flow | Connect explicitly and disconnect in `finally` | Configured phone, first login with 2FA, cached authorization, failure cleanup |
| Example YAML prevents environment-only report/proxy activation | Either source can enable; a YAML-enabled proxy retains its address precedence; Compose forwards proxy variables | Environment and YAML activation, copied environment defaults, quoted false |
| Missing credential variables are accepted as literal placeholders | Treat an unresolved optional credential placeholder as missing | Enabled report missing required credentials |
| SQLite transaction context leaves connection handles open | Close each connection after its transaction | Real temporary database cleanup, reopen, encryption, history, invalid keys |
| Invalid usernames or negative action waits reach the execution stage | Validate these before connecting | Blank/bare usernames and negative waits; existing shorthands remain supported |
| A long report exceeds Telegram's message limit | Send complete consecutive chunks without splitting Unicode characters | Short message unchanged, long Unicode report preserved |
| A successful check-in loses its state if completion time cannot be fetched | Fall back to already trusted start time and atomically replace the marker | Confirmation failure, ordinary success, failed check-in, file permissions |
| A future success marker is treated as due | Fail closed with guard exit code 2 | No Docker action and unchanged future marker |
| Nonroot Docker user cannot read private bind mounts owned by another host UID | Optional Compose UID/GID configuration and matching setup instructions | CI image storage test using a different numeric UID |
| Build context includes private files and environments | Add `.dockerignore` exclusions | Docker build context review and build validation |
| No automated checks | Add pinned, read-only GitHub Actions CI and offline regression suite | Three Python versions, shell syntax, Compose, Docker smoke test |

The cryptography dependency remains `>=49,<50` to address Dependabot alert
#4 (GHSA-jwv3-5hgf-82ww / CVE-2026-69249).

## Preserved behavior

Per-account/per-bot queues, all four action types, command shorthand and
action aliases, SOCKS5 remote DNS, pacing settings, encrypted session format,
SQLite history schema, normal one-shot exit codes, and failure isolation
remain intact. Report delivery failure continues to be logged without
changing successful check-in exit status. The scheduler keeps its rolling
24-hour default and records success only after a successful Docker run.

## Remaining improvements with behavioral implications

1. **Partial-run retries:** failed runs keep the last success marker. The next
   trigger retries the whole queue, including previously successful actions.
   Per-bot daily state or a configurable failure retry interval should be
   designed separately because it changes which actions execute and when.
2. **Overlapping entry points:** the daily guard holds a lock; direct
   `docker compose run` and `scripts/run-once.sh` bypass that lock. Operators
   should avoid starting these while the guard is active. Unifying locking
   must preserve intentional interactive first-login runs.
3. **Flood waits:** the current runner records a failure and continues its
   configured queue. Stopping or deferring a whole account on a flood wait
   needs an explicit pacing/retry policy.
4. **Storage retention:** execution history and logs grow without a retention
   policy. Add rotation/retention with operator-selected history requirements.
5. **Reproducible deployments:** dependency ranges and the tracked Python base
   tag allow newer compatible releases at rebuild time. A reviewed lockfile
   and automated base-image pin updates would improve reproducibility.

The production server currently has local Docker image/base pins and a
calendar-day scheduler variant that differ from the repository. This PR
does not overwrite that deployment or its private account configuration.
Deployment should reconcile those changes deliberately after review.
