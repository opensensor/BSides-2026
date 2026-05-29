/*
 * Live caption overlay for the deck.
 * Connects to the local faster-whisper WebSocket (captions/transcribe.py) and
 * renders minimalist, high-contrast captions at the bottom of the stage.
 *
 *   c  -> toggle captions on/off
 *
 * Reconnects automatically if the server isn't up yet / drops.
 */
(function () {
  'use strict';

  var WS_URL = 'ws://127.0.0.1:8765';
  var INTERIM_HIDE_MS = 1800; // a partial with no follow-up clears quickly
  var REPEAT_WINDOW_MS = 4000;// drop an identical final re-sent within this window
  var MAX_CHARS = 180;        // keep the line readable; trim from the front

  // Finals clear after roughly the time it takes to read them, so a short line
  // doesn't linger as long as a full sentence. Clamped to a sane range.
  function finalHideMs(text) {
    var words = text.trim() ? text.trim().split(/\s+/).length : 0;
    return Math.min(6000, Math.max(2200, 800 + words * 240));
  }

  // ----- styles -------------------------------------------------------------
  var css = document.createElement('style');
  css.textContent = [
    '#cc-overlay{position:fixed;left:50%;bottom:4.5vh;transform:translateX(-50%);',
    'z-index:9999;max-width:80vw;pointer-events:none;text-align:center;',
    'opacity:0;transition:opacity .35s ease;}',
    '#cc-overlay.on{opacity:1;}',
    '#cc-overlay .cc-text{display:inline-block;padding:.45em .9em;border-radius:14px;',
    'background:rgba(8,12,16,.82);color:#fff;font-weight:600;line-height:1.32;',
    "font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;",
    'font-size:clamp(22px,2.4vw,40px);letter-spacing:.005em;',
    'box-shadow:0 10px 40px rgba(0,0,0,.45);backdrop-filter:blur(4px);',
    '-webkit-backdrop-filter:blur(4px);border:1px solid rgba(255,255,255,.08);}',
    '#cc-overlay .cc-text:empty{display:none;}',
    '#cc-overlay.interim .cc-text{color:#dfe7ee;}',   // partial = slightly dim
    '#cc-badge{position:fixed;right:14px;bottom:12px;z-index:9999;',
    'font:600 12px/1 ui-monospace,Menlo,monospace;letter-spacing:.12em;',
    'text-transform:uppercase;padding:5px 9px;border-radius:7px;pointer-events:none;',
    'background:rgba(8,12,16,.7);color:#9aa7b2;opacity:0;transition:opacity .3s;}',
    '#cc-badge.show{opacity:1;}',
    '#cc-badge .dot{display:inline-block;width:7px;height:7px;border-radius:50%;',
    'background:#ff5a4d;margin-right:7px;vertical-align:middle;}',
    '#cc-badge.live .dot{background:#00e38c;}'
  ].join('');
  document.head.appendChild(css);

  // ----- dom ----------------------------------------------------------------
  var overlay = document.createElement('div');
  overlay.id = 'cc-overlay';
  var textEl = document.createElement('span');
  textEl.className = 'cc-text';
  overlay.appendChild(textEl);

  var badge = document.createElement('div');
  badge.id = 'cc-badge';
  badge.innerHTML = '<span class="dot"></span><span class="lbl">CC off</span>';
  var badgeLbl = badge.querySelector('.lbl');

  function mount() {
    document.body.appendChild(overlay);
    document.body.appendChild(badge);
  }
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);

  // ----- state --------------------------------------------------------------
  var enabled = true;        // captions visible by default
  var connected = false;
  var hideTimer = null;
  var lastFinalText = '';    // last committed line (for repeat suppression)
  var lastFinalAt = 0;       // when we last saw it

  function clamp(s) {
    if (s.length <= MAX_CHARS) return s;
    return '…' + s.slice(s.length - MAX_CHARS);
  }

  function show(text, interim) {
    if (!enabled) return;
    var line = clamp(text);

    // The server resets its dedup state on every commit, so an identical final
    // can be re-broadcast moments later (next utterance's first interim, or a
    // re-committed line). Suppress it so the caption doesn't pop back up.
    if (!interim) {
      var now = Date.now();
      if (line === lastFinalText && (now - lastFinalAt) < REPEAT_WINDOW_MS) {
        lastFinalAt = now;       // keep the window rolling, but don't re-show
        return;
      }
      lastFinalText = line;
      lastFinalAt = now;
    }

    textEl.textContent = line;
    overlay.classList.toggle('interim', !!interim);
    overlay.classList.add('on');
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(function () { overlay.classList.remove('on'); },
                           interim ? INTERIM_HIDE_MS : finalHideMs(line));
  }

  function refreshBadge() {
    badge.classList.toggle('live', enabled && connected);
    badgeLbl.textContent = !enabled ? 'CC off'
                         : connected ? 'CC live' : 'CC …';
    // show the badge briefly on state change
    badge.classList.add('show');
    setTimeout(function () {
      if (enabled) return;             // keep visible while off as a reminder
      badge.classList.remove('show');
    }, 2500);
  }

  // ----- websocket with auto-reconnect -------------------------------------
  function connect() {
    var ws;
    try { ws = new WebSocket(WS_URL); }
    catch (e) { return setTimeout(connect, 2000); }

    ws.onopen = function () { connected = true; refreshBadge(); };
    ws.onclose = function () { connected = false; refreshBadge(); setTimeout(connect, 2000); };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'clear') { overlay.classList.remove('on'); return; }
      if (msg.type === 'interim') show(msg.text, true);
      else if (msg.type === 'final') show(msg.text, false);
    };
  }
  connect();

  // ----- toggle key ---------------------------------------------------------
  window.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (e.key === 'c' || e.key === 'C') {
      enabled = !enabled;
      if (!enabled) overlay.classList.remove('on');
      refreshBadge();
    }
  });

  refreshBadge();
})();
