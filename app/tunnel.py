"""Reads the current Cloudflare quick-tunnel URL.

``cloudflared tunnel --url`` (no domain, no Cloudflare account needed) mints a new
random ``*.trycloudflare.com`` hostname on every start. The systemd unit
(``deploy/cloudflared-tunnel.service``) redirects its stdout/stderr straight to
``LOG_PATH`` via ``StandardOutput=append:`` — this module just greps the latest match
out of that file so ``/tunnel`` (admin bot) can hand it back on demand instead of
someone having to SSH in and read the journal.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "cloudflared_tunnel.log"
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def get_tunnel_url() -> "str | None":
    if not LOG_PATH.exists():
        return None
    matches = _URL_RE.findall(LOG_PATH.read_text(errors="replace"))
    return matches[-1] if matches else None
