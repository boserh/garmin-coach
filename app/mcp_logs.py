"""Ops-log tools for the coach MCP server: read the deployment's own logs without
SSH-ing into the Pi.

Investigating a bug used to start with "send me the last hour of bot.log" — which needs
the owner at a terminal. These tools let the MCP client the admin already connected
(:mod:`app.mcp_server`, the "Bihun" connector) pull exactly the slice it needs.

They ride on the coach server rather than a server of their own on purpose: they are
read-only like every other tool there, and a fourth process would have cost a tunnel
hostname, a systemd unit and a second connector for nothing a per-call check doesn't
already give. That check is the one rule that differs: logs mention EVERY account on the
install, so each tool refuses a token whose user is not an admin (:func:`_authorize`).
An ordinary athlete's coach token sees the tools listed and gets a refusal.

* ``read_app_log`` — the shared rotating application log (``LOG_FILE`` + its ``.1``…
  ``.5`` backups) that every process writes, parsed into records (a traceback stays
  attached to its line) and filtered by time / level / logger / regex.
* ``read_journal`` — ``journalctl`` for one of the units in :data:`UNITS`: start/stop,
  crashes before logging was set up, gunicorn, the backup unit. Whitelisted unit names,
  argv without a shell, a timeout. Needs the service user in ``systemd-journal``.
* ``service_status`` — ``systemctl show`` for the same units.

No Claude and no Garmin call, and everything returned passes :func:`redact` first (bot
tokens, API keys, bearer tokens, passwords) — defence in depth: nothing should log a
secret, but this is the one place such a line would leave the Pi.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mcp.server.auth.middleware.auth_context import get_access_token

from app.core.config import settings
from app.db import users
from app.db.base import async_session_maker

logger = logging.getLogger("mcp.logs")

# The units journal/status may be asked about. A whitelist, not a pattern: the name goes
# into an argv, and "any unit on the box" (sshd, the tunnel) is not this server's to show.
UNITS = (
    "garmin-bot",
    "garmin-admin-bot",
    "garmin-web",
    "garmin-mcp",
    "garmin-mcp-notify",
    "garmin-backup",
)

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# How much one call may return. An MCP answer lands in the client's context window: a
# whole megabyte of log would crowd out the investigation it was fetched for. Newest
# records win when the cap bites — the end of a window is where a failure usually is.
DEFAULT_MAX_CHARS = 40_000
HARD_MAX_CHARS = 150_000
DEFAULT_LIMIT = 200
HARD_MAX_LIMIT = 2_000
JOURNAL_TIMEOUT_S = 20
JOURNAL_SCAN_LINES = 50_000

# --- redaction ----------------------------------------------------------------------

_REDACTIONS = (
    # Telegram bot token: <bot id>:<35 chars>.
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<telegram-token>"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), "<anthropic-key>"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <token>"),
    (re.compile(r"(?i)\b(password|passwd|secret|api_key|apikey|token)(\s*[=:]\s*)"
                r"(['\"]?)[^\s'\",;&]{4,}\3"), r"\1\2<redacted>"),
)


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# --- time arguments -----------------------------------------------------------------

_RELATIVE = re.compile(r"^\s*(\d+)\s*(m|min|h|d)\s*(?:ago)?\s*$", re.I)


def parse_when(value: Optional[str], *, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """A naive LOCAL datetime from ``"30m"``/``"2h"``/``"3d"`` (that long ago) or an ISO
    date/datetime. The log's timestamps are the Pi's local time, so the comparison is too.

    Parsed here rather than handed to journalctl as-is: the value goes into an argv, and
    one parser for both tools means "since=2h" means the same thing in each.
    """
    if value is None or not str(value).strip():
        return None
    now = now or dt.datetime.now()
    m = _RELATIVE.match(str(value))
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": dt.timedelta(minutes=n), "min": dt.timedelta(minutes=n),
                 "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n)}[unit]
        return now - delta
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(
            f"Can't read {value!r} as a time: use '30m', '2h', '3d' (that long ago) or an "
            "ISO date/datetime like '2026-10-12' or '2026-10-12T07:30'."
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _clamp(value: int, default: int, hard_max: int) -> int:
    if value is None or value <= 0:
        return default
    return min(int(value), hard_max)


def _compile(grep: Optional[str]) -> Optional[re.Pattern]:
    if not grep:
        return None
    try:
        return re.compile(grep, re.I)
    except re.error as exc:
        raise ValueError(f"Invalid regex {grep!r}: {exc}") from None


def _fit(chunks: list[str], max_chars: int) -> tuple[str, bool]:
    """Join newest-last chunks, dropping the OLDEST until it fits."""
    out, size = [], 0
    for chunk in reversed(chunks):
        if size + len(chunk) + 1 > max_chars:
            return "\n".join(reversed(out)), True
        out.append(chunk)
        size += len(chunk) + 1
    return "\n".join(reversed(out)), False


# --- the application log ------------------------------------------------------------

# app.core.logging's format: "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s".
_RECORD = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (\w+)\s* \| ([^|]+?) \| ?(.*)$"
)


@dataclass
class Record:
    ts: Optional[dt.datetime]
    level: str
    name: str
    text: str           # the whole record: first line + any continuation (traceback)


def log_files(base: Optional[str] = None) -> list[Path]:
    """The log and its rotated backups, OLDEST first (``.5`` … ``.1``, then the live file)."""
    path = Path(base or settings.LOG_FILE)
    rotated = []
    for p in path.parent.glob(path.name + ".*"):
        suffix = p.name[len(path.name) + 1:]
        if suffix.isdigit():
            rotated.append((int(suffix), p))
    files = [p for _, p in sorted(rotated, reverse=True)]
    if path.exists():
        files.append(path)
    return files


def parse_records(lines) -> list[Record]:
    """Group raw lines into records: a line that doesn't open a record (a traceback, a
    multi-line message) belongs to the one before it. Lines before the first record (a
    file that rotated mid-traceback) become a record of their own with no timestamp."""
    records: list[Record] = []
    for raw in lines:
        line = raw.rstrip("\n")
        m = _RECORD.match(line)
        if m:
            ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            records.append(Record(ts, m.group(2).upper(), m.group(3).strip(), line))
        elif records:
            records[-1].text += "\n" + line
        elif line.strip():
            records.append(Record(None, "", "", line))
    return records


def read_records(base: Optional[str] = None) -> list[Record]:
    records: list[Record] = []
    for path in log_files(base):
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                records.extend(parse_records(fh))
        except OSError as exc:  # rotated away between glob and open
            logger.info("LOGS: skipped %s (%s)", path, exc)
    return records


def filter_records(
    records: list[Record],
    *,
    since: Optional[dt.datetime] = None,
    until: Optional[dt.datetime] = None,
    level: Optional[str] = None,
    logger_prefix: Optional[str] = None,
    pattern: Optional[re.Pattern] = None,
    context: int = 0,
) -> list[Record]:
    """Records matching every given filter; ``context`` adds that many neighbouring
    records (inside the time window) around each match — what happened just before an
    error is usually the point."""
    min_level = LEVELS.index(level) if level else None
    window = [
        r for r in records
        if (since is None or (r.ts is not None and r.ts >= since))
        and (until is None or (r.ts is not None and r.ts <= until))
    ]
    hits = []
    for i, r in enumerate(window):
        if min_level is not None and (r.level not in LEVELS
                                      or LEVELS.index(r.level) < min_level):
            continue
        if logger_prefix and not (r.name == logger_prefix
                                  or r.name.startswith(logger_prefix + ".")):
            continue
        if pattern is not None and not pattern.search(r.text):
            continue
        hits.append(i)
    if not context:
        return [window[i] for i in hits]
    keep = sorted({j for i in hits
                   for j in range(max(0, i - context), min(len(window), i + context + 1))})
    return [window[j] for j in keep]


# --- auth ---------------------------------------------------------------------------


async def _authorize() -> str:
    """Refuse anyone but the deployment's admin. Over stdio there is no token: the
    process is a local child of whoever launched it (who can read the files anyway)."""
    token = get_access_token()
    if token is None or not token.subject:
        return "stdio"
    user_id = int(token.subject)
    async with async_session_maker() as session:
        user = await users.get_by_id(session, user_id)
    if user is None or not user.is_admin:
        logger.warning("LOGS: refused non-admin user_id=%s", user_id)
        raise PermissionError("Logs are restricted to the deployment's admin account.")
    return f"user {user_id}"


# --- tools --------------------------------------------------------------------------


async def read_app_log(
    since: Optional[str] = "1h",
    until: Optional[str] = None,
    level: Optional[str] = None,
    logger_name: Optional[str] = None,
    grep: Optional[str] = None,
    context: int = 0,
    limit: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """Read the application log every Bihun process writes (bot, admin bot, web, MCP
    servers) — the file behind `bot.log`, including its rotated backups (~6 MB total).

    Each record is one log call: `YYYY-MM-DD HH:MM:SS | LEVEL | logger | message`, with
    any traceback kept attached. Timestamps are the Pi's LOCAL time (Europe/Warsaw).

    Filters (all optional, combined with AND):
    - `since` / `until`: '30m', '2h', '3d' (that long ago) or ISO '2026-10-12T07:30'.
      `since` defaults to '1h'; pass '' for no lower bound.
    - `level`: minimum level — DEBUG, INFO, WARNING, ERROR, CRITICAL.
    - `logger_name`: a logger and its children, e.g. 'claude', 'bot.jobs', 'api', 'garmin'.
    - `grep`: case-insensitive Python regex over the whole record (traceback included),
      e.g. 'MORNING|morning_job', 'user_id=3\\b', 'Traceback'.
    - `context`: also return this many records before and after each match.

    Returns the NEWEST `limit` matching records (default 200, max 2000), trimmed from the
    oldest end to `max_chars` (default 40000, max 150000). `truncated` says whether
    anything was cut — narrow the window or the filters rather than raising the caps.
    Secrets are redacted. Read-only; no Claude or Garmin call.
    """
    who = await _authorize()
    lvl = level.strip().upper() if level else None
    if lvl and lvl not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    since_dt, until_dt = parse_when(since), parse_when(until)
    pattern = _compile(grep)
    limit = _clamp(limit, DEFAULT_LIMIT, HARD_MAX_LIMIT)
    max_chars = _clamp(max_chars, DEFAULT_MAX_CHARS, HARD_MAX_CHARS)

    files = log_files()
    records = await asyncio.to_thread(read_records)
    hits = filter_records(
        records, since=since_dt, until=until_dt, level=lvl,
        logger_prefix=(logger_name or "").strip() or None,
        pattern=pattern, context=max(0, min(int(context or 0), 20)),
    )
    matched = len(hits)
    hits = hits[-limit:]
    text, cut = _fit([redact(r.text) for r in hits], max_chars)
    logger.info("LOGS: %s read_app_log since=%s level=%s logger=%s grep=%r → %d/%d",
                who, since, lvl, logger_name, grep, len(hits), matched)
    return {
        "log_file": str(Path(settings.LOG_FILE).resolve()),
        "files_read": [p.name for p in files],
        "oldest_record": next((r.ts.isoformat(sep=" ") for r in records if r.ts), None),
        "matched": matched,
        "returned": len(hits),
        "truncated": cut or matched > len(hits),
        "text": text,
    }


def _unit(unit: str) -> str:
    name = (unit or "").strip().removesuffix(".service")
    if name not in UNITS:
        raise ValueError(f"Unknown unit {unit!r}; one of: {', '.join(UNITS)}")
    return name + ".service"


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=JOURNAL_TIMEOUT_S,
        errors="replace", check=False,
    )


async def read_journal(
    unit: str = "garmin-bot",
    since: Optional[str] = "1h",
    until: Optional[str] = None,
    priority: Optional[str] = None,
    grep: Optional[str] = None,
    lines: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """Read systemd's journal for one Bihun service — what the app log can't show:
    start/stop/restart, crashes before logging was set up, OOM kills, gunicorn's own
    output, the nightly backup unit (garmin-backup), anything printed to stderr.

    `unit`: one of garmin-bot, garmin-admin-bot, garmin-web, garmin-mcp,
    garmin-mcp-notify, garmin-backup.
    `since` / `until`: '30m', '2h', '3d' or ISO datetime (Pi local time); since
    defaults to '1h'. `priority`: max syslog priority, e.g. 'err', 'warning'.
    `grep`: case-insensitive regex (filtered here, after journalctl).
    `lines`: newest N entries (default 200, max 2000), then trimmed to `max_chars`.

    Note: the services also print their app log to stdout, so the journal repeats it —
    prefer read_app_log for application events. Read-only.
    """
    who = await _authorize()
    name = _unit(unit)
    lines = _clamp(lines, DEFAULT_LIMIT, HARD_MAX_LIMIT)
    max_chars = _clamp(max_chars, DEFAULT_MAX_CHARS, HARD_MAX_CHARS)
    pattern = _compile(grep)
    argv = ["journalctl", f"--unit={name}", "--no-pager", "--output=short-iso",
            "--quiet"]
    since_dt, until_dt = parse_when(since), parse_when(until)
    if since_dt:
        argv.append(f"--since={since_dt:%Y-%m-%d %H:%M:%S}")
    if until_dt:
        argv.append(f"--until={until_dt:%Y-%m-%d %H:%M:%S}")
    if priority:
        prio = priority.strip().lower()
        if prio not in ("emerg", "alert", "crit", "err", "warning", "notice", "info",
                        "debug") and not prio.isdigit():
            raise ValueError("priority must be a syslog level name like 'err' or 'warning'")
        argv.append(f"--priority={prio}")
    # With a regex the newest N MATCHES are wanted, so journalctl can't cut to N first;
    # it still gets a ceiling, so a '3d' window can't pull the whole journal into memory.
    argv.append(f"--lines={lines if pattern is None else JOURNAL_SCAN_LINES}")

    try:
        proc = await asyncio.to_thread(_run, argv)
    except FileNotFoundError:
        raise ValueError("journalctl is not available on this host.") from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"journalctl took over {JOURNAL_TIMEOUT_S}s — narrow `since`.") \
            from None
    if proc.returncode != 0 and not proc.stdout:
        hint = ""
        if "permission" in proc.stderr.lower() or "not seeing messages" in proc.stderr:
            hint = (" The service user can't read the journal: add it to the "
                    "'systemd-journal' group (sudo usermod -aG systemd-journal pi) and "
                    "restart garmin-mcp-logs.")
        raise ValueError(f"journalctl failed: {proc.stderr.strip()[:500]}{hint}")

    entries = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if pattern is not None:
        entries = [ln for ln in entries if pattern.search(ln)]
    matched = len(entries)
    entries = entries[-lines:]
    text, cut = _fit([redact(ln) for ln in entries], max_chars)
    logger.info("LOGS: %s read_journal %s since=%s → %d/%d", who, name, since,
                len(entries), matched)
    result = {"unit": name, "matched": matched, "returned": len(entries),
              "truncated": cut or matched > len(entries), "text": text}
    if "not seeing messages" in proc.stderr:
        result["warning"] = proc.stderr.strip()[:500]
    return result


_STATUS_PROPS = ("ActiveState", "SubState", "Result", "NRestarts",
                 "ActiveEnterTimestamp", "ExecMainStartTimestamp", "ExecMainStatus",
                 "MainPID", "MemoryCurrent", "LoadState")


def parse_show(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


async def service_status(unit: Optional[str] = None) -> dict:
    """Is each Bihun service running, since when, how often has it restarted?

    `unit`: one service name (see read_journal) or omit for all of them. Units not
    installed on this host show LoadState 'not-found'. Read-only.
    """
    who = await _authorize()
    names = [_unit(unit)] if unit else [u + ".service" for u in UNITS]
    out = {}
    for name in names:
        try:
            proc = await asyncio.to_thread(
                _run, ["systemctl", "show", name, "--property=" + ",".join(_STATUS_PROPS)]
            )
        except FileNotFoundError:
            raise ValueError("systemctl is not available on this host.") from None
        props = parse_show(proc.stdout)
        mem = props.get("MemoryCurrent", "")
        if mem.isdigit():
            props["MemoryCurrent"] = f"{int(mem) / 1_048_576:.1f} MiB"
        out[name.removesuffix(".service")] = props or {"error": proc.stderr.strip()[:300]}
    logger.info("LOGS: %s service_status %s", who, unit or "all")
    return {"now": dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
            "services": out}


TOOLS = (read_app_log, read_journal, service_status)
