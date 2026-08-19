"""System alerts go to the SYSTEM bot, not the coaching one.

``bot.main`` runs every scheduled job, so anything a job wants to say has ``ctx.bot`` —
the product bot — right at hand. That is correct for coaching messages (a report, a
nudge, a spent budget) and wrong for infrastructure ones: "the off-SD backup copy is
failing, check the USB" is not something the athlete's coach says between a sleep score
and tomorrow's intervals. It belongs to ``bot.admin_main``
(``TELEGRAM_ADMIN_BOT_TOKEN``), the same channel ``/deploy`` and the ``/test_*``
commands already live on.

This is the mirror image of ``jobs._main_bot_ctx``, which exists because the forced
``/test_morning`` runs under the admin bot and its *product* output must go out over the
main token. Same trick, other direction: the admin bot runs no JobQueue of its own (see
its module docstring), so the check stays in the morning tick and only the identity it
speaks under changes. The ``Bot`` is built once, lazily, and cached — same as
``_main_bot``.

Fallbacks, in order — an ops alert must never be lost just because it could not be sent
*prettily*:

1. no ``TELEGRAM_ADMIN_BOT_TOKEN`` configured (single-bot install) → the product bot;
2. the admin bot cannot reach that chat (Telegram forbids a bot from opening a
   conversation, so an admin who never pressed Start on the system bot is unreachable)
   or any other send failure → the product bot, with a WARNING naming why.
"""
from __future__ import annotations

import logging

from telegram import Bot

from app.core.config import settings

logger = logging.getLogger("bot")

_admin_bot: "Bot | None" = None


def _get_admin_bot() -> "Bot | None":
    """The system bot identity, or None on a single-bot install."""
    global _admin_bot
    if not settings.TELEGRAM_ADMIN_BOT_TOKEN:
        return None      # never cache a None — the token can be set and the process reloaded
    if _admin_bot is None:
        _admin_bot = Bot(token=settings.TELEGRAM_ADMIN_BOT_TOKEN)
    return _admin_bot


async def send_ops_alert(ctx, chat_id: int, text: str) -> None:
    """Send an infrastructure alert through the admin bot, falling back to ``ctx.bot``.

    ``ctx`` is the PTB job context (only ``ctx.bot`` is used), ``chat_id`` the admin's
    chat. Best-effort by contract: the caller is a guard inside the morning tick.
    """
    admin_bot = _get_admin_bot()
    if admin_bot is not None:
        try:
            await admin_bot.send_message(chat_id, text)
            return
        except Exception as exc:  # noqa: BLE001 — falls back rather than losing the alert
            logger.warning(
                "OPS alert: admin bot send failed (%s) — falling back to the product "
                "bot. If this is 'chat not found' / 'blocked', press Start on the admin "
                "bot so it may write to that chat.", exc,
            )
    await ctx.bot.send_message(chat_id, text)
