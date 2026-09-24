"""The monitoring channel: text in, Telegram out.

The third bot identity in this project, and the reason it exists is the same reason
:mod:`bot.opsalert` exists — *who is speaking* matters more than *what is sent*:

- ``TELEGRAM_BOT_TOKEN``       — the coach. Reports, nudges, plans.
- ``TELEGRAM_ADMIN_BOT_TOKEN`` — the system bot. ``/deploy``, ``/test_*``, ops alerts.
- ``TELEGRAM_MONITOR_BOT_TOKEN`` — this one. Whatever an outside MCP client pushes in
  (the morning war-threshold brief), and nothing else.

Unlike ``send_ops_alert`` there is **no fallback to another bot**: an ops alert must
never be lost, but a monitoring message misdelivered into the coaching thread is worse
than one that fails loudly — the caller is a machine that gets the error back and can
retry. Same reasoning for the destination: it is read from settings
(``TELEGRAM_MONITOR_CHAT_ID``), never from the calling user's ``telegram_chat_id``, so
the channel belongs to the deployment rather than to whoever's token happened to call.

That setting may name several subscribers (comma-separated). Each gets the whole
message; one subscriber who blocked the bot must not cost the others their brief, so a
per-chat failure is logged and counted, and only "nobody got it" is an error — which is
also the only case where the caller retrying can't duplicate a message someone already
has.

The only non-obvious mechanic here is chunking. Telegram rejects a message over 4096
characters outright, and a monitoring digest is exactly the kind of text that quietly
grows past it one morning — so ``split_message`` cuts on paragraph, then line, then
(only if a single line is itself oversized) on a hard boundary, and the parts go out in
order. Splitting is pure and tested; sending is a thin wrapper around it.
"""
from __future__ import annotations

import logging
from typing import List, NamedTuple, Optional

from app.core.config import settings

logger = logging.getLogger("notify")

# Telegram's own limit is 4096 characters per message; leave room for the "(2/3)" suffix
# a multi-part send appends.
MAX_CHARS = 4000
# A ceiling on the whole payload, before chunking: past this, something upstream is
# looping or dumping raw input, and 20 Telegram messages at 06:00 is not the answer.
MAX_TOTAL_CHARS = 20_000

_monitor_bot = None  # built lazily, cached — same shape as bot.opsalert._admin_bot
_coach_bot = None    # same shape, for TELEGRAM_BOT_TOKEN — the product/coaching identity


class NotifyError(RuntimeError):
    """Delivery did not happen. The message text is in ``args[0]``, meant for the caller
    (an MCP client), so it says what to fix rather than what threw."""


class Delivery(NamedTuple):
    """What :func:`send_monitor_message` did: ``parts`` messages per chat, to
    ``delivered`` chats; ``failed`` chats refused (already logged)."""
    parts: int
    delivered: int
    failed: int


def monitor_chat_ids(raw=None) -> List[int]:
    """Parse ``TELEGRAM_MONITOR_CHAT_ID`` — one id or a comma-separated list — into
    distinct ints, in order. Blank → ``[]``. A malformed entry raises :class:`NotifyError`
    naming it, rather than being skipped: a typo'd subscriber silently never receiving
    anything is exactly what nobody notices."""
    if raw is None:
        raw = settings.TELEGRAM_MONITOR_CHAT_ID
    if raw is None:
        return []
    ids: List[int] = []
    for token in str(raw).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            chat_id = int(token)
        except ValueError:
            raise NotifyError(
                f"TELEGRAM_MONITOR_CHAT_ID has a malformed entry {token!r}: expected "
                "chat ids separated by commas, e.g. 123,-100456."
            ) from None
        if chat_id not in ids:
            ids.append(chat_id)
    return ids


