"""OPS-03 · remote deploy trigger: git pull + restart the systemd services.

Pure subprocess orchestration — no DB, no Claude. Triggered only from the bot
(``bot.handlers.deploy``/``deploy_callback``), admin-only, behind an explicit
confirm button and the ``DEPLOY_ENABLED`` master switch.

Restarting ``garmin-bot.service`` restarts the very process running this code — and
that's a sharper problem than it sounds. A direct ``sudo scripts/restart_services.sh``
child lives in the SAME cgroup as ``garmin-bot.service`` (sudo doesn't move it), so the
instant ``systemctl restart`` queues the stop job, systemd's default
``KillMode=control-group`` sends SIGTERM to every process in that cgroup — including
the very child that just asked for the restart. ``--no-block`` only shrinks the race
window, it doesn't close it: observed in practice as an intermittent, spurious
``returncode == -15`` (SIGTERM) with an empty pipe, reported as a false "restart
failed" even though the restart had, in fact, just fired.

``restart_services`` instead runs the script inside a **transient systemd unit**
(``sudo systemd-run --unit=garmin-deploy-restart --collect ...``) rather than as a
direct child. `systemd-run` only opens a short D-Bus round trip to register that unit
and exits — the script itself then runs as a child of PID1 in its OWN cgroup, so it's
never touched by garmin-bot.service's kill. That closes the race structurally instead
of special-casing signal-based return codes.
"""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("deploy")

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTART_SCRIPT = REPO_ROOT / "scripts" / "restart_services.sh"
RESTART_UNIT = "garmin-deploy-restart"


@dataclass
class CommandResult:
    ok: bool
    output: str
    returncode: "int | None" = None


async def _run(*args: str, cwd: "Path | None" = None) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    text = stdout.decode("utf-8", errors="replace").strip()
    result = CommandResult(ok=proc.returncode == 0, output=text, returncode=proc.returncode)
    # Logged server-side (journalctl -u garmin-bot) regardless of what makes it into the
    # Telegram reply — a denied sudo call can produce an empty/near-empty pipe (e.g. the
    # rejection goes to the syslog auth log, not to this process' stdout/stderr).
    logger.info(f"DEPLOY {' '.join(args)} → code={proc.returncode} output={text!r}")
    return result


async def git_pull() -> CommandResult:
    # --ff-only: a diverged history fails loudly instead of silently creating a merge
    # commit the admin didn't ask for — SSH in and sort it out by hand instead.
    return await _run("git", "pull", "--ff-only", cwd=REPO_ROOT)


async def restart_services() -> CommandResult:
    # --collect: systemd forgets the transient unit once it exits, instead of piling up
    # a "garmin-deploy-restart" entry in `systemctl list-units` after every deploy.
    return await _run(
        "sudo", "systemd-run", f"--unit={RESTART_UNIT}", "--collect", str(RESTART_SCRIPT)
    )
