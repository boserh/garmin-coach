"""OPS-03 · remote deploy trigger: git pull + restart the systemd services.

Pure subprocess orchestration — no DB, no Claude. Triggered only from the bot
(``bot.handlers.deploy``/``deploy_callback``), admin-only, behind an explicit
confirm button and the ``DEPLOY_ENABLED`` master switch.

Restarting ``garmin-bot.service`` restarts the very process running this code, so
``restart_services`` shells out to ``scripts/restart_services.sh`` via passwordless
sudo (see ``deploy/sudoers-garmin-deploy``) rather than calling systemctl directly —
that keeps the sudoers grant to one fixed script path instead of pattern-matching a
systemctl command line, and the script uses ``--no-block`` so the call returns as
soon as the restart job is queued instead of waiting on a process that's about to
be killed.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTART_SCRIPT = REPO_ROOT / "scripts" / "restart_services.sh"


@dataclass
class CommandResult:
    ok: bool
    output: str


async def _run(*args: str, cwd: "Path | None" = None) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode("utf-8", errors="replace").strip()
    return CommandResult(ok=proc.returncode == 0, output=text)


async def git_pull() -> CommandResult:
    # --ff-only: a diverged history fails loudly instead of silently creating a merge
    # commit the admin didn't ask for — SSH in and sort it out by hand instead.
    return await _run("git", "pull", "--ff-only", cwd=REPO_ROOT)


async def restart_services() -> CommandResult:
    return await _run("sudo", str(RESTART_SCRIPT))