def split_message(text: str, limit: int = MAX_CHARS) -> List[str]:
    """Cut ``text`` into Telegram-sized parts, preferring natural boundaries.

    Order of preference: paragraph break, line break, then a hard cut — a 5000-character
    single line still has to go somewhere. Never returns an empty part; a blank input
    returns ``[]`` so the caller can reject it before touching the network.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        # rindex of the widest natural boundary that leaves a non-trivial chunk. The
        # `> limit // 4` guard stops a stray "\n\n" near the start from producing a
        # two-word part followed by a still-oversized remainder.
        cut = -1
        for sep in ("\n\n", "\n", " "):
            at = window.rfind(sep)
            if at > limit // 4:
                cut = at + (len(sep) if sep == "\n\n" else 0)
                break
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def _get_bot():
    """The monitoring bot identity, or None when this install has no monitoring channel."""
    global _monitor_bot
    if not settings.TELEGRAM_MONITOR_BOT_TOKEN:
        return None      # never cache a None — the token can be set and the process reloaded
    if _monitor_bot is None:
        from telegram import Bot

        _monitor_bot = Bot(token=settings.TELEGRAM_MONITOR_BOT_TOKEN)
    return _monitor_bot


async def send_monitor_message(
    text: str, *, parse_mode: Optional[str] = None, silent: bool = False
) -> Delivery:
    """Deliver ``text`` to every monitoring chat. Raises only when no chat received it.

    ``parse_mode`` is optional and best-effort: Telegram rejects the whole message when
    the markup is malformed (an unescaped ``_`` in a place name is enough), and a brief
    that arrives as plain text beats one that does not arrive — so a formatting rejection
    is retried once, unformatted. Every other failure raises :class:`NotifyError`.
    """
    bot = _get_bot()
    if bot is None:
        raise NotifyError(
            "Monitoring channel is not configured on the server: set "
            "TELEGRAM_MONITOR_BOT_TOKEN."
        )
    chat_ids = monitor_chat_ids()
    if not chat_ids:
        raise NotifyError(
            "Monitoring channel is not configured on the server: set "
            "TELEGRAM_MONITOR_CHAT_ID."
        )
    if len(text or "") > MAX_TOTAL_CHARS:
        raise NotifyError(
            f"Message is {len(text)} characters; the limit is {MAX_TOTAL_CHARS}. "
            "Send a summary, not the raw source."
        )
    parts = split_message(text or "")
    if not parts:
        raise NotifyError("Message is empty.")

    total = len(parts)
    errors = []
    for chat_id in chat_ids:
        try:
            await _send_parts(bot, chat_id, parts, parse_mode=parse_mode, silent=silent)
        except Exception as exc:  # noqa: BLE001 — counted; raised below only if all failed
            logger.error("NOTIFY: send to chat %s failed: %s", chat_id, exc)
            errors.append(exc)
    if len(errors) == len(chat_ids):
        exc = errors[0]
        raise NotifyError(
            f"Telegram refused the message: {exc}. If this is 'chat not found' or "
            "'bot was blocked', press Start on the monitoring bot (or re-add it to "
            "the group) so it may write there."
        ) from exc
    delivered = len(chat_ids) - len(errors)
    logger.info("NOTIFY: sent %d part(s), %d chars to %d chat(s), %d failed",
                total, len(text), delivered, len(errors))
    return Delivery(parts=total, delivered=delivered, failed=len(errors))


async def _send_parts(bot, chat_id: int, parts: List[str], *,
                      parse_mode: Optional[str], silent: bool) -> None:
    """Send every part to one chat, in order. A markup rejection is retried once as
    plain text; any other failure propagates."""
    total = len(parts)
    for i, part in enumerate(parts, 1):
        body = part if total == 1 else f"{part}\n\n({i}/{total})"
        try:
            await bot.send_message(
                chat_id, body, parse_mode=parse_mode, disable_notification=silent
            )
        except Exception as exc:  # noqa: BLE001
            if not parse_mode:
                raise
            logger.warning(
                "NOTIFY: %s rejected (%s) — resending part %d/%d to %s as plain text",
                parse_mode, exc, i, total, chat_id,
            )
            await bot.send_message(chat_id, body, disable_notification=silent)


def _get_coach_bot():
    """The product/coaching bot identity (``TELEGRAM_BOT_TOKEN`` — the same one /report
    and the morning job use), built standalone here because the web process has no
    running ``bot.Application``/``ctx.bot`` of its own — same one-off ``Bot()`` pattern as
    ``app.cli._trigger_plan_adapt``."""
    global _coach_bot
    if not settings.TELEGRAM_BOT_TOKEN:
        return None       # never cache a None — the token can be set and the process reloaded
    if _coach_bot is None:
        from telegram import Bot

        _coach_bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    return _coach_bot


async def send_coach_message(chat_id: int, text: str, *, silent: bool = False) -> int:
    """Deliver ``text`` to one athlete's own Telegram chat over the product bot identity —
    for web-triggered sends, e.g. the activity page's "regenerate and send to Telegram"
    button, where there's no bot-side handler already holding the chat open. Returns how
    many messages were sent. Unlike :func:`send_monitor_message` the destination is the
    CALLER's chat_id (the user's own linked chat), not a fixed deployment channel."""
    bot = _get_coach_bot()
    if bot is None:
        raise NotifyError("TELEGRAM_BOT_TOKEN is not configured on the server.")
    parts = split_message(text or "")
    if not parts:
        raise NotifyError("Message is empty.")
    total = len(parts)
    for i, part in enumerate(parts, 1):
        body = part if total == 1 else f"{part}\n\n({i}/{total})"
        try:
            await bot.send_message(chat_id, body, disable_notification=silent)
        except Exception as exc:  # noqa: BLE001 — turned into the caller's error below
            logger.error("NOTIFY: coach send failed on part %d/%d: %s", i, total, exc)
            raise NotifyError(
                f"Telegram refused the message: {exc}. If this is 'chat not found' or "
                "'bot was blocked', press Start on the bot so it may write there."
            ) from exc
    logger.info("NOTIFY: sent %d coach part(s), %d chars to chat %s", total, len(text), chat_id)
    return total
