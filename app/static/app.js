// Global double-submit guard: any HTML form on the site disables its
// submit controls the instant it's submitted, so a slow request (most are
// backed by a Claude call) can't be fired twice by an impatient click.
// A form can opt out with data-no-busy="1".
(function () {
  'use strict';

  function busyLabel(el) {
    return el.getAttribute('data-busy-text') || 'Зачекайте…';
  }

  function lockForm(form) {
    if (form.dataset.submitting === '1') return false;
    form.dataset.submitting = '1';
    var controls = form.querySelectorAll('button:not(:disabled), input[type="submit"]:not(:disabled)');
    controls.forEach(function (el) {
      el.dataset.wasEnabled = '1';
      el.disabled = true;
      if (el.tagName === 'BUTTON' && el.querySelector('*') === null) {
        el.dataset.origText = el.textContent;
        el.textContent = busyLabel(el);
      }
    });
    return true;
  }

  function unlockForm(form) {
    form.dataset.submitting = '';
    form.querySelectorAll('[data-was-enabled="1"]').forEach(function (el) {
      el.disabled = false;
      if (el.dataset.origText !== undefined) {
        el.textContent = el.dataset.origText;
        delete el.dataset.origText;
      }
      delete el.dataset.wasEnabled;
    });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.submitting === '1') { e.preventDefault(); return; }
    if (form.dataset.noBusy === '1') return;
    lockForm(form);
  }, true);

  // Safety net: a bfcache restore (browser back button) must not leave
  // buttons stuck disabled forever if the request never actually landed.
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-submitting="1"]').forEach(unlockForm);
  });
})();

// UI-01: ONE chart tooltip for every `.chart[data-pts]` on the site.
//
// This used to be four near-identical inline <script>s (dashboard, activity, daily,
// detail) that had already drifted apart — the activity page formatted cadence and
// elevation as heart rate, and the edge clamp was 28px in two of them and 42px in the
// others. Worse, all four listened only for `mousemove`, so on the phone the app is
// designed around the charts were decoration: no way to read a value at a point.
//
// Pointer Events cover mouse, finger and stylus with one code path. The finger gets
// pointer capture so a drag survives leaving the card, and the bubble stays up after
// the finger lifts (a phone has no `mouseleave`, and a tooltip that vanishes on release
// reads as "nothing happened") until the next tap somewhere else.
(function () {
  'use strict';

  function pace(v) {
    var m = Math.floor(v), s = Math.round((v - m) * 60);
    if (s === 60) { m++; s = 0; }
    return m + ':' + (s < 10 ? '0' : '') + s + '/км';
  }

  // Keyed by the `data-fmt` the server puts on the card (app/charts.py).
  var FORMATS = {
    pace: pace,
    speed: function (v) { return v.toFixed(1) + ' км/год'; },
    power: function (v) { return Math.round(v) + ' Вт'; },
    elev: function (v) { return Math.round(v) + ' м'; },
    cadence: function (v) { return Math.round(v) + ' кр/хв'; },
    hr: function (v) { return Math.round(v) + ' уд'; },
    f1: function (v) { return v.toFixed(1); },
    int: function (v) { return String(Math.round(v)); }
  };

  // What the point is: a date on a trend sparkline, a distance on an activity series.
  function pointLabel(p) {
    if (p.lbl) return p.lbl;
    if (p.d !== null && p.d !== undefined) return p.d.toFixed(2) + ' км';
    return '';
  }

  function tooltipText(fmt, p) {
    var f = FORMATS[fmt] || FORMATS.int, lbl = pointLabel(p);
    return f(p.v) + (lbl ? ' · ' + lbl : '');
  }

  var charts = [];

  function hideAll(except) {
    charts.forEach(function (c) { if (c !== except) c.hide(); });
  }

  function setup(ch) {
    var pts;
    try { pts = JSON.parse(ch.dataset.pts); } catch (e) { return; }
    if (!pts || !pts.length) return;
    var wrap = ch.querySelector('.cwrap'),
        tip = ch.querySelector('.tip'),
        guide = ch.querySelector('.guide');
    if (!wrap || !tip || !guide) return;

    var fmt = ch.dataset.fmt, idx = -1;

    function hide() {
      idx = -1;
      tip.style.display = 'none';
      tip.textContent = '';
      guide.style.display = 'none';
    }

    function draw(i) {
      if (i < 0 || i >= pts.length) return;
      idx = i;
      var p = pts[i], r = wrap.getBoundingClientRect(), x = p.x * r.width;
      guide.style.left = x + 'px';
      guide.style.display = 'block';
      tip.style.display = 'block';
      tip.textContent = tooltipText(fmt, p);
      // Clamp by the bubble's own half-width instead of a guessed constant, so a long
      // "4:52/км · 12.40 км" is held inside the card just like a bare "62".
      var half = Math.min((tip.offsetWidth || 84) / 2, r.width / 2);
      tip.style.left = Math.min(Math.max(x, half), r.width - half) + 'px';
    }

    function nearest(clientX) {
      var r = wrap.getBoundingClientRect();
      if (!r.width) return -1;
      var frac = (clientX - r.left) / r.width, best = -1, bd = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i].x - frac);
        if (d < bd) { bd = d; best = i; }
      }
      return best;
    }

    // Focusable so the whole thing is reachable (and readable) from a keyboard.
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'group');
    tip.setAttribute('aria-live', 'polite');

    wrap.addEventListener('pointerdown', function (e) {
      // Capture keeps a finger drag alive past the card's edge. Old WebKit without it
      // degrades gracefully: the tooltip follows while the finger stays inside.
      if (wrap.setPointerCapture) {
        try { wrap.setPointerCapture(e.pointerId); } catch (err) { /* not capturable */ }
      }
      draw(nearest(e.clientX));
    });
    wrap.addEventListener('pointermove', function (e) {
      // A mouse reports moves while merely hovering (the desktop behaviour we keep);
      // a finger only reports them while it's down, so no button check is needed.
      draw(nearest(e.clientX));
    });
    wrap.addEventListener('pointerup', function (e) {
      if (wrap.releasePointerCapture) {
        try { wrap.releasePointerCapture(e.pointerId); } catch (err) { /* already gone */ }
      }
      // Deliberately NOT hiding: on touch the reading has to survive the lift.
    });
    wrap.addEventListener('pointercancel', hide);
    wrap.addEventListener('pointerleave', function (e) {
      // Only the mouse has a meaningful "left the element" — a finger leaves by lifting.
      if (e.pointerType === 'mouse') hide();
    });

    wrap.addEventListener('keydown', function (e) {
      var to = null;
      if (e.key === 'ArrowRight') to = idx < 0 ? 0 : Math.min(idx + 1, pts.length - 1);
      else if (e.key === 'ArrowLeft') to = idx < 0 ? pts.length - 1 : Math.max(idx - 1, 0);
      else if (e.key === 'Home') to = 0;
      else if (e.key === 'End') to = pts.length - 1;
      else if (e.key === 'Escape') { hide(); return; }
      if (to === null) return;
      e.preventDefault();  // ←/→ would otherwise scroll the page sideways
      draw(to);
    });
    wrap.addEventListener('blur', hide);

    var api = {hide: hide, redraw: function () { if (idx >= 0) draw(idx); }};
    charts.push(api);
    // A tap that starts on another chart (or anywhere else) closes this one — the
    // phone's stand-in for `mouseleave`.
    wrap.addEventListener('pointerdown', function () { hideAll(api); });
  }

  function init() {
    document.querySelectorAll('.chart[data-pts]').forEach(setup);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // A tap outside every chart dismisses whatever is open. Capture phase, so it runs
  // before a chart's own handler re-opens it.
  document.addEventListener('pointerdown', function (e) {
    var t = e.target;
    if (!(t && t.closest && t.closest('.chart[data-pts]'))) hideAll(null);
  }, true);

  // A shown tooltip is positioned in pixels — a rotate/resize would leave it stranded.
  window.addEventListener('resize', function () {
    charts.forEach(function (c) { c.redraw(); });
  });

  // Test seam: the formatting rules are pure and worth checking without a browser
  // (tests/test_chart_tooltip.py runs them under node).
  window.chartTip = {formats: FORMATS, label: pointLabel, text: tooltipText};
})();

