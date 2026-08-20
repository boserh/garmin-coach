"""Diagnose an Open-Meteo failure from the box that's actually failing (the Pi).

``app/weather.py`` deliberately swallows every weather error — the report goes out
without weather rather than not at all — so a persistent 503 leaves one warning line
and no way to tell *what kind* of 503 it is. This script asks the same endpoints the
app asks, from the same host, and prints everything the app throws away.

Usage (on the Pi, no venv needed beyond ``requests``)::

    ./venv/bin/python -m scripts.weather_probe                 # both hosts, v4 + v6
    ./venv/bin/python -m scripts.weather_probe --repeat 10     # is it constant or flaky?
    ./venv/bin/python -m scripts.weather_probe --lat 54.35 --lon 18.65

Run it **as the service user, with the service's environment**, when curl from the same
box succeeds — that gap is the whole question::

    sudo systemctl show garmin-bot.service -p Environment
    sudo tr '\0' '\n' < /proc/$(pgrep -f 'bot.main')/environ | grep -i proxy

Zero cost: no Claude, no Garmin, no database. Reading the output:

- **curl 200 but this script 503** — the network is fine and the caller is the variable.
  Compare the two UA rows: if only `python-requests/...` fails, a front end is shedding
  the library UA (hence the explicit UA in ``app/weather.py``). If both fail while curl
  works, look at the proxy variables printed below — a proxy in the service environment
  that an interactive shell doesn't have answers 503 all by itself.

- **Same failure on IPv4 and IPv6** — the address isn't the variable. If the body is
  Open-Meteo's own JSON (``{"error": true, "reason": "... limit exceeded"}``) it is
  telling you it's throttling this IP; if it's an HTML page or a ``server:`` header
  that isn't Open-Meteo's, something between you and it answered (ISP, proxy, DNS).
- **IPv6 fails, IPv4 works** — the classic Pi case, and not a ban at all: the box
  prefers a broken AAAA route while your phone goes out over v4. Fix the route, or
  force v4 (``precedence ::ffff:0:0/96 100`` in /etc/gai.conf).
- **Intermittent across ``--repeat``** — a real transient blip; the retry/backoff in
  ``weather._get_json`` is the fix, nothing to chase.
- **Geocoding host fine, forecast host failing** — one service is having a moment;
  an IP-level block would hit both.
"""
import argparse
import os
import socket
import time

import requests

_HOSTS = {
    "forecast": ("https://api.open-meteo.com/v1/forecast", {
        "timezone": "auto", "forecast_days": 7, "daily": "temperature_2m_max",
    }),
    "geocoding": ("https://geocoding-api.open-meteo.com/v1/search", {
        "name": "Gdansk", "count": 1, "format": "json",
    }),
}

# The app's UA (what weather.py now sends), the library default it used to send, and a
# curl-shaped one — the failing case is usually one row of these three, not all of them.
_AGENTS = {
    "app  ": "bihun-coach/1.0 (+https://github.com/boserh/garmin-coach)",
    "lib  ": f"python-requests/{requests.__version__}",
    "curl ": "curl/8.5.0",
}

_orig_getaddrinfo = socket.getaddrinfo


def _force_family(family):
    """Make every lookup in this process resolve to one address family only — the way
    to ask "is it the v6 path?" without touching system config."""
    def gai(host, port, f=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    socket.getaddrinfo = _orig_getaddrinfo if family == socket.AF_UNSPEC else gai


def _resolve(host: str) -> None:
    for family, label in ((socket.AF_INET, "A   "), (socket.AF_INET6, "AAAA")):
        try:
            addrs = sorted({r[4][0] for r in _orig_getaddrinfo(host, 443, family)})
            print(f"  {label} {', '.join(addrs)}")
        except OSError as e:
            print(f"  {label} — {e}")


def _probe(url: str, params: dict, family, label: str, agent: str) -> None:
    _force_family(family)
    t0 = time.monotonic()
    try:
        r = requests.get(url, params=params, timeout=8,
                         headers={"User-Agent": agent})
        ms = (time.monotonic() - t0) * 1000
        head = "; ".join(f"{h}={r.headers[h]}" for h in
                         ("server", "retry-after", "cf-ray", "x-cache")
                         if h in r.headers)
        print(f"  {label} {r.status_code} in {ms:.0f}ms  {head}")
        if r.status_code >= 400:
            print(f"       body: {' '.join(r.text.split())[:300]}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        print(f"  {label} FAILED in {ms:.0f}ms — {type(e).__name__}: {e}")
    finally:
        _force_family(socket.AF_UNSPEC)


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe Open-Meteo from this host.")
    ap.add_argument("--lat", type=float, default=54.35227)
    ap.add_argument("--lon", type=float, default=18.64912)
    ap.add_argument("--repeat", type=int, default=3, help="attempts per host/family")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between attempts")
    args = ap.parse_args()

    # requests honours these silently; a systemd unit can carry one an interactive shell
    # never sees, and then "same box, same URL" is not the same request at all.
    # Only the four requests actually honours — a box can carry a dozen tool-specific
    # PROXY variables that have nothing to do with this call.
    seen = {k: v[:60] for k, v in os.environ.items()
            if k.lower() in ("http_proxy", "https_proxy", "no_proxy", "all_proxy")}
    print("proxy env: " + ("; ".join(f"{k}={v}" for k, v in sorted(seen.items()))
                           if seen else "none"))

    for name, (url, params) in _HOSTS.items():
        host = url.split("/")[2]
        print(f"\n{name} — {host}")
        _resolve(host)
        call = dict(params)
        if name == "forecast":
            call |= {"latitude": args.lat, "longitude": args.lon}
        for family, fam_label in ((socket.AF_UNSPEC, "auto"),
                                  (socket.AF_INET, "v4  "),
                                  (socket.AF_INET6, "v6  ")):
            for ua_label, agent in _AGENTS.items():
                for _ in range(max(1, args.repeat)):
                    _probe(url, call, family, f"{fam_label} ua={ua_label}", agent)
                    time.sleep(args.delay)


if __name__ == "__main__":
    main()
