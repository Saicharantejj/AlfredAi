/**
 * Alfred Shortcut Engine  v3
 * ──────────────────────────
 * "Hey Alfred" voice wake word only.
 * Persists via localStorage + cookie-authenticated /shortcuts backend.
 *
 * Public API:
 *   AlfredShortcuts.init()     – restore state + arm listener
 *   AlfredShortcuts.stop()     – tear down listener
 *   AlfredShortcuts.test()     – show wake toast (debug)
 *   AlfredShortcuts.getMode()  – 'off' | 'voice'
 *   AlfredShortcuts.setMode(m) – change + persist
 */

const AlfredShortcuts = (() => {
  /* ── State ──────────────────────────────────────────── */
  let _mode    = localStorage.getItem('alfred_shortcut_mode') || 'off';
  let _running = false;
  let _armed   = false;
  let _paused  = false;  // true while Alfred TTS is playing — blocks auto-restart
  let _pauseTimer = null;
  let _sr        = null;
  let _srRunning = false;

  let _toast = null;

  /* ── Toast ──────────────────────────────────────────── */
  function _ensureToast() {
    if (document.getElementById('alfred-wake-toast')) {
      _toast = document.getElementById('alfred-wake-toast'); return;
    }
    const t = document.createElement('div');
    t.id = 'alfred-wake-toast';
    t.innerHTML = `<div class="awt-inner"><div class="awt-dot"></div><span class="awt-label">Alfred is listening…</span></div>`;
    const s = document.createElement('style');
    s.textContent = `
      #alfred-wake-toast{
        position:fixed;bottom:32px;left:50%;transform:translateX(-50%) translateY(20px);
        background:hsl(240,28%,10%);color:#fff;
        padding:12px 22px;border-radius:40px;display:flex;align-items:center;
        box-shadow:0 8px 32px hsla(248,60%,20%,0.5);
        opacity:0;z-index:99999;pointer-events:none;
        transition:opacity 0.3s cubic-bezier(0.16,1,0.3,1),
                   transform 0.4s cubic-bezier(0.34,1.56,0.64,1);
        font-family:'Geist',system-ui,sans-serif;font-size:13px;letter-spacing:0.2px;
        border:0.5px solid rgba(255,255,255,0.08);
      }
      #alfred-wake-toast.visible{opacity:1;transform:translateX(-50%) translateY(0)}
      .awt-inner{display:flex;align-items:center;gap:10px}
      .awt-dot{
        width:8px;height:8px;border-radius:50%;
        background:hsl(221,90%,60%);flex-shrink:0;
        animation:awtPulse 1s ease-in-out infinite;
      }
      @keyframes awtPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:0.5;transform:scale(0.6)}}
    `;
    document.head.appendChild(s);
    document.body.appendChild(t);
    _toast = t;
  }

  function _showToast(msg = 'Alfred is listening…', duration = 2200) {
    _ensureToast();
    _toast.querySelector('.awt-label').textContent = msg;
    _toast.classList.add('visible');
    clearTimeout(_toast._hideTimer);
    _toast._hideTimer = setTimeout(() => _toast.classList.remove('visible'), duration);
  }

  /* ── Chip (dashboard nav) ───────────────────────────── */
  function _refreshChip() {
    const chip  = document.getElementById('shortcutChip');
    const label = document.getElementById('shortcutChipLabel');
    if (!chip || !label) return;
    label.textContent = _mode === 'voice' ? '🎙 Hey Alfred' : 'Shortcuts off';
    chip.classList.toggle('active', _mode === 'voice');
    
    // Toggle a 'listening' class if actually armed and mic is running
    const isListening = _srRunning && _running && !_paused;
    chip.classList.toggle('listening', isListening);
  }

  /* ── Wake Action ────────────────────────────────────── */
  function _wake() {
    console.log('[Alfred] Wake word detected');
    _showToast('Alfred is listening…', 2500);
    if (window.location.pathname === '/chat-ui') {
      if (typeof toggleMic === 'function') setTimeout(toggleMic, 400);
    } else {
      sessionStorage.setItem('alfred_auto_listen', '1');
      setTimeout(() => { window.location.href = '/chat-ui'; }, 500);
    }
  }

  /* ── Voice listener ─────────────────────────────────── */
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

  function _startVoice() {
    if (!SR || _srRunning) return;
    try {
      _sr = new SR();
      _sr.continuous     = true;
      _sr.interimResults = true;
      _sr.lang           = 'en-IN';

      _sr.onstart  = () => { _srRunning = true; _refreshChip(); };
      _sr.onend    = () => {
        _srRunning = false;
        _refreshChip();
        // Only auto-restart if we're still active and NOT paused for TTS
        if (_running && !_paused && _mode === 'voice') setTimeout(_startVoice, 400);
      };
      _sr.onerror  = (e) => {
        _srRunning = false;
        _refreshChip();
        console.warn('[Alfred] SR Error:', e.error);
        if (e.error !== 'aborted' && e.error !== 'not-allowed' && _running) setTimeout(_startVoice, 1500);
      };
      _sr.onresult = (e) => {
        const transcript = Array.from(e.results)
          .map(r => r[0].transcript.toLowerCase()).join(' ');
        
        // Match 'hey alfred' with optional comma/space
        if (/hey[,\s]*alfred/i.test(transcript)) {
          console.log('[Alfred] Wake pattern matched:', transcript);
          _paused = true;   // block onend auto-restart
          _sr.abort();      // close mic now
          _wake();
        }
      };
      _sr.start();
    } catch (err) {
      console.warn('[Alfred] SpeechRecognition error:', err);
    }
  }

  function _stopVoice() {
    _srRunning = false;
    if (_sr) { try { _sr.abort(); } catch(_) {} _sr = null; }
  }

  /* ── Gesture gate ───────────────────────────────────── */
  function _startListeners() {
    if (_armed || _mode === 'off') return;
    _armed   = true;
    _running = true;
    _startVoice();
  }

  function _attachGestureGate() {
    if (_mode === 'off') return;
    const handler = () => {
      document.removeEventListener('click',   handler);
      document.removeEventListener('keydown', handler);
      _startListeners();
    };
    document.addEventListener('click',   handler, { passive: true });
    document.addEventListener('keydown', handler, { passive: true });
  }

  /* ── Persist ────────────────────────────────────────── */
  function _persist(mode) {
    localStorage.setItem('alfred_shortcut_mode', mode);
    fetch('/shortcuts', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body:    JSON.stringify({ mode, threshold: 0.035 })
    }).catch(() => {});
  }

  /* ── Public API ─────────────────────────────────────── */
  function init() {
    _refreshChip();
    _attachGestureGate();

    // Background sync from backend
    fetch('/shortcuts', { credentials: 'same-origin' }).then(r => r.json()).then(data => {
      if (data.mode && data.mode !== _mode) {
        _mode = (data.mode === 'voice') ? 'voice' : 'off';
        localStorage.setItem('alfred_shortcut_mode', _mode);
        _refreshChip();
        if (_armed) stop(); 
        _attachGestureGate();
      }
    }).catch(() => {});

    // Auto-listen after wake-navigate
    if (sessionStorage.getItem('alfred_auto_listen') === '1') {
      sessionStorage.removeItem('alfred_auto_listen');
      setTimeout(() => { if (typeof toggleMic === 'function') toggleMic(); }, 800);
    }
  }

  function stop() {
    _running = false;
    _armed   = false;
    _stopVoice();
  }

  function setMode(mode) {
    const m  = (mode === 'voice') ? 'voice' : 'off';
    _mode    = m;
    _armed   = false;
    _running = false;
    _stopVoice();
    _persist(m);
    _refreshChip();
    if (m === 'voice') {
      // User just clicked — that IS the gesture, start immediately
      _armed   = true;
      _running = true;
      _startVoice();
    }
  }

  function getMode() { return _mode; }

  /**
   * Hard-stop the mic immediately — call this BEFORE speechSynthesis.speak().
   * The auto-restart in _sr.onend is blocked by _paused=true.
   */
  function pauseForSpeech(estimatedMs = 4000) {
    _paused = true;
    clearTimeout(_pauseTimer);
    // Abort the active SR session — mic goes silent immediately
    if (_sr) { try { _sr.abort(); } catch(_) {} }
    // Safety fallback: if onend never fires the resume, do it automatically
    _pauseTimer = setTimeout(resumeAfterSpeech, estimatedMs + 2000);
  }

  /**
   * Re-open the mic. Call this from u.onend / u.onerror with a small delay.
   */
  function resumeAfterSpeech() {
    clearTimeout(_pauseTimer);
    _paused = false;
    if (_running && _mode === 'voice') setTimeout(_startVoice, 300);
  }

  /**
   * Call this after a conversation ends and voice output is disabled,
   * so the wake SR restarts even without a speak() → resumeAfterSpeech() cycle.
   */
  function resumeFromWake(delayMs = 800) {
    clearTimeout(_pauseTimer);
    _paused = false;
    if (_running && _mode === 'voice') setTimeout(_startVoice, delayMs);
  }

  function test() {
    _showToast('Alfred is listening…', 2500);
    _ensureToast();
    const dot = _toast.querySelector('.awt-dot');
    if (dot) { dot.style.background = 'hsl(142,71%,42%)'; setTimeout(() => dot.style.background = '', 1000); }
  }

  function isListening() { return _srRunning && _running && !_paused; }

  window.AlfredShortcuts = { init, stop, setMode, getMode, pauseForSpeech, resumeAfterSpeech, resumeFromWake, test, isListening };
  return window.AlfredShortcuts;
})();
