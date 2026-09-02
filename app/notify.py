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

The only non-obvious mechanic here is chunking. Telegram rejects a message over 4096
characters outright, and a monitoring digest is exactly the kind of text that quietly
grows past it one morning — so ``split_message`` cuts on paragraph, then line, then
(only if a single line is itself oversized) on a hard boundary, and the parts go out in
order. Splitting is pure and tested; sending is a thin wrapper around it.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from app.core.config import settings

logger = logging.getLogger("notify")

# Telegram's own limit is 4096 characters per message; leave room for the "(2/3)" suffix
# a multi-part send appends.
MAX_CHARS = 4000
# A ceiling on the whole payload, before chunking: past this, something upstream is
# looping or dumping raw input, and 20 Telegram messages at 06:00 is not the answer.
MAX_TOTAL_CHARS = 20_000

_monitor_bot = None  # built lazily, cached — same shape as bot.opsalert._admin_bot


class NotifyError(RuntimeError):
    """Delivery did not happen. The message text is in ``args[0]``, meant for the caller
    (an MCP client), so it says what to fix rather than what threw."""


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
) -> int:
    """Deliver ``text`` to the monitoring chat. Returns how many messages were sent.

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
    if settings.TELEGRAM_MONITOR_CHAT_ID is None:
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

    chat_id = settings.TELEGRAM_MONITOR_CHAT_ID
    total = len(parts)
    for i, part in enumerate(parts, 1):
        body = part if total == 1 else f"{part}\n\n({i}/{total})"
        try:
            await bot.send_message(
                chat_id, body, parse_mode=parse_mode, disable_notification=silent
            )
        except Exception as exc:  # noqa: BLE001 — turned into the caller's error below
            if parse_mode:
                logger.warning(
                    "NOTIFY: %s rejected (%s) — resending part %d/%d as plain text",
                    parse_mode, exc, i, total,
                )
                try:
                    await bot.send_message(
                        chat_id, body, disable_notification=silent
                    )
                    continue
                except Exception as plain_exc:  # noqa: BLE001
                    exc = plain_exc
            logger.error("NOTIFY: send failed on part %d/%d: %s", i, total, exc)
            raise NotifyError(
                f"Telegram refused the message: {exc}. If this is 'chat not found' or "
                "'bot was blocked', press Start on the monitoring bot (or re-add it to "
                "the group) so it may write there."
            ) from exc
    logger.info("NOTIFY: sent %d part(s), %d chars to chat %s", total, len(text), chat_id)
    return total