// UI-03: service-worker registration, sign-out purge, and the install button.
//
// Everything here is optional by construction: a browser without service workers, or a
// user who declines the install prompt, gets exactly the app as it was before.
(function () {
  'use strict';

  function assetVersion() {
    var meta = document.querySelector('meta[name="asset-v"]');
    return meta ? meta.getAttribute('content') : '';
  }

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      // The ?v= is the same asset digest the CSS/JS links carry: a deploy changes the
      // worker's script URL, so the browser fetches and installs the new one instead of
      // keeping a wedged copy.
      // scope '/' (allowed by the Service-Worker-Allowed header the app sends) — a
      // worker under /static/ would otherwise only ever see /static/ requests.
      navigator.serviceWorker.register('/static/sw.js?v=' + assetVersion(), {scope: '/'})
        .catch(function () { /* unsupported, or blocked by policy — nothing breaks */ });
    });

    // Signing out must take the cached personal pages with it. Sent on submit so the
    // worker gets it before the navigation; the worker ALSO purges when it sees the
    // POST, because a message can lose that race.
    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (form instanceof HTMLFormElement &&
          form.getAttribute('action') === '/logout' &&
          navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage({type: 'purge'});
      }
    }, true);

    // The page is only served from the cache when the server couldn't be reached (see
    // sw.js: network-first). If the request lands after we'd already given up, offer the
    // fresh copy rather than letting the reader sit on a page we've labelled stale.
    navigator.serviceWorker.addEventListener('message', function (e) {
      if (!e.data || e.data.type !== 'fresh') return;
      var banner = document.querySelector('.banner--warn .btext');
      if (!banner || banner.dataset.swRefreshed === '1') return;
      banner.dataset.swRefreshed = '1';
      var link = document.createElement('a');
      link.href = window.location.href;
      link.className = 'blink';
      link.textContent = 'Зʼявились свіжі дані — оновити →';
      // A text node, or the link glues itself to the sentence: "…станом на 13:07.Зʼявились".
      banner.appendChild(document.createTextNode(' '));
      banner.appendChild(link);
    });
  }

  // The install prompt: Chrome fires this only once the PWA criteria are met, so the
  // button appears when installing is actually possible and stays hidden otherwise.
  var deferredPrompt = null;
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferredPrompt = e;
    var btn = document.getElementById('install-pwa');
    if (btn) btn.hidden = false;
  });

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('#install-pwa');
    if (!btn || !deferredPrompt) return;
    e.preventDefault();
    deferredPrompt.prompt();
    deferredPrompt.userChoice.then(function () {
      deferredPrompt = null;
      btn.hidden = true;
    });
  });
})();
