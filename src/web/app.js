/* =====================================================================
   Focus Agent — app shell logic (P3-01)

   Organised as clear component factories so it maps 1:1 to React later.
   Each factory owns its DOM + behaviour and exposes a small API.
   Vanilla JS, no build step; the app is served same-origin at "/".

   Component  ->  future React component
   ---------------------------------------------------------------
   AgentState ->  useAgentState() hook (a small state machine)
   Eyes       ->  <Eyes state={agentState} />  (renders + blinks)
   Mic        ->  <Mic onActivate={...} state={agentState} />
   BlurPanel  ->  <BlurPanel />  (frosted conversation surface)
   Stage      ->  <Stage open={...} />  (park morph + the horizon plan view)
   Settings   ->  <SettingsModal /> + a FocusSettings context/store
   ===================================================================== */
(function () {
  "use strict";

  /* ---------- workspace identity (P14) ----------
     Guest by default, never a login wall. A first visit mints a crypto-random
     per-browser workspace id and keeps it in localStorage, so two browsers
     never share state. Google sign-in upgrades the browser to a stable
     per-user id (the ?signin=connected return below persists it; the signed
     HTTP-only session cookie is the actual credential). ?ws=... overrides for
     the demo tooling, so ws_demo stays reachable on demand. */
  var WS_FROM_QUERY = false;          // a ?ws= override is a look, not a home
  var WS = (function () {
    var qs = window.location.search || "";
    var m = /[?&]ws=([A-Za-z0-9_-]+)/.exec(qs);
    if (m) {
      if (/[?&]signin=connected/.test(qs)) {
        // Back from Google sign-in: this browser now lives in the signed-in
        // workspace. Persist the binding; the cookie holds the credential.
        try { localStorage.setItem("focus.workspace", m[1]); } catch (_) {}
      } else {
        WS_FROM_QUERY = true;
      }
      return m[1];
    }
    try {
      var saved = localStorage.getItem("focus.workspace");
      if (saved) return saved;
      if (!(window.crypto && window.crypto.getRandomValues)) throw new Error("no crypto");
      var bytes = new Uint8Array(12);
      window.crypto.getRandomValues(bytes);
      var id = "g_" + Array.prototype.map.call(bytes, function (b) {
        return ("0" + b.toString(16)).slice(-2);
      }).join("");
      localStorage.setItem("focus.workspace", id);
      return id;
    } catch (_) {
      // No storage or no crypto (ancient/locked-down browser): the shared
      // demo room is the only honest fallback left.
      return "ws_demo";
    }
  })();
  var reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- P12-02: which thinking profile this request asks for ----------
     The choice matters server-side, so it travels as an optional per-request
     field rather than server session state. Read fresh on every call, so a
     switch flipped mid-session lands on the very next request. A missing or
     unrecognised value is fast on the server, so this can never break a turn. */
  function thinkingMode() {
    try {
      return (window.FocusSettings && window.FocusSettings.get("deepThinking")) ? "deep" : "fast";
    } catch (_) { return "fast"; }
  }
  function deepModeOn() { return thinkingMode() === "deep"; }

  /* ---------- tiny fetch helper (same-origin API) ---------- */
  function api(path, opts) {
    return fetch("/v1/workspaces/" + WS + path, opts).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* P15-00 — tell the server which day the user is living in.
     Every "today" the server computes (the evening check-in, the morning
     brief, the streak, the `today` field in /details) is a question about the
     USER'S calendar day, and the server has no way to know that on its own.
     Left unset it falls back to UTC, which silently misfires for anyone west
     of Greenwich: the UTC date advances at 17:00 in Los Angeles, so an evening
     check-in would look at the wrong day and find nothing to ask about.
     Fire-and-forget on load. A failure here is not worth bothering the user
     with, because the server degrades to UTC exactly as it always did. */
  function reportTimezone() {
    var tz;
    try {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch (e) { return; }
    if (!tz) return;
    api("/profile/timezone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ timezone: tz }),
    }).catch(function () { /* degrade to UTC, never nag */ });
  }

  // Stable commitment colour: djb2-xor over the id picks a hue inside the
  // sage-to-teal band the Nocturne palette lives in (140–219), fixed
  // sat/light so the dot reads against it. Module-scope because the
  // horizon chips AND the Now surface (P9-07) both wear the same dot.
  function commitmentColor(id) {
    if (!id) return "var(--faint)";
    var h = 5381;
    for (var i = 0; i < id.length; i++) h = ((h * 33) ^ id.charCodeAt(i)) >>> 0;
    return "hsl(" + (140 + (h % 80)) + ", 34%, 58%)";
  }

  /* =====================================================================
     AgentState — the single signal that drives the whole face.
     States: idle | listening | thinking | speaking | asking
     Maps to a useReducer/useState hook in React.
     ===================================================================== */
  function createAgentState(appEl, setHint, onChange) {
    var HINTS = {
      // P11-02a: the idle hint used to name a door that wasn't drawn ("or
      // type"). Now it just points at the three controls sitting under it.
      idle: "Hold the mic to talk, or tap the keyboard to type",
      listening: "Listening…",
      thinking: "Thinking…",
      speaking: "Blink is answering",
      asking: "One quick thing",
    };
    var current = "idle";
    function set(s) {
      current = s;
      appEl.dataset.state = s;
      if (setHint) setHint(HINTS[s] || "");
      if (onChange) onChange(s);
    }
    return {
      set: set,
      get: function () { return current; },
    };
  }

  /* =====================================================================
     Hint (P7-02) — the dock hint line, debounced + cross-faded.
     Rapid churn (mic release fires "Release to edit" and then the compose
     hint within the same tick) used to flash; a 150ms debounce means only
     the hint that SETTLES is shown, and each change swaps through a ~120ms
     opacity dip rather than snapping. pulse() bypasses the debounce for a
     brief attention nudge (the busy guard) and then restores the settled
     hint. Reduced-motion / hidden tabs swap instantly.
     Maps to a useHint() hook in React.
     ===================================================================== */
  function createHint(el) {
    var current = el ? (el.textContent || "").trim() : "";
    var wanted = current;                 // the hint that should win
    var debounce = null, fadeTimer = null, pulseTimer = null;

    function apply(t) {
      if (!el || t === current) return;
      current = t;
      clearTimeout(fadeTimer);
      if (reduce || document.visibilityState === "hidden") {
        el.style.opacity = "";
        el.textContent = t;
        return;
      }
      el.style.opacity = "0";
      fadeTimer = setTimeout(function () {
        el.textContent = t;
        el.style.opacity = "1";
      }, 120);
    }

    function set(t) {
      wanted = t;
      clearTimeout(debounce);
      debounce = setTimeout(function () { apply(wanted); }, 150);
    }

    // Immediate one-off ("One at a time…"), then back to the settled hint.
    function pulse(t) {
      clearTimeout(pulseTimer);
      apply(t);
      pulseTimer = setTimeout(function () { apply(wanted); }, 1400);
    }

    return { set: set, pulse: pulse };
  }

  /* =====================================================================
     Eyes — capsule eyes that blink + breathe. Visual states are pure CSS
     (driven by [data-state]); this component owns the blink scheduler.
     Maps to <Eyes /> with a useEffect blink loop in React.
     ===================================================================== */
  function createEyes(agent, appEl) {
    var eyes = document.querySelectorAll(".eye");
    var emoting = false;   // an emotion holds -> the random blink scheduler waits

    function blink(dbl) {
      // eyes stay squinted while thinking, and don't blink while asleep
      if (agent.get() === "thinking" || appEl.classList.contains("sleeping")) return;
      eyes.forEach(function (e) { e.classList.add("blink"); });
      setTimeout(function () {
        eyes.forEach(function (e) { e.classList.remove("blink"); });
        if (dbl) setTimeout(function () { blink(false); }, 180);
      }, 110);
    }

    function scheduleBlink() {
      var wait = 2600 + Math.random() * 3600;
      setTimeout(function () {
        if (!reduce && !emoting && document.visibilityState === "visible") blink(Math.random() < 0.25);
        scheduleBlink();
      }, wait);
    }
    scheduleBlink();

    // Occasional slow glance while idle — the eyes drift and settle, like a
    // passing thought. Only when awake, idle, and not mid-animation.
    // Touch devices (P7-09): no cursor to track, so the glance fires roughly
    // twice as often — the drift stands in for the pupil life mice get.
    var coarse = matchMedia("(pointer: coarse)").matches;
    var eyesEl = document.querySelector(".eyes");
    function glance() {
      if (reduce || !eyesEl) return;
      if (agent.get() !== "idle" || appEl.classList.contains("sleeping")) return;
      if (eyesEl.classList.contains("glance")) return;
      eyesEl.classList.add("glance");
      eyesEl.addEventListener("animationend", function h() {
        eyesEl.classList.remove("glance");
        eyesEl.removeEventListener("animationend", h);
      });
    }
    function scheduleGlance() {
      var wait = coarse
        ? 4500 + Math.random() * 4500            // touch: every ~4.5-9s
        : 9000 + Math.random() * 9000;           // mouse: every ~9-18s
      setTimeout(function () {
        if (document.visibilityState === "visible") glance();
        scheduleGlance();
      }, wait);
    }
    scheduleGlance();

    // Pupils track the cursor: each glint eases toward wherever the mouse is,
    // so Focus seems to actually look at you. A gentle transition does the
    // smoothing. Skipped for reduced-motion and while asleep.
    var glints = [];
    eyes.forEach(function (e) {
      var g = e.querySelector(".glint");
      if (g) { g.style.transition = "transform 0.18s ease-out"; glints.push({ eye: e, g: g }); }
    });
    // Generous travel so it reads clearly, esp. downward (the eyes sit high on
    // screen, so the cursor is usually far below — separate reach per axis).
    var MAX_X = 18, MAX_Y = 26, REACH_X = 160, REACH_Y = 380;
    function aimPupils(clientX, clientY) {
      if (reduce) return;
      var asleep = appEl.classList.contains("sleeping");
      // Parked (P7-05): under the rig's scale(.42) the same travel reads
      // weak, so boost it — 18px * .42 * 1.6 ≈ 12px on screen, still alive.
      var boost = appEl.classList.contains("viewing") ? 1.6 : 1;
      glints.forEach(function (p) {
        if (asleep) { p.g.style.transform = "translate(0px,0px)"; return; }
        var r = p.eye.getBoundingClientRect();
        var dx = clientX - (r.left + r.width / 2);
        var dy = clientY - (r.top + r.height / 2);
        var ox = Math.max(-1, Math.min(1, dx / REACH_X)) * MAX_X * boost;
        var oy = Math.max(-1, Math.min(1, dy / REACH_Y)) * MAX_Y * boost;
        p.g.style.transform = "translate(" + ox.toFixed(1) + "px," + oy.toFixed(1) + "px)";
      });
    }
    function centerPupils() {
      glints.forEach(function (p) { p.g.style.transform = "translate(0px,0px)"; });
    }
    if (!reduce && glints.length) {
      window.addEventListener("mousemove", function (ev) { aimPupils(ev.clientX, ev.clientY); }, { passive: true });
      window.addEventListener("mouseleave", centerPupils);
    }

    /* --- Emotions (P7-03) -------------------------------------------------
       emote(name, holdMs): put `emote-<name>` on .eyes; the CSS owns the
       look. holdMs > 0 auto-clears; holdMs 0/omitted holds until
       clearEmote() (curious stays up for the whole ask). Newest wins — any
       previous emotion class is dropped first. While a class is on (or
       easing back out) the random blink scheduler is suppressed; blink and
       emotion squash compose via --blink-sy * --emo-sy, so a manual blink
       (wake double-blink) still reads correctly mid-emotion.
       "satisfied" is procedural: one deliberate slow blink, no held class. */
    var EMOTES = ["happy", "wide", "sorry", "curious", "heart",
                  "surprised", "sleepy", "proud", "sheepish", "worried", "celebrate"];
    var emoTimer = null, easeTimer = null;

    function clearEmote(immediate) {
      clearTimeout(emoTimer); emoTimer = null;
      if (!eyesEl) return;
      var had = false;
      EMOTES.forEach(function (n) {
        if (eyesEl.classList.contains("emote-" + n)) had = true;
        eyesEl.classList.remove("emote-" + n);
      });
      if (had && !immediate && !reduce) {
        // keep the slower transition on during the ease-back, then drop it
        eyesEl.classList.add("emote-ease");
        clearTimeout(easeTimer);
        easeTimer = setTimeout(function () {
          eyesEl.classList.remove("emote-ease");
          emoting = false;
        }, 300);
      } else {
        clearTimeout(easeTimer);
        eyesEl.classList.remove("emote-ease");
        emoting = false;
      }
    }

    // One deliberate slow blink — a quiet "done". Temporarily slows the
    // shape transition, runs a single blink, restores. Reduced motion: a
    // normal blink.
    function slowBlink() {
      if (appEl.classList.contains("sleeping")) return;
      if (reduce) { blink(false); return; }
      var shapes = document.querySelectorAll(".eye-shape");
      emoting = true;   // no random blink mid-slow-blink
      shapes.forEach(function (s) {
        s.style.transition = "transform 0.45s ease-in-out, box-shadow 0.4s ease, filter 0.4s ease";
      });
      eyes.forEach(function (e) { e.classList.add("blink"); });
      setTimeout(function () {
        eyes.forEach(function (e) { e.classList.remove("blink"); });
        setTimeout(function () {
          shapes.forEach(function (s) { s.style.transition = ""; });
          emoting = false;
        }, 500);
      }, 480);
    }

    function emote(name, holdMs) {
      if (!eyesEl) return;
      if (name === "satisfied") { clearEmote(true); slowBlink(); return; }
      if (EMOTES.indexOf(name) < 0) return;
      clearEmote(true);                         // newest wins
      eyesEl.classList.add("emote-" + name);
      emoting = true;
      if (holdMs > 0) {
        emoTimer = setTimeout(function () { clearEmote(false); }, holdMs);
      }
    }

    return { blink: blink, emote: emote, clearEmote: clearEmote };
  }

  /* =====================================================================
     Surface — the FLAT, softly-blurred conversation area BELOW the eyes
     (redesign of the old floating BlurPanel; see index.html + style.css).
     The eyes are never overlapped — the surface lives in the lower band.
     Modes:
       compose(onSend, prefill): editable text field + Send (type or review)
       live()/setLiveText():     stream live transcription as the user speaks
       speak():                  Focus's reply, typed out
       pending():                thinking caret
       ask():                    render a ClarifyQuestion control here
     Maps to <Surface mode={...} /> in React.
     ===================================================================== */
  function createSurface(onAction) {
    var panel = document.getElementById("surface");
    var pSaid = document.getElementById("p-said");
    var pWhy = document.getElementById("p-why");
    var pExtra = document.getElementById("p-extra");
    var eyesEl = document.querySelector(".eyes");
    /* P20-02: the artifact rows. #p-art sits ABOVE the words (tool trace,
       search sources) and is built here rather than in index.html so the
       markup contract stays the three rows it always was; the cards that
       land BELOW the words ride #p-extra like reply-actions do. Every mode
       reset clears both — an artifact never outlives its reply. */
    var pArt = document.createElement("div");
    pArt.id = "p-art";
    panel.insertBefore(pArt, pSaid);
    function clearArtifacts() { pArt.innerHTML = ""; }
    var timers = [];
    var activeType = null;   // in-flight type-on: {el, text, done} (P7-01 fix)
    function clearTimers() {
      timers.forEach(clearTimeout); timers = []; activeType = null;
      pExtra.classList.remove("veil");   // never strand a hidden control
    }
    function later(fn, ms) { var t = setTimeout(fn, ms); timers.push(t); return t; }

    function hide() { cancelSwap(); panel.classList.remove("show"); }
    function show() {
      // fresh content always arrives full-size — a reply left receded
      // (auto-minimize, or the horizon interplay) never strands a new turn
      panel.classList.remove("surface-min");
      panel.classList.add("show");
      syncEdgeFades();   // new content: the edge fades re-read the scroll state
    }

    /* --- Mode cross-fade (P7-02) -------------------------------------
       swapMode(fn): ease the current content out (140ms), run fn (the
       actual mode swap), and let the same CSS transition ease the new
       content back in once .swap lifts. Instant when reduced-motion, the
       tab is hidden (timers/transitions throttle there), or the panel
       isn't visible yet — its own show transition covers the entrance.
       A newer mode call cancels a still-queued swap, so only the latest
       content ever lands. */
    /* --- THE CLAIM GATE (P12-04) -------------------------------------
       Nothing paints in here without holding the CURRENT turn.

       This bug has been reported four times. P8-05 introduced a render
       token; P11-12 moved the claim from paint time to hand-off. Both
       hardened one path and left the discipline OPT-IN: `speak(text, done)`
       with the token argument simply left off claimed a fresh one and
       painted, and ask()/pending()/live()/compose() claimed unconditionally.
       Every new call site was therefore one forgotten argument away from
       repainting an older turn over a newer one, and most call sites did
       forget.

       So the default is inverted. EVERY painter takes a claim, and the only
       way to get one is surface.claim() — the single named path that says
       "I am starting a new turn, the room is mine". A painter holding a
       claim the surface has moved past, or holding none at all, does
       NOTHING. A call site that forgets renders silence, never a lie, and
       says so in the console. Safe by construction, not by remembering.

       Claims start at 1, so `undefined` can never accidentally match.

       READ buildWordSpans TOO. The fourth report was not a claim failure at
       all — it was GSAP SplitText restoring the previous reply's text from
       inside the one place reply DOM is built, downstream of every check
       here. The gate is what stops the NEXT class of this bug; the note down
       there is what stopped this one. */
    var renderSeq = 0;
    function claimRender() { return ++renderSeq; }
    function holdsRender(seq) { return seq === renderSeq; }
    // Returns the claim while it is still the current turn, else null.
    // `who` names the painter, so a refusal is debuggable rather than a
    // mysteriously blank surface.
    function gate(seq, who) {
      if (seq === renderSeq) return seq;
      try {
        console.debug("[surface] " + who + " refused: " + (seq == null
          ? "no claim (start a turn with surface.claim())"
          : "stale claim " + seq + ", current is " + renderSeq));
      } catch (_) {}
      return null;
    }
    function still(seq, who) { return gate(seq, who) !== null; }

    /* A reply is ONE thing: its words and its voice live or die together.
       If a paint is refused, the audio that was going to ride it is closed
       in the same beat — otherwise the screen would show one reply while
       the room heard another, which is exactly what the user reported. An
       unplayed PCM stream also holds an audio context and a socket open
       (P12-03b), so this is the cleanup path too. */
    function dropAudio(audio) {
      if (audio && audio.pause) { try { audio.pause(); } catch (_) {} }
    }

    var swapTimer = null, swapFn = null;
    function cancelSwap() {
      clearTimeout(swapTimer); swapFn = null;
      panel.classList.remove("swap");
    }
    function flushSwap() {
      // land a queued swap NOW (timer fire, or the hidden-tab completion path)
      var f = swapFn;
      cancelSwap();
      if (f) f();
    }
    function swapMode(fn) {
      cancelSwap();
      if (reduce || document.visibilityState === "hidden" || !panel.classList.contains("show")) {
        fn(); return;
      }
      swapFn = fn;
      panel.classList.add("swap");
      swapTimer = setTimeout(flushSwap, 140);
    }

    /* --- Scrolled-out edges fade, they do not crop (2026-08-27) ---------
       The surface is a bounded scroll region (max-height in conversation.css),
       so a long reply that has been scrolled used to end in a hard horizontal
       cut at the top edge: it read as a rendering fault rather than as "there
       is more above". The fade itself is a CSS mask (see conversation.css);
       all this owns is the STATE — the mask ramps are 0 unless there really is
       content out of view, because a permanent fade would dim a short reply
       for no reason and look like the same fault it fixes.

       Cheap by construction: the listener is passive, reads two numbers, and
       only touches classList when a boundary is actually crossed. */
    var fadeTop = false, fadeBot = false;
    function syncEdgeFades() {
      var over = panel.scrollHeight - panel.clientHeight;
      // 2px slack: sub-pixel layout should not flicker a fade on and off
      var top = over > 2 && panel.scrollTop > 2;
      var bot = over > 2 && panel.scrollTop < over - 2;
      if (top !== fadeTop) { fadeTop = top; panel.classList.toggle("fade-top", top); }
      if (bot !== fadeBot) { fadeBot = bot; panel.classList.toggle("fade-bot", bot); }
    }
    panel.addEventListener("scroll", syncEdgeFades, { passive: true });
    window.addEventListener("resize", syncEdgeFades, { passive: true });
    // content changes: a reply typing on, a clarify kit mounting, a mode swap.
    // ResizeObserver watches the rows themselves, so growth is caught without
    // any polling; where it is missing the render paths below still call sync.
    if (typeof ResizeObserver === "function") {
      var ro = new ResizeObserver(syncEdgeFades);
      ro.observe(panel); ro.observe(pSaid); ro.observe(pExtra); ro.observe(pArt);
    }

    // Keep the caret / latest word in view while a long reply grows past the
    // surface's max-height (P7-02). Cheap: only scrolls once content overflows.
    function keepLatestVisible() {
      if (panel.scrollHeight > panel.clientHeight + 2) {
        panel.scrollTop = panel.scrollHeight;
      }
      syncEdgeFades();
    }

    function typeInto(el, text, done, seq) {
      // Reduced motion, or a hidden tab (rAF/timers throttle there): land the
      // full text immediately — never leave a half-typed reply + stuck caret.
      if (reduce || document.visibilityState === "hidden") {
        el.textContent = text; keepLatestVisible(); if (done) done(); return;
      }
      var me = { el: el, text: text, done: done };
      activeType = me;
      el.innerHTML = ""; var i = 0;
      (function step() {
        // Two locks, because a half-typed line is a claim about what the
        // agent is saying. The identity check stops one type-on repainting
        // another's text (P9 bugfix); the claim check stops a type-on from
        // outliving the turn it belongs to, so when a turn's voice is cut
        // the words it was writing stop in the same beat.
        if (activeType !== me) return;
        if (!holdsRender(seq)) { activeType = null; return; }
        if (i <= text.length) {
          el.innerHTML = escapeHtml(text.slice(0, i)) + '<span class="caret">▍</span>';
          keepLatestVisible();
          i++; later(step, 22);
        } else { el.textContent = text; activeType = null; if (done) done(); }
      })();
    }

    function escapeHtml(s) {
      return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // Compose mode: user types (or reviews a released transcript). Calls
    // onSend(text) on submit. `prefill` seeds the field with a transcript
    // (hold-to-talk release) so it's editable BEFORE sending — never auto-sent.
    function compose(onSend, prefill, token) {
      var seq = gate(token, "compose");
      if (seq === null) return;
      clearTimers();
      swapMode(function () {
        if (!still(seq, "compose")) return;
        pSaid.textContent = "";
        pWhy.textContent = "";
        clearArtifacts();
        // The compose row (P8-01d, rebuilt by P11-02b): the ruled field and
        // the pill Send. The attach "+" left this row for the dock trio,
        // where it stands beside the mic and the keyboard as one of the
        // three equal ways in — see createDock and index.html.
        pExtra.innerHTML =
          '<div class="compose-row">' +
          '<textarea class="field" id="compose" rows="2" ' +
          'placeholder="Type here, or hold the mic to speak…"></textarea>' +
          '<button class="btn go" id="send">Send</button>' +
          '</div>';
        show();
        var ta = document.getElementById("compose");
        var send = document.getElementById("send");
        if (prefill) ta.value = prefill;
        var submit = function () {
          var v = ta.value.trim();
          if (v) onSend(v);
        };
        send.addEventListener("click", submit);
        ta.addEventListener("keydown", function (e) {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
        });
        setTimeout(function () {
          ta.focus();
          var n = ta.value.length;
          try { ta.setSelectionRange(n, n); } catch (_) {}   // caret at end of prefill
        }, 60);
      });
    }

    // Live mode: while recording, stream interim+final transcription here.
    // setLiveText() updates the text with a caret. No speaker label: the echo
    // margin note carries "this was you" (P11-01 unboxed the reply).
    function live(token) {
      var seq = gate(token, "live");
      if (seq === null) return;
      clearTimers();
      swapMode(function () {
        if (!still(seq, "live")) return;
        pSaid.innerHTML = '<span class="caret">▍</span>';
        pWhy.textContent = "";
        pExtra.innerHTML = "";
        clearArtifacts();
        show();
      });
    }
    function setLiveText(text, token) {
      if (!still(token, "setLiveText")) return;
      pSaid.innerHTML = escapeHtml(text || "") + '<span class="caret">▍</span>';
      keepLatestVisible();
    }

    // Speak mode: show Focus's reply, typed out (fast fallback path).
    function speak(text, done, decor, token) {
      var seq = gate(token, "speak");
      if (seq === null) return;          // an older reply's late fallback
      clearTimers();
      stopSynced();
      swapMode(function () {
        if (!still(seq, "speak")) return;
        pWhy.textContent = "";
        pExtra.innerHTML = "";
        clearArtifacts();
        applyPreArtifacts(decor);   // P20-02: trace/sources land with the words
        show();
        typeInto(pSaid, text, function () {
          // P11-08: this path types PLAIN text, so run the finished string back
          // through the ONE reply-DOM builder (fully revealed, so the screen
          // does not change) and decorate that. buildWordSpans stays the only
          // place reply DOM is built, and the words remain addressable.
          if (decor && (decor.refs || decor.actions)) {
            try {
              var built = buildWordSpans(text);
              built.forEach(function (s) { s.classList.add("on"); });
              applyRefs(decor.refs, built);
              applyActions(decor.actions);
            } catch (_) {}
          }
          applyPostArtifacts(decor);   // P20-02: cards deal in below the words
          if (done) done();
        }, seq);
      });
    }

    /* --- Caption-synced speech (P7-01) -------------------------------
       speakSynced(text, audio, done): split the reply into word spans and
       reveal them in step with audio.currentTime via a rAF loop. The same
       loop drives --talk-amp on .eyes (a cheap pseudo-amplitude) so the
       eyes "talk" with the voice. Cancellable via stopSynced(); on any
       cancel/finish every remaining word is revealed so content persists. */
    var sync = null;   // active run: { raf, audio, revealAll, finish }

    function stopSynced() {
      // NOTE (P9 bugfix): stopSynced no longer cancels a queued swap. It used
      // to — and any interrupt that ran inside another reply's 140ms cross-
      // fade window silently dropped that reply's render: the screen kept the
      // PREVIOUS text while the new turn carried on. Staleness is now the
      // claim's job (a stale queued callback self-suppresses at the gate);
      // the latest render's queued commit is untouchable.
      if (!sync) return;
      var run = sync;
      sync = null;
      if (run.raf) cancelAnimationFrame(run.raf);
      try { run.revealAll(); } catch (_) {}
      if (eyesEl) {
        eyesEl.classList.remove("talking-live");
        eyesEl.style.setProperty("--talk-amp", "0");
      }
    }

    /* buildWordSpans is the ONE place the reply's DOM gets built, and the
       array it returns is the addressable index of that DOM: word i is
       always lastWords[i], never "the i-th child of #p-said". Nothing here
       reads textContent back out to re-derive state, and nothing walks
       children positionally, so a later pass can wrap runs of these spans
       in a parent (to decorate a word RANGE) without breaking the reveal
       loop. lastWords is kept so that pass has the mapping for free. */
    var lastWords = [];
    var lastSplit = null;   // the GSAP split still attached to #p-said
    function sameText(a, b) {
      return (a || "").replace(/\s+/g, " ").trim() === (b || "").replace(/\s+/g, " ").trim();
    }
    function buildWordSpans(text) {
      var spans = [];
      /* THE FOUR-TIME BUG LIVED HERE (P12-04). GSAP's SplitText caches the
         element's ORIGINAL html and restores it whenever a NEW SplitText is
         constructed on that same element. #p-said is reused by every reply,
         so the second voiced reply's construction quietly put the FIRST
         reply's text back on screen and handed back the FIRST reply's word
         spans — which the reveal loop then played against the SECOND reply's
         audio. Hence "the audio is right and the screen is showing the
         previous interaction", and hence three render-token fixes that never
         landed it: the divergence was born inside the one place reply DOM is
         built, downstream of every claim check. Reverting the previous split
         first clears that cache. */
      if (lastSplit) { try { lastSplit.revert(); } catch (_) {} lastSplit = null; }
      pSaid.innerHTML = "";
      // GSAP SplitText when available; manual split otherwise. Both paths end
      // with the same span list carrying the .w class (CSS owns the styling).
      if (window.gsap && window.SplitText) {
        try {
          pSaid.textContent = text;
          var split = new window.SplitText(pSaid, { type: "words" });
          if (split.words && split.words.length) { spans = split.words.slice(); lastSplit = split; }
        } catch (_) { spans = []; }
      }
      /* THE DOM VERIFIES ITSELF, the way applyRefs' spans already do. The
         spoken string and the displayed string are the same string (P11-10
         pins that server-side), so if what actually landed in #p-said is not
         the string we were asked to render, the build is thrown away and
         rebuilt by hand rather than shown. Showing words the voice is not
         saying is the one outcome worse than a plain paragraph. */
      if (spans.length && !sameText(pSaid.textContent, text)) {
        try { console.debug("[surface] split DOM did not match the reply — rebuilding by hand"); } catch (_) {}
        if (lastSplit) { try { lastSplit.revert(); } catch (_) {} lastSplit = null; }
        spans = [];
      }
      if (!spans.length) {
        pSaid.innerHTML = "";
        text.split(/\s+/).forEach(function (w) {
          if (!w) return;
          var s = document.createElement("span");
          s.textContent = w;
          s.style.display = "inline-block";
          pSaid.appendChild(s);
          pSaid.appendChild(document.createTextNode(" "));
          spans.push(s);
        });
      }
      spans.forEach(function (s) { s.classList.add("w"); });
      lastWords = spans;
      return spans;
    }

    /* --- Typed inline references + one action (P11-08) -----------------
       THE INVARIANT: the model never emits markup. The reply is ONE plain
       string (exactly what Cloud TTS speaks) and decoration arrives beside
       it as word-aligned typed spans, `{words:[i,j], kind, payload}`, built
       server-side by src/core/annotate.py from values it holds real objects
       for. So a date is tappable only because a block exists behind it, and
       a fabricated one has nothing to match and stays flat text.

       Mechanically this is pure wrapping: the run lastWords[i..j-1] is moved
       under one new parent and the leaf `.w` / `.w.on` classes are left
       exactly where they were, so revealUpTo (which indexes the ARRAY, never
       the DOM children) is untouched and the voice-sync reveal keeps driving.
       No spans supplied = renders exactly as P11-01 ships it. */
    function applyRefs(refs, words) {
      // `words` is passed in by the caller — the EXACT array the build it is
      // decorating just returned — never read back off `lastWords`. Two turns
      // landing close together used to be able to apply the newer reply's
      // spans over the older reply's DOM; indices are only ever meaningful
      // against the string they were computed for, so the array travels with
      // them. The live-document check below is the second belt.
      if (!refs || !refs.length || !words || !words.length) return;
      refs.forEach(function (r) {
        var range = r && r.words;
        if (!range || range.length !== 2) return;
        var i = range[0], j = range[1];
        if (!(i >= 0 && j > i && j <= words.length)) return;
        var first = words[i], last = words[j - 1];
        if (!first || !last || !first.parentNode) return;
        if (!pSaid.contains(first) || !pSaid.contains(last)) return;   // stale build
        // THE SPAN VERIFIES ITSELF. The server sends the grounded value it
        // matched, so before wrapping anything we confirm the run really does
        // contain it. Word indices are only meaningful against the string they
        // were computed for; if two turns land close together and the DOM on
        // screen is a different reply, this check fails and the span is simply
        // dropped. Decoration is a truth signal, so it renders nothing rather
        // than mark the wrong words.
        if (r.value) {
          var run = "";
          for (var k = i; k < j; k++) run += (k > i ? " " : "") + words[k].textContent;
          if (run.indexOf(r.value) === -1) return;
        }
        if (first.parentNode !== last.parentNode) return;   // never reparent across containers
        var payload = r.payload || {};
        var actionable = !!(payload.action && onAction);
        var wrap = document.createElement(actionable ? "button" : "span");
        wrap.className = "ref ref-" + (r.kind || "count") + (actionable ? " ref-act" : "");
        if (actionable) {
          wrap.type = "button";
          // a11y: a real button in the flow, keyboard reachable, named by what
          // it DOES. #p-said's aria-live still announces the plain reply once.
          wrap.setAttribute("aria-label", payload.label || "Open this in your plan");
          wrap.addEventListener("click", function (e) {
            e.stopPropagation();          // the surface's click-to-dismiss must not eat it
            try { onAction(payload); } catch (_) {}
          });
        }
        var parent = first.parentNode;
        parent.insertBefore(wrap, first);
        var node = wrap.nextSibling, guard = 0, hit = false;
        while (node && guard++ < 64) {
          var next = node.nextSibling;
          hit = (node === last);
          wrap.appendChild(node);
          node = next;
          if (hit) break;
        }
      });
    }

    // RESTRAINT (an acceptance criterion, not a preference): at most ONE
    // prominent action, and the server capped it before it got here.
    function applyActions(actions) {
      if (!actions || !actions.length || !onAction) return;
      var a = actions[0];
      if (!a || !a.action) return;
      var row = document.createElement("div");
      row.className = "reply-actions";
      var b = document.createElement("button");
      b.className = "btn ghost reply-action";
      b.type = "button";
      b.textContent = a.label || "Open";
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        try { onAction(a); } catch (_) {}
      });
      row.appendChild(b);
      pExtra.appendChild(row);
    }

    /* =================================================================
       P20-02: conversational artifacts. THE LAW: no payload, no artifact.
       Every function below renders ONLY from fields that are present and
       well-formed, and silently renders nothing otherwise — a backend that
       has not started sending these yet costs zero pixels. All text lands
       via textContent (titles, summaries, domains are DATA, never markup).

       Two landing zones, both cleared on every mode reset:
         #p-art  (above the words)  — tool trace lines, search sources
         #p-extra (below the words) — session cards, move cards
       ================================================================= */
    function mk(tag, cls, text) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    }
    function artDate(iso) {
      if (typeof iso !== "string" || !iso) return null;
      var d = new Date(iso);
      return isNaN(d) ? null : d;
    }
    function artTime(d) {
      return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    }
    // The calendar tile: accent strip (weekday / TODAY), big day number,
    // small time. Rendered only when the datetime really parses — a tile is
    // a claim about a date, so no date means no tile (zero hallucinated
    // datetimes; the body still renders beside an absent tile).
    function artTile(d) {
      if (!d) return null;
      var tile = mk("div", "art-tile");
      var now = new Date();
      var today = d.getFullYear() === now.getFullYear() &&
                  d.getMonth() === now.getMonth() &&
                  d.getDate() === now.getDate();
      var wd = today ? "TODAY"
             : d.toLocaleDateString(undefined, { weekday: "short" }).toUpperCase();
      tile.appendChild(mk("span", "art-tile-wd", wd));
      tile.appendChild(mk("span", "art-tile-day", String(d.getDate())));
      tile.appendChild(mk("span", "art-tile-time", artTime(d)));
      return tile;
    }

    // (c) Tool lines: the turn's real tool calls, as a quiet mono column.
    // Known tools get plain words; an unknown one is humanized, never hidden
    // (the trace is a truth record — see agent-governance).
    var TOOL_WORDS = {
      list_calendar_events: "reading your calendar",
      get_capacity: "measuring free time",
      propose_schedule_for_workspace: "drafting a plan",
      list_todays_sessions: "checking today's sessions",
      web_search: "searching the web",
      propose_reschedule: "planning the moves",
    };
    function toolWords(name) {
      var key = String(name || "");
      if (TOOL_WORDS[key]) return TOOL_WORDS[key];
      return key.replace(/[_-]+/g, " ").trim();
    }
    function applyTrace(trace) {
      if (!Array.isArray(trace) || !trace.length) return;
      var col = mk("div", "trace");
      trace.forEach(function (t) {
        if (!t || typeof t !== "object") return;
        var words = toolWords(t.tool);
        if (!words && !t.summary) return;
        var line = mk("div", "trace-line");
        if (words) line.appendChild(mk("span", "trace-tool", words));
        line.appendChild(mk("span", "trace-check", "✓"));
        if (t.summary) line.appendChild(mk("span", "trace-sum", String(t.summary)));
        col.appendChild(line);
      });
      if (col.childNodes.length) pArt.appendChild(col);
    }

    // (d) Search artifact: source chips (dot + domain) above the reply, and
    // the query line only when the reply data actually carries the query.
    function artDomain(url) {
      try {
        var u = new URL(String(url));
        if (u.protocol !== "https:" && u.protocol !== "http:") return null;
        return { href: u.href, domain: u.hostname.replace(/^www\./, "") };
      } catch (_) { return null; }
    }
    function applySearch(sources, query) {
      var cited = [];
      (Array.isArray(sources) ? sources : []).forEach(function (s) {
        if (!s || typeof s !== "object" || !s.url) return;
        var d = artDomain(s.url);
        if (d) cited.push(d);
      });
      if (!cited.length) return;
      var wrap = mk("div", "search-art");
      if (query) {
        var q = mk("div", "search-query");
        q.appendChild(mk("span", "search-glyph", "⌕"));
        q.appendChild(mk("span", "search-q", String(query)));
        wrap.appendChild(q);
      }
      var row = mk("div", "source-chips");
      cited.forEach(function (c) {
        var chip = mk("a", "source-chip", c.domain);
        chip.href = c.href;
        chip.target = "_blank";
        chip.rel = "noopener noreferrer";
        row.appendChild(chip);
      });
      wrap.appendChild(row);
      pArt.appendChild(wrap);
    }

    // (a) Session cards: one horizontal card per planned session — tile on
    // the left, title / mono span / why on the right, and the calendar chip
    // ONLY when the server says calendar === true (truthful, like emotions).
    function applySessions(artifacts) {
      var list = artifacts && artifacts.sessions;
      if (!Array.isArray(list) || !list.length) return;
      var col = mk("div", "art-cards");
      list.forEach(function (s) {
        if (!s || typeof s !== "object") return;
        var start = artDate(s.starts_at), end = artDate(s.ends_at);
        if (!s.title && !start) return;   // nothing real to show
        var card = mk("div", "art-card");
        var tile = artTile(start);
        if (tile) card.appendChild(tile);
        var body = mk("div", "art-body");
        if (s.title) body.appendChild(mk("div", "art-title", String(s.title)));
        if (start && end) {
          var mins = Math.round((end - start) / 60000);
          body.appendChild(mk("div", "art-when",
            artTime(start) + " to " + artTime(end) + " · " + mins + "m"));
        }
        if (s.why) body.appendChild(mk("div", "art-why", String(s.why)));
        if (s.calendar === true) {
          body.appendChild(mk("span", "art-chip art-chip-cal", "On your calendar"));
        }
        card.appendChild(body);
        col.appendChild(card);
      });
      if (col.childNodes.length) pExtra.appendChild(col);
    }

    // (b) Move cards: old time struck through, arrow, new time in the accent
    // chip. calendar: "moved" earns the chip, "partial"/"failed" the honest
    // warm retry note, "none" nothing at all — the card claims exactly what
    // the server did (never claim actions not taken).
    function applyMoves(moves, note) {
      if (!Array.isArray(moves) || !moves.length) return;
      var col = mk("div", "art-cards");
      moves.forEach(function (m) {
        if (!m || typeof m !== "object") return;
        var oldD = artDate(m.old_start), newD = artDate(m.new_start);
        if (!m.title && !newD) return;
        var card = mk("div", "art-card art-move");
        var tile = artTile(newD);
        if (tile) card.appendChild(tile);
        var body = mk("div", "art-body");
        if (m.title) body.appendChild(mk("div", "art-title", String(m.title)));
        if (oldD || newD) {
          var when = mk("div", "art-when");
          if (oldD) when.appendChild(mk("span", "art-old", artTime(oldD)));
          if (oldD && newD) when.appendChild(mk("span", "art-arrow", "→"));
          if (newD) when.appendChild(mk("span", "art-new", artTime(newD)));
          body.appendChild(when);
        }
        if (m.calendar === "moved") {
          body.appendChild(mk("span", "art-chip art-chip-cal", "Calendar moved"));
        } else if (m.calendar === "partial" || m.calendar === "failed") {
          body.appendChild(mk("span", "art-note", "Calendar retrying"));
        }
        card.appendChild(body);
        col.appendChild(card);
      });
      if (!col.childNodes.length) return;
      pExtra.appendChild(col);
      if (note) pExtra.appendChild(mk("div", "art-cal-note", String(note)));
    }

    // The two halves, in paint order. Pre lands with the first words (the
    // trace reads as "here is how I got this"); post lands once the words
    // have, beside applyActions, so cards never deal in over a half-typed
    // sentence. Both are hard-gated on their payloads being present.
    function applyPreArtifacts(decor) {
      if (!decor) return;
      try { applyTrace(decor.trace); } catch (_) {}
      try { applySearch(decor.sources, decor.query); } catch (_) {}
      keepLatestVisible();
    }
    function applyPostArtifacts(decor) {
      if (!decor) return;
      try { applySessions(decor.artifacts); } catch (_) {}
      try { applyMoves(decor.moves, decor.calendar_note); } catch (_) {}
      keepLatestVisible();
    }

    function speakSynced(text, audio, done, decor, token) {
      var seq = gate(token, "speakSynced");
      // An older reply's late audio: the words are not landing, so the voice
      // does not either. Both halves of a superseded reply die together.
      if (seq === null) { dropAudio(audio); return; }
      clearTimers();
      stopSynced();
      // The whole run — word spans, audio start, rAF loop — waits behind the
      // cross-fade, so the first words never reveal into a fading-out panel.
      // swapMode is instant when reduced-motion/hidden, and completeReveals()
      // flushes a queued swap before completing, so no path strands this.
      swapMode(function () {
        if (!still(seq, "speakSynced")) { dropAudio(audio); return; }
        speakSyncedNow(text, audio, done, decor, seq);
      });
    }

    function speakSyncedNow(text, audio, done, decor, seq) {
      try { console.debug("[surface] syncedNow:", (text || "").slice(0, 40)); } catch (_) {}
      pWhy.textContent = "";
      pExtra.innerHTML = "";
      clearArtifacts();
      applyPreArtifacts(decor);   // P20-02: trace/sources land with the words
      show();

      var spans = buildWordSpans(text);
      // P11-08: decorate BEFORE the reveal starts. Wrapping runs of `.w` spans
      // leaves the leaves (and their classes) alone, so revealUpTo below is
      // completely unaffected; a missing/empty `refs` is simply a no-op.
      try { applyRefs(decor && decor.refs, spans); } catch (_) {}
      var wordCount = spans.length;
      var shown = 0;
      var lastReveal = 0;
      var finished = false;

      function revealUpTo(n) {
        var before = shown;
        while (shown < n && shown < wordCount) {
          spans[shown].classList.add("on");
          shown++;
          lastReveal = performance.now();
        }
        if (shown !== before) keepLatestVisible();
      }

      var run = {
        raf: null,
        audio: audio,
        revealAll: function () { revealUpTo(wordCount); },
      };

      function finish() {
        if (finished) return;
        finished = true;
        if (sync === run) stopSynced(); else run.revealAll();
        // the prominent action lands once the words have, never over them
        try { applyActions(decor && decor.actions); } catch (_) {}
        applyPostArtifacts(decor);   // P20-02: cards deal in below the words
        if (done) done();
      }
      run.finish = finish;

      audio.addEventListener("ended", finish);

      if (eyesEl && !reduce) eyesEl.classList.add("talking-live");

      function frame() {
        if (sync !== run || finished) return;   // cancelled elsewhere
        // The turn moved on under us: the words stop and the voice stops with
        // them, so the room can never hear a reply the screen has dropped.
        if (!holdsRender(seq)) { dropAudio(audio); stopSynced(); return; }
        var dur = audio.duration || 0;
        if (dur > 0) {
          revealUpTo(Math.floor(audio.currentTime / dur * wordCount));
        }
        if (eyesEl && !reduce) {
          var wordPulse = (performance.now() - lastReveal < 90) ? 0.04 : 0;
          var amp = 0.06 + 0.05 * Math.abs(Math.sin(audio.currentTime * 7.3)) + wordPulse;
          eyesEl.style.setProperty("--talk-amp", amp.toFixed(3));
        }
        if (audio.ended || shown >= wordCount) { finish(); return; }
        run.raf = requestAnimationFrame(frame);
      }

      sync = run;
      try {
        var p = audio.play();
        if (p && p.catch) p.catch(function () { finish(); });   // autoplay blocked -> text still lands
      } catch (_) { finish(); return; }
      // Hidden tab: rAF never fires there, so don't strand a frozen reveal —
      // land the full text now and let the audio keep playing.
      if (document.visibilityState === "hidden") { finish(); return; }
      run.raf = requestAnimationFrame(frame);
    }

    // Tab hidden mid-reveal (P7-01 fix): rAF + timers freeze in hidden tabs
    // while audio keeps playing, stranding a half-revealed reply. Complete
    // everything immediately — full text, no caret — and leave audio alone.
    function completeReveals() {
      flushSwap();   // a queued mode swap must land before we can complete it
      if (sync) {
        var run = sync;
        try { run.finish(); } catch (_) { stopSynced(); }
      }
      if (activeType) {
        var t = activeType;
        clearTimers();                       // also nulls activeType
        t.el.textContent = t.text;           // full text, caret gone
        if (t.done) t.done();
      }
      pExtra.classList.remove("veil");       // ask control never stays hidden
    }
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") completeReveals();
    });

    // Thinking placeholder (caret only).
    function pending(token) {
      var seq = gate(token, "pending");
      if (seq === null) return;
      clearTimers();
      swapMode(function () {
        if (!still(seq, "pending")) return;
        pSaid.innerHTML = '<span class="caret">▍</span>';
        pWhy.textContent = "";
        pExtra.innerHTML = "";
        clearArtifacts();
        show();
      });
    }

    // Ask mode: render a ClarifyQuestion control and collect one typed answer.
    // The question text/why live in the panel's own p-said/p-why rows (so the
    // control renders just the input); renderClarifyQuestion emits the live
    // value on every change, and Send commits the latest via onSubmit(value).
    // For input_type "confirm" the yes/no click IS the commit — no Send button.
    // -> future <BlurPanel mode="ask" question={...} onSubmit={...} />
    function ask(question, onSubmit, token) {
      var seq = gate(token, "ask");
      if (seq === null) return;
      clearTimers();
      question = question || {};
      swapMode(function () {
        if (!still(seq, "ask")) return;
        pSaid.textContent = "";
        pWhy.textContent = question.why || "";
        pExtra.innerHTML = "";
        clearArtifacts();

        // The panel owns the question/why lines, so hand the control a copy with
        // them blanked — otherwise renderClarifyQuestion would repeat the prompt.
        var qForControl = {};
        Object.keys(question).forEach(function (k) { qForControl[k] = question[k]; });
        qForControl.question = "";
        qForControl.why = "";

        var host = document.createElement("div");
        pExtra.appendChild(host);

        var hasValue = false;   // guards against an empty submit
        var latest;

        // P11-02e: the moment an answer is committed, the kit COLLAPSES
        // toward the margin note (clarify.css .answered) instead of sitting
        // there as a dead widget until the next mode swap. Purely visual and
        // strictly after the fact: commit() hands the value on first, so the
        // request timing, the busy guard and the emitted shapes are all
        // untouched. Marked once, so a stray second emit can't restart it.
        var answered = false;
        function collapseKit() {
          if (answered) return;
          answered = true;
          var kit = pExtra.querySelector(".clarify");
          if (kit) kit.classList.add("answered");
        }
        function commit(v) { onSubmit(v); collapseKit(); }

        if (question.input_type === "confirm" || question.input_type === "courses") {
          // Decisive controls: confirm commits on the yes/no click, the
          // course cards (P9-04) on their own Use these / Skip pair.
          window.FocusComponents.renderClarifyQuestion(host, qForControl, function (v) {
            hasValue = true; latest = v; commit(v);
          });
        } else {
          var send = document.createElement("button");
          send.className = "btn go";
          send.id = "ask-send";
          send.type = "button";
          send.textContent = "Send";
          send.disabled = true;                 // enabled once a value is emitted
          window.FocusComponents.renderClarifyQuestion(host, qForControl, function (v) {
            hasValue = true; latest = v; send.disabled = false;
          });
          var row = document.createElement("div");
          row.className = "send";
          row.appendChild(send);
          // Skippable questions (P9-08 onboarding): a ghost Skip beside Send,
          // always live, emitting the {__skip:true} sentinel. Every interview
          // answer stays optional without leaving the existing kit.
          if (question.skippable) {
            var skipBtn = document.createElement("button");
            skipBtn.className = "btn ghost";
            skipBtn.type = "button";
            skipBtn.textContent = "Skip";
            row.appendChild(skipBtn);
            skipBtn.addEventListener("click", function () {
              commit({ __skip: true });
            });
          }
          pExtra.appendChild(row);
          send.addEventListener("click", function () {
            if (hasValue) commit(latest);
          });
        }

        // The question types on like a reply (P7-02): the control is built
        // but veiled, and fades UP into place 120ms behind the last letter
        // (P11-02e; conversation.css owns the lift). Hidden tab / reduced
        // motion: typeInto is instant and the veil lifts instantly.
        pExtra.classList.add("veil");
        show();
        typeInto(pSaid, question.question || "", function () {
          if (reduce || document.visibilityState === "hidden") {
            pExtra.classList.remove("veil");
          } else {
            later(function () { pExtra.classList.remove("veil"); }, 120);
          }
        }, seq);
      });
    }

    /* The interrupt seam: a turn's voice is being cut, so the words that
       voice was writing stop in the same beat. Before this, stopSpeech()
       killed the audio and left an in-flight type-on happily typing the rest
       of the sentence to an empty room. Text and audio are ONE unit. */
    function abort() {
      stopSynced();
      clearTimers();
    }

    return {
      compose: compose, live: live, setLiveText: setLiveText,
      speak: speak, speakSynced: speakSynced, stopSynced: stopSynced,
      abort: abort,              // interrupt: kill this turn's words with its voice
      claim: claimRender,        // the ONE way to get a paint claim: start a turn
      holds: holdsRender,        // …and a painter must still hold it to paint
      words: function () { return lastWords; },   // word-range decoration seam
      pending: pending, ask: ask,
      hide: hide, later: later, clearTimers: clearTimers,
    };
  }

  /* =====================================================================
     Dock (P11-02a) — the input trio under the eyes: keyboard · mic · attach.
     The mic itself belongs to createVoiceInput (it owns hold-to-talk and the
     Spacebar); this factory owns the two controls beside it, which used to
     be either missing or hidden inside the compose row:
       keyboard -> opens and focuses the compose field (onKeyboard)
       "+"      -> the hidden file input, straight into the shared sendImage
                   path that drag-drop and clipboard paste already use
     Maps to <Dock onKeyboard={...} onFile={...} /> in React.
     ===================================================================== */
  function createDock(opts) {
    opts = opts || {};
    var keys = document.getElementById("keys");
    var attach = document.getElementById("attach");
    var attachFile = document.getElementById("attach-file");

    if (keys && opts.onKeyboard) {
      keys.addEventListener("click", function () { opts.onKeyboard(); });
    }
    if (attach && attachFile) {
      attach.addEventListener("click", function () {
        if (opts.onFile) attachFile.click();
      });
      attachFile.addEventListener("change", function () {
        var f = attachFile.files && attachFile.files[0];
        attachFile.value = "";          // same file re-pickable next time
        if (f && opts.onFile) opts.onFile(f);
      });
    }
    return { keys: keys, attach: attach };
  }

  /* =====================================================================
     VoiceInput — hold-to-talk over the mic button (below the eyes) and the
     Spacebar. Uses the Web Speech API for LIVE transcription: while held,
     interim + final results stream onto the surface; on release the accrued
     transcript drops into the editable compose field (NOT auto-sent) so the
     user can review + Send. A quick tap (or an unsupported browser) just
     opens the empty editable field to type. Never throws.

     Deps: agent (state), surface (compose/live/setLiveText), onCommit(text)
     to send, setHint(text) for the dock hint line, onBegin() fired at the
     start of every hold (mic OR spacebar) so the controller can cut any
     reply audio before listening starts (P7-01).
     Maps to <VoiceInput onCommit={...} state={agentState} /> in React.
     ===================================================================== */
  // onBegin: the controller's startTurn. Reaching for the mic or the keyboard
  // starts a turn, so it cuts the previous reply and RETURNS that turn's
  // surface claim, which every paint below carries.
  function createVoiceInput(agent, surface, onCommit, setHint, onBegin, isLocked) {
    var mic = document.getElementById("mic");
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var supported = !!SR;
    var rec = null;
    var recording = false;
    var finalText = "";     // accumulated final results
    var liveText = "";      // final + current interim, trimmed
    // A hold is physically down. Tracked because permission is now checked
    // ASYNC on first use (see ensureMic), so a release can land BEFORE we ever
    // start listening — the difference between "held to talk" and "tapped".
    var holdActive = false;
    // Mic permission, remembered once granted. Gating recognition behind an
    // explicit getUserMedia makes Chrome's prompt appear reliably and turns a
    // denial into a CLEAR message; SpeechRecognition alone fails into onerror,
    // which this code used to swallow — the "I click the mic and nothing
    // happens" report (2026-08-30).
    var micGranted = false;
    function ensureMic() {
      if (micGranted) return Promise.resolve(true);
      var md = navigator.mediaDevices;
      if (!md || !md.getUserMedia) return Promise.resolve(true); // let SR try; its onerror speaks
      return md.getUserMedia({ audio: true }).then(function (stream) {
        stream.getTracks().forEach(function (t) { try { t.stop(); } catch (_) {} });
        micGranted = true;
        return true;
      }).catch(function () { return false; });
    }
    var MIC_BLOCKED = "Microphone is blocked. Allow it from your browser’s address bar, then hold the mic again.";

    // Don't start mid-request or mid-question — those own the surface.
    function canStart() {
      var s = agent.get();
      return s !== "thinking" && s !== "asking";
    }

    // Auto-send (P8-01c): release commits straight through the Send path.
    function autoSendOn() {
      return !!(window.FocusSettings && window.FocusSettings.get("autoSend"));
    }

    // The surface claim for the turn this hold/keyboard press started. Reaching
    // for the mic or the keyboard IS the start of a turn (see startTurn in the
    // controller), so onBegin hands the claim back and every paint below rides
    // it. If a newer turn has since taken the room, these paints do nothing.
    var claim = null;
    function beginTurn() {
      if (!onBegin) { claim = null; return; }
      try { claim = onBegin(); } catch (_) { claim = null; }
    }

    // A one-shot hint that survives the next toEditable. A mic error opens the
    // compose field to fall back to typing, and toEditable would otherwise
    // overwrite the "why" with its generic "Review, then Send" — so the error
    // reason is stashed here and consumed once, keeping the field-open AND the
    // explanation (2026-08-30).
    var stickyHint = null;

    // Released -> editable/reviewable state (prefilled, focused, NOT sent).
    // Also the whole job of the dock's keyboard button (P11-02a): open the
    // field, empty, focused, with the hint naming what Enter will do.
    function toEditable(text) {
      var h = stickyHint || (supported ? "Review, then Send · Enter to send"
                                       : "Type your message · Enter to send");
      stickyHint = null;
      setHint(h);
      surface.compose(onCommit, text || "", claim);
    }

    // Released -> either commit the transcript now (auto-send on, non-empty)
    // or settle into review. onCommit is the SAME path as pressing Send, so
    // the double-submit guard and the startTurn interrupt all apply.
    function commitOrEdit(text) {
      var v = (text || "").trim();
      if (v && autoSendOn()) { onCommit(v); return; }
      toEditable(v);
    }

    function begin() {
      if (recording || !canStart() || (isLocked && isLocked())) return;
      beginTurn();

      if (!supported) {
        // Fallback: no live transcription — just open the field to type.
        agent.set("listening");
        setHint("Speech input isn’t available in this browser, so type instead.");
        surface.compose(onCommit, "", claim);
        return;
      }

      holdActive = true;
      // Show the listening posture straight away so the hold feels responsive,
      // even while a first-time permission prompt is up.
      agent.set("listening");
      setHint(autoSendOn() ? "Listening… release to send" : "Listening… release to edit");
      surface.live(claim);

      ensureMic().then(function (ok) {
        if (!holdActive) {
          // Released before we could start — a TAP, not a hold. Open the field
          // to type, which is what a tap on the mic has always done.
          toEditable("");
          return;
        }
        if (!ok) {
          // Blocked or dismissed. Say why, in words, and fall back to typing
          // so the turn is never a silent dead end. The reason rides the
          // sticky hint so opening the field doesn't erase it.
          stickyHint = MIC_BLOCKED;
          toEditable("");
          return;
        }
        startRecognition();
      });
    }

    function startRecognition() {
      finalText = ""; liveText = "";
      rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = "en-US";
      rec.onresult = function (e) {
        var interim = "";
        for (var i = e.resultIndex; i < e.results.length; i++) {
          var r = e.results[i];
          if (r.isFinal) finalText += r[0].transcript;
          else interim += r[0].transcript;
        }
        liveText = (finalText + interim).replace(/\s+/g, " ").trim();
        surface.setLiveText(liveText || "…", claim);
      };
      rec.onerror = function (e) {
        // No longer swallowed: a denied or broken mic must SAY so, or it reads
        // as "the button does nothing" (2026-08-30). onend still follows and
        // settles the surface to an editable field, so typing always works.
        var err = (e && e.error) || "";
        // Stash the reason on the sticky hint; onend follows immediately and
        // its toEditable() surfaces it while opening the field to type.
        if (err === "not-allowed" || err === "service-not-allowed") {
          micGranted = false;
          stickyHint = MIC_BLOCKED;
        } else if (err === "audio-capture") {
          stickyHint = "I can’t find a microphone on this device. You can type instead.";
        } else if (err === "network") {
          stickyHint = "Speech recognition needs a connection just now. You can type instead.";
        }
        /* no-speech / aborted: quiet — onend settles to edit */
      };
      rec.onend = function () {
        // Ended on its own while still held (timeout/network): settle to edit.
        if (recording) { recording = false; toEditable(liveText); }
      };

      recording = true;
      try { rec.start(); } catch (_) { /* already started — ignore */ }
    }

    function end() {
      holdActive = false;
      // recording is false on a tap or while permission is still pending; the
      // begin() promise settles those paths, so there is nothing to stop here.
      if (!recording) return;
      recording = false;
      // No transient hint flash here: commitOrEdit() (via toEditable or the
      // send path) settles the one hint that should survive the release —
      // the debounce absorbs the churn.
      try { rec.stop(); } catch (_) {}
      commitOrEdit(liveText);
    }

    // --- Hold the mic (pointer) ---
    mic.addEventListener("pointerdown", function (e) { e.preventDefault(); begin(); });
    mic.addEventListener("pointerup",   function (e) { e.preventDefault(); end(); });
    mic.addEventListener("pointerleave", function () { if (recording) end(); });
    mic.addEventListener("pointercancel", function () { if (recording) end(); });
    mic.addEventListener("contextmenu", function (e) { e.preventDefault(); });
    // Open the field to type: Enter on the focused mic (P7-09), and the
    // dock's keyboard button (P11-02a) — the same one path, so the two
    // affordances can never drift apart.
    function openCompose() {
      if (recording || !canStart() || (isLocked && isLocked())) return;
      beginTurn();
      toEditable("");
      return true;
    }
    mic.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      openCompose();
    });

    // --- Hold Spacebar (only when not typing in a field) ---
    function isTyping(t) {
      return !!t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.isContentEditable);
    }
    document.addEventListener("keydown", function (e) {
      if (e.code !== "Space" && e.key !== " ") return;
      if (isTyping(e.target)) return;         // let the spacebar type normally
      e.preventDefault();                     // never scroll the page
      if (e.repeat || recording) return;      // ignore auto-repeat / re-entry
      begin();
    });
    document.addEventListener("keyup", function (e) {
      if (e.code !== "Space" && e.key !== " ") return;
      if (isTyping(e.target)) return;
      if (recording) { e.preventDefault(); end(); }
    });

    // --- Just start typing (P11-03) ---
    // With the plan open the dock is hidden, so the keyboard itself has to be
    // a door. Any printable key, no modifier, nothing focused: the field
    // opens and that character lands in it. Holding Space still talks, so the
    // spacebar is deliberately not a trigger here.
    document.addEventListener("keydown", function (e) {
      if (isTyping(e.target)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!e.key || e.key.length !== 1 || e.key === " ") return;
      var ch = e.key;
      if (!openCompose()) return;
      e.preventDefault();
      var seed = function () {
        var f = document.querySelector("#surface textarea.field, #surface input.field");
        if (!f) return false;
        f.focus();
        f.value = ch;
        f.dispatchEvent(new Event("input", { bubbles: true }));
        try { f.setSelectionRange(1, 1); } catch (_) {}
        return true;
      };
      if (!seed()) requestAnimationFrame(seed);
    });

    return { supported: supported, begin: begin, end: end, openCompose: openCompose };
  }

  /* =====================================================================
     Stage (P7-05) — the morph-reveal orchestrator. One continuous room:
     when the plan is wanted the eyes PARK — a brief spread, then the rig
     rises and shrinks into a slim top band — while #horizon materializes
     in the space they vacated. The park transform lives on .face-rig and
     nowhere else, so blink / breathe / emotions / cursor-tracking keep
     their own channels and the parked eyes stay alive. Fully reversible:
     peek handle, wheel/swipe, Esc, or clicking the parked eyes.
     Choreography is CSS-delay-driven off the #app.viewing class (state
     truth), so the no-GSAP path lands the identical end state; GSAP, when
     present, adds the glint-brighten flourish on top.
     The horizon body is the ported week renderer — P7-06 replaces it
     with createHorizon. Maps to <Stage open={...}> in React.
     ===================================================================== */
  function createStage(appEl, agent) {
    var rig = document.querySelector(".face-rig");
    var stageEl = document.querySelector(".stage");
    var eyesEl = document.querySelector(".eyes");
    var horizon = document.getElementById("horizon");
    var handle = document.getElementById("peek-handle");
    var surfaceEl = document.getElementById("surface");
    var scrimEl = document.getElementById("convo-scrim");   // P11-03
    var dockEl = document.getElementById("dock");
    var echoEl = document.getElementById("echo");

    var open = false;
    var surfaceWasUp = false;   // a showing reply recedes on open, restores on close
    var wantsReveal = false;    // typed intent ("show my week") — armed per turn
    var spreadTimer = null, focusTimer = null, soonTimer = null;

    function isOpen() { return open; }

    /* --- the two layers stop fighting (P11-03) --------------------------
       While the plan is open the conversation layer and the plan were
       drawing in the same pixels. The dock now leaves entirely (CSS), and
       the plan reserves exactly the band that is still VISIBLE — measured
       every time it changes, never a constant — so #h-meta and everything
       else in the plan ends above the words rather than under them. What
       does still arrive over the plan (a reply, a clarify question, the
       compose field) gets the feathered scrim for legibility. */
    function visibleTop(el) {
      if (!el) return null;
      var cs = window.getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none") return null;
      if (parseFloat(cs.opacity) < 0.05) return null;
      var r = el.getBoundingClientRect();
      return r.height > 0 ? r.top : null;
    }
    function convoTop() {
      var top = null;
      function note(t) { if (t != null) top = (top == null) ? t : Math.min(top, t); }
      if (surfaceEl && surfaceEl.classList.contains("show")) note(visibleTop(surfaceEl));
      if (echoEl && echoEl.classList.contains("show")) note(visibleTop(echoEl));
      note(visibleTop(dockEl));
      return top;
    }
    var lastReserve = null;
    function reserveSpace() {
      if (!horizon || !stageEl) return;
      if (!open) {
        lastReserve = null;
        horizon.style.paddingBottom = "";
        if (echoEl) echoEl.style.bottom = "";
        if (scrimEl) { scrimEl.classList.remove("on"); scrimEl.style.height = "0px"; }
        return;
      }
      var bottom = stageEl.getBoundingClientRect().bottom;
      // the echo's default home is 40vh above the bottom, which with the plan
      // open lands it across the hour labels. Pin it to the reply's ACTUAL
      // top instead, so your words and Blink's stay one block (P11-03).
      if (echoEl) {
        if (surfaceEl && surfaceEl.classList.contains("show")) {
          var sTop = surfaceEl.getBoundingClientRect().top;
          echoEl.style.bottom = Math.round(bottom - sTop + 8) + "px";
        } else {
          echoEl.style.bottom = "";
        }
      }
      var top = convoTop();
      var band = (top == null) ? 0 : Math.max(0, bottom - top);
      var vh = window.innerHeight || 800;
      // the plan never gets squeezed past half the screen; beyond that the
      // scrim carries legibility and the plan keeps scrolling underneath
      var reserve = Math.max(52, Math.min(band + 22, Math.round(vh * 0.5)));
      if (reserve !== lastReserve) {          // write only on a real change
        lastReserve = reserve;
        horizon.style.paddingBottom = reserve + "px";
      }
      if (scrimEl) {
        var somethingUp =
          (surfaceEl && surfaceEl.classList.contains("show")) ||
          (echoEl && echoEl.classList.contains("show"));
        // the scrim covers the words and their feather, never the whole plan:
        // a long reply must not wash the week away, only the band it sits in
        var scrimH = Math.min(Math.max(band, 0) + 70, Math.round(vh * 0.46));
        scrimEl.style.height = Math.round(scrimH) + "px";
        scrimEl.classList.toggle("on", !!somethingUp && band > 0);
      }
    }
    // the band changes when the reply arrives, when the echo lands, when the
    // dock finishes fading, and on resize — so re-measure on all of them
    // The band moves for a dozen reasons — a reply typing on, the field
    // mounting, the dock finishing its fade, a rotate. Rather than try to
    // name them all, the measurement simply runs while the plan is open and
    // only WRITES when the answer changes, so it can never be stale.
    var reserveTimer = null;
    function reserveSoon() {
      reserveSpace();
      clearInterval(reserveTimer);
      if (!open) return;
      reserveTimer = setInterval(reserveSpace, 220);
    }
    if (window.MutationObserver) {
      var mo = new MutationObserver(reserveSoon);
      if (surfaceEl) mo.observe(surfaceEl, { attributes: true, attributeFilter: ["class"], childList: true, subtree: true });
      if (echoEl) mo.observe(echoEl, { attributes: true, attributeFilter: ["class"] });
    }
    window.addEventListener("resize", reserveSpace);
    // Instant contexts: reduced motion, or a hidden tab (transitions/timers
    // throttle there) — the class toggle IS the end state, no beats.
    function instant() { return reduce || document.visibilityState === "hidden"; }

    // The rise: parked eyes sit vertically centred in a ~90px top band.
    // Measured from the rig's LAYOUT position (offsetTop — transforms never
    // move it), so it's exact on any viewport; recomputed on resize.
    function measureRise() {
      if (!rig || !stageEl) return;
      var centerY = stageEl.getBoundingClientRect().top + rig.offsetTop + rig.offsetHeight / 2;
      appEl.style.setProperty("--park-rise", (46 - centerY).toFixed(1) + "px");
    }
    window.addEventListener("resize", function () { if (open) measureRise(); });

    // The GSAP flourish: a brief glint brighten as the spread begins.
    // Guarded — without GSAP the open reads the same, just without the wink.
    function glintBrighten() {
      if (!window.gsap) return;
      try {
        window.gsap.fromTo(".eye .glint",
          { opacity: 1, filter: "blur(0.5px)" },
          { opacity: 0.85, filter: "blur(1px)", duration: 0.5, delay: 0.15,
            clearProps: "opacity,filter" });
      } catch (_) { /* flourish only — never load-bearing */ }
    }

    function openHorizon() {
      if (open) { render(); return; }   // already open: just freshen the plan
      open = true;
      wantsReveal = false;
      // Opening the plan does NOT cut the voice (user report, 2026-08-27).
      // Looking at part of the app is not an interrupt — only starting to
      // speak or send is. The surface still recedes to surface-min below so
      // the two do not fight for space, which was the real need here; the
      // reply keeps talking and keeps its words while you read.
      measureRise();
      render();
      // surface interplay: a showing reply recedes instead of fighting for space
      surfaceWasUp = surfaceEl.classList.contains("show");
      if (surfaceWasUp) surfaceEl.classList.add("surface-min");
      // Choreography (P11-05): 0ms spread + glint brighten, then the class
      // flip runs the park while the plan is DRAWN UP from the band the eyes
      // are vacating — it starts pushed down and slightly small, which is
      // exactly the state the pull-down close settles into, so the two halves
      // are one object run in either direction. horizon.css owns the timing
      // (80ms in, 0.42s/0.5s travel = 580ms, against the close's 500ms), and
      // the day cards stagger ~45ms apart behind it.
      if (!instant() && eyesEl) {
        eyesEl.classList.add("spread");
        clearTimeout(spreadTimer);
        spreadTimer = setTimeout(function () { eyesEl.classList.remove("spread"); }, 250);
        glintBrighten();
      }
      appEl.classList.add("viewing");
      if (handle) {
        handle.setAttribute("aria-expanded", "true");
        handle.setAttribute("aria-label", "Hide your plan");
      }
      // a11y: focus moves into the horizon once it has materialized
      clearTimeout(focusTimer);
      focusTimer = setTimeout(function () {
        try { horizon.focus({ preventScroll: true }); } catch (_) {}
      }, instant() ? 0 : 480);          // once the sheet has settled (P11-05)
      openedAt = Date.now();
      reserveSoon();                     // P11-03: the plan takes the dock's room back
    }

    function closeHorizon() {
      if (!open) return;
      open = false;
      clearTimeout(spreadTimer);
      clearTimeout(soonTimer);
      if (eyesEl) eyesEl.classList.remove("spread");
      // reverse: the horizon eases out over 200ms, then the rig unparks
      // (500ms, delayed 200ms) — all off the same class flip.
      appEl.classList.remove("viewing");
      if (handle) {
        handle.setAttribute("aria-expanded", "false");
        handle.setAttribute("aria-label", "Show your plan");
      }
      if (surfaceWasUp && surfaceEl.classList.contains("show")) {
        surfaceEl.classList.remove("surface-min");
      }
      surfaceWasUp = false;
      setDrag(null);                     // P11-03: drop any follow-the-finger offset
      clearInterval(reserveTimer);
      reserveSpace();
      hz.dismissPopover();               // no stranded block popover on reopen
      // a11y: focus returns to the mic
      clearTimeout(focusTimer);
      var mic = document.getElementById("mic");
      if (mic) { try { mic.focus({ preventScroll: true }); } catch (_) {} }
    }

    function toggle() { if (open) closeHorizon(); else openHorizon(); }

    // openSoon(): the planned-reply hinge — the satisfied slow blink lands
    // first (~600ms), then the morph. Instant contexts skip the wait.
    function openSoon() {
      clearTimeout(soonTimer);
      if (instant()) { openHorizon(); return; }
      soonTimer = setTimeout(openHorizon, 620);
    }

    // Typed intent: "show my week", "open the plan", "my schedule" … arms an
    // open that the controller fires once the reply finishes rendering.
    var INTENT = /\b(show|see|view|open)\b[\s\S]*\b(week|day|plan|schedule|calendar)\b/i;
    var INTENT_MY = /\bmy (week|day|schedule|plan)\b/i;
    function noteIntent(text) {
      wantsReveal = INTENT.test(text) || INTENT_MY.test(text);
    }
    function consumeIntent() { var w = wantsReveal; wantsReveal = false; return w; }

    /* --- manual affordances --------------------------------------------- */
    if (handle) handle.addEventListener("click", toggle);
    // clicking the parked eyes brings the conversation back
    if (rig) rig.addEventListener("click", function () { if (open) closeHorizon(); });
    // wheel-down anywhere on the stage opens (once open the horizon scrolls
    // itself); a reply scrolling inside the surface never triggers it
    if (stageEl) stageEl.addEventListener("wheel", function (e) {
      if (open) return;
      if (e.target && e.target.closest && e.target.closest("#surface")) return;
      if (e.deltaY > 14) openHorizon();
    }, { passive: true });
    /* --- leaving the plan should be as easy as entering it (P11-03) -----
       The old rule closed on a swipe-down ONLY while horizon.scrollTop <= 2,
       so the gesture went silently dead the moment you had scrolled the plan
       at all — which is what made closing feel awkward. Now:
         · a downward drag from the top of the plan (or an over-scroll there)
           takes the sheet with it and settles, so the gesture is physical
           rather than a jump;
         · a decisive swipe-down closes from ANY scroll position;
         · a wheel/trackpad over-scroll at the top closes the same way, so
           mouse users get the gesture too;
         · the close now runs on the open's own 0.5s character (horizon.css)
           instead of a 200ms snap.
       Esc, the parked eyes and the peek handle all keep working, and the
       handle itself says "pull down to close" while the plan is up. */
    function setDrag(dy) {
      if (!horizon) return;
      if (dy == null) { horizon.style.transform = ""; horizon.style.opacity = ""; return; }
      var f = Math.min(1, dy / 280);
      horizon.style.transform =
        "translateY(" + Math.round(dy * 0.55) + "px) scale(" + (1 - f * 0.03).toFixed(4) + ")";
      horizon.style.opacity = String(1 - f * 0.5);
    }

    var touchY = null, dragging = false;
    document.addEventListener("touchstart", function (e) {
      if (e.touches && e.touches.length > 1) { touchY = null; endDrag(0); return; }
      touchY = e.touches && e.touches[0] ? e.touches[0].clientY : null;
      dragging = false;
    }, { passive: true });

    document.addEventListener("touchmove", function (e) {
      if (touchY == null || !open || reduce) return;
      if (!e.touches || e.touches.length !== 1) return;
      var dy = e.touches[0].clientY - touchY;
      if (!dragging) {
        // the sheet takes the gesture only from the top of the plan, so
        // scrolling the plan itself is never stolen
        if (dy < 14 || horizon.scrollTop > 2) return;
        dragging = true;
        appEl.classList.add("dragging-close");
      }
      if (e.cancelable) e.preventDefault();     // the drag is ours now
      setDrag(Math.max(0, dy));
    }, { passive: false });

    function endDrag(dy) {
      if (!dragging) return false;
      dragging = false;
      appEl.classList.remove("dragging-close");
      setDrag(null);
      if (dy > 90) { closeHorizon(); return true; }
      return true;
    }

    document.addEventListener("touchend", function (e) {
      var from = touchY; touchY = null;
      var dy = (from != null && e.changedTouches && e.changedTouches[0])
        ? e.changedTouches[0].clientY - from : 0;
      if (endDrag(dy)) return;
      if (!open && dy < -60) openHorizon();
      // no scrollTop gate: a decisive swipe-down always closes
      else if (open && dy > 70) closeHorizon();
    }, { passive: true });

    // wheel/trackpad: an over-scroll at the top of the plan closes it, the
    // same gesture as the swipe, so this is not a touch-only door
    var closeWheel = 0, closeWheelAt = 0, openedAt = 0;
    if (horizon) horizon.addEventListener("wheel", function (e) {
      if (!open || e.ctrlKey) return;          // ctrl+wheel is the zoom (P7-06)
      var now = Date.now();
      if (now - openedAt < 600) return;        // never close on the open's own momentum
      if (now - closeWheelAt > 400) closeWheel = 0;
      closeWheelAt = now;
      // deliberate only: at the top, pulling up, past a real distance. A
      // stray one-notch scroll must never dismiss the plan.
      if (horizon.scrollTop > 2 || e.deltaY > -10) { closeWheel = 0; return; }
      closeWheel += -e.deltaY;
      if (closeWheel > 200) { closeWheel = 0; closeHorizon(); }
    }, { passive: true });
    // Esc closes. This listener registers before the surface's Esc dismiss,
    // so one press closes the horizon ONLY — the rest of the chain is cut.
    // An open block popover (P7-06) eats the first Esc; the horizon the next.
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !open) return;
      if (!hz.dismissPopover()) closeHorizon();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    });

    /* --- the horizon body (P7-06): the zoomable plan canvas -------------- */
    var hz = createHorizon(instant);
    function render() { hz.render(); }

    // Keyboard (P7-09): once focus lands on the horizon itself (it does on
    // open), ← → switch levels without tabbing to the pills first. The pills
    // keep their own arrow nav — this only fires on the region element.
    if (horizon) horizon.addEventListener("keydown", function (e) {
      if (e.target !== horizon) return;
      if (e.key === "ArrowRight") { e.preventDefault(); hz.stepLevel(1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); hz.stepLevel(-1); }
    });

    // refresh(): re-render the horizon content when it's up (calendar sync,
    // FocusRefresh) — a closed horizon just renders fresh on its next open.
    function refresh() { if (open) hz.refresh(); }

    // P11-08: open the plan AT a level+date (the reply-reference entry point).
    // The horizon paints on open, so the anchor is set first either way.
    function openAt(level, date) {
      hz.goTo(level, date);
      if (!open) openHorizon();
    }

    return {
      open: openHorizon, close: closeHorizon, toggle: toggle,
      isOpen: isOpen, openSoon: openSoon, openAt: openAt,
      noteIntent: noteIntent, consumeIntent: consumeIntent,
      render: render, refresh: refresh, stageDiff: hz.stageDiff,
    };
  }

  /* =====================================================================
     Horizon (P7-06) — the zoomable plan canvas inside the stage.

     One canvas, five zoom levels: day | week | month | quarter | year.
     Week and day are fully built; month/quarter/year show the designed
     "coming into focus" placeholder until P7-08 fills them in.

     Data: one /details?days=N fetch per level-width (day+week share N=7,
     month 35, quarter 92, year 366), cached 30s per N — a narrower level
     happily reads a fresh wider fetch, so zooming out then back in costs
     nothing. Level + anchor day persist through FocusSettings
     ("horizonLevel"), so a reopened app lands where you left it.

     Zoom: pill click, arrow keys on the tablist, ctrl+wheel on the canvas,
     or a two-finger pinch. Level swaps are a 260ms shared-axis cross-fade
     between two stacked layers — the outgoing scales toward the zoom
     direction while the incoming does the inverse. Reduced-motion and
     hidden tabs swap instantly.

     DOM is built with el() + textContent only — task titles are data,
     never markup. Maps to <Horizon level={...} /> in React.
     ===================================================================== */
  function createHorizon(instant) {
    var LEVELS = ["day", "week", "month", "quarter", "year"];
    // Year reads milestones / commitments / profile only — every /details
    // response carries those regardless of window — so it never pays for a
    // 366-day ledger: any fresh cached fetch satisfies its width, and a
    // cold cache costs just the 7-day default.
    var WIDTH = { day: 7, week: 7, month: 35, quarter: 92, year: 7 };
    /* P11-13: A LEVEL QUOTES ITS OWN WINDOW, NEVER THE PAYLOAD'S.
       The 30s cache deliberately lets a WIDER fetch satisfy a narrower level
       (zoom out to month, zoom back to week, no second request). Every
       sentence that reads `ledger_days.length` therefore quoted whatever the
       cache happened to hold: the week support line read "across the next 35
       days" after a visit to month. A real number with the wrong scope is
       still a false claim. So the model is TRIMMED to the level's own window
       before a single word or mark is drawn, and every renderer, headline,
       support line and meta line reads the trimmed list. The trim covers the
       ledger only: `data.blocks` is the whole store at every width (see the
       /details handler), so measured fills and milestone hours are unchanged
       by it, and the per-day maps are narrowed to the same window so the
       shared axis cannot be widened by a day this level is not drawing. */
    var LEVEL_DAYS = WIDTH;
    function scopeToLevel(m, lv) {
      var n = LEVEL_DAYS[lv] || 7;
      var all = m.data.ledger_days || [];
      if (all.length <= n) return m;
      // the day level's window follows its anchor, so opening a day three
      // weeks out from the month grid still lands on that day
      var from = 0;
      if (lv === "day") {
        var ai = -1;
        all.forEach(function (d2, i) { if (d2.date === state.anchorDate) ai = i; });
        if (ai > 0) from = Math.max(0, Math.min(ai, all.length - n));
      }
      var days = all.slice(from, from + n);
      var keep = {};
      days.forEach(function (d2) { keep[d2.date] = true; });
      var byDate = {}, findings = {};
      Object.keys(m.byDate).forEach(function (k) { if (keep[k]) byDate[k] = m.byDate[k]; });
      Object.keys(m.findingsByDate).forEach(function (k) { if (keep[k]) findings[k] = m.findingsByDate[k]; });
      var data = Object.assign({}, m.data, { ledger_days: days });
      return Object.assign({}, m, { data: data, byDate: byDate, findingsByDate: findings });
    }

    var titleEl = document.getElementById("h-title");
    var streakEl = document.getElementById("hz-streak");
    var subEl = document.getElementById("h-sub");
    var metaEl = document.getElementById("h-meta");
    var pillsEl = document.getElementById("hz-pills");
    var gaugeEl = document.getElementById("hz-gauge");
    var unplacedEl = document.getElementById("hz-unplaced");
    var legendEl = document.getElementById("hz-legend");   // the visible key (P11-03)
    var canvasEl = document.getElementById("hz-canvas");
    // a11y (P7-09): the canvas is the panel the level tabs control; its
    // aria-label follows the level (set per render in renderChrome)
    if (canvasEl) canvasEl.setAttribute("role", "tabpanel");
    var layerEls = canvasEl ? canvasEl.querySelectorAll(".hz-layer") : [];
    var layerA = layerEls[0], layerB = layerEls[1];
    var active = layerA;

    var state = { level: "week", anchorDate: todayISO() };
    var restored = false;          // FocusSettings restore happens lazily —
                                   // the store is constructed after the stage
    var renderSeq = 0;             // stale-response guard across fetches
    var swapTimer = null;

    /* ---------- tiny DOM + format helpers ---------- */
    function el(tag, cls, text) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    }
    function pad2(n) { return (n < 10 ? "0" : "") + n; }
    function todayISO() {
      var d = new Date();
      return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
    }
    function fmtDay(iso) {
      var d = new Date(iso + "T00:00:00");
      if (isNaN(d)) return iso;
      return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    }
    function fmtDayLong(iso) {
      var d = new Date(iso + "T00:00:00");
      if (isNaN(d)) return iso;
      return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    }
    function fmtTime(iso) {
      var d = new Date(iso);
      if (isNaN(d)) return "";
      return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    }
    function hrs(min) { return (min / 60).toFixed(min % 60 ? 1 : 0); }
    /* A commitment's NAME (P11-03). When a goal arrives as a brain dump the
       stored title can be the whole paragraph, and every place that showed it
       clipped it mid-word — "Also I am prepping for a conference talk in six
       weeks. Outli". A name is short and has no sentence in it; anything else
       gets its first clause if that reads as a name, and otherwise nothing at
       all. Saying nothing is honest; a truncated fragment is not. */
    function commitmentName(c) {
      var t = ((c && c.title) || "").trim();
      if (!t) return "";
      if (t.length <= 44 && !/[.!?]/.test(t)) return t;
      var first = t.split(/[.!?\n]/)[0].trim();
      return (first.length >= 3 && first.length <= 44) ? first : "";
    }
    // minute-of-day for an ISO datetime, in the viewer's clock
    function minOf(iso) {
      var d = new Date(iso);
      return isNaN(d) ? null : d.getHours() * 60 + d.getMinutes();
    }

    /* ---------- level state: restore + persist (FocusSettings) ---------- */
    function restoreOnce() {
      if (restored || !window.FocusSettings) return;
      restored = true;
      try {
        var saved = window.FocusSettings.get("horizonLevel");
        if (saved && LEVELS.indexOf(saved.level) !== -1) state.level = saved.level;
        if (saved && /^\d{4}-\d{2}-\d{2}$/.test(saved.anchorDate || "")) state.anchorDate = saved.anchorDate;
      } catch (_) { /* corrupt state — the defaults stand */ }
    }
    function persistState() {
      try {
        if (window.FocusSettings) {
          window.FocusSettings.set("horizonLevel", { level: state.level, anchorDate: state.anchorDate });
        }
      } catch (_) { /* persistence is a nicety, never load-bearing */ }
    }

    /* ---------- data: one fetch per level-width, cached 30s ---------- */
    var cache = {};                // days-width -> { at, data }
    var CACHE_TTL = 30000;
    function fetchDetails(n) {
      // a fresh wider fetch covers a narrower ask — reuse the tightest one
      var now = Date.now(), hit = null;
      Object.keys(cache).forEach(function (k) {
        var kn = +k, e = cache[k];
        if (kn >= n && now - e.at < CACHE_TTL && (!hit || kn < hit.n)) hit = { n: kn, data: e.data };
      });
      if (hit) return Promise.resolve(hit.data);
      return api("/details?days=" + n).then(function (d) {
        cache[n] = { at: Date.now(), data: d };
        return d;
      });
    }

    // The joined shape every renderer reads: task + commitment lookups,
    // blocks grouped per calendar date, and findings mapped onto the days
    // whose blocks their entity_ref tasks touch.
    function buildModel(d) {
      var tasks = {}, commitments = {}, byDate = {}, findingsByDate = {};
      (d.tasks || []).forEach(function (t) { tasks[t.id] = t; });
      (d.commitments || []).forEach(function (c) { commitments[c.id] = c; });
      (d.blocks || []).forEach(function (b) {
        if (b.status === "cancelled") return;
        var key = (b.starts_at || "").slice(0, 10);
        (byDate[key] = byDate[key] || []).push(b);
      });
      Object.keys(byDate).forEach(function (k) {
        byDate[k].sort(function (a, b) { return a.starts_at < b.starts_at ? -1 : 1; });
      });
      (d.findings || []).forEach(function (f) {
        var tid = f.entity_ref && f.entity_ref.task_id;
        if (!tid) return;
        Object.keys(byDate).forEach(function (k) {
          var touches = byDate[k].some(function (b) { return b.task_id === tid; });
          if (touches) findingsByDate[k] = (findingsByDate[k] || 0) + 1;
        });
      });
      // ONE CLOCK (P11-03). Every date in this payload was stamped by the
      // server's clock: ledger_days[].date, blocks[].starts_at, the free
      // windows. The browser's own Date() disagrees with that clock for part
      // of every day in any timezone that is not UTC, which is exactly how
      // week could show four sessions on Wednesday while day opened an empty
      // Thursday. So "today" and "now" come from the same response as the
      // data they describe. If an older server has not sent them, we degrade
      // to the browser's clock rather than fabricate one.
      var today = /^\d{4}-\d{2}-\d{2}$/.test(d.today || "") ? d.today : todayISO();
      var nowMin = d.now ? minOf(d.now) : null;
      if (nowMin == null) {
        var localNow = new Date();
        nowMin = localNow.getHours() * 60 + localNow.getMinutes();
      }
      return {
        data: d, tasks: tasks, commitments: commitments,
        byDate: byDate, findingsByDate: findingsByDate,
        today: today, nowMin: nowMin,
      };
    }

    /* ---------- the level pills (tablist, arrow-key nav) ---------- */
    var pillBtns = {};
    LEVELS.forEach(function (lv) {
      var b = el("button", "hz-pill", lv);
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", "false");
      b.tabIndex = -1;
      b.addEventListener("click", function () { setLevel(lv); });
      if (pillsEl) pillsEl.appendChild(b);
      pillBtns[lv] = b;
    });
    if (pillsEl) pillsEl.addEventListener("keydown", function (e) {
      var i = LEVELS.indexOf(state.level);
      if (e.key === "ArrowRight" && i < LEVELS.length - 1) { e.preventDefault(); setLevel(LEVELS[i + 1], true); }
      else if (e.key === "ArrowLeft" && i > 0) { e.preventDefault(); setLevel(LEVELS[i - 1], true); }
    });
    function syncPills() {
      LEVELS.forEach(function (lv) {
        var b = pillBtns[lv];
        var on = lv === state.level;
        b.classList.toggle("on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
      });
    }

    /* ---------- zoom: pills / keys / ctrl+wheel / pinch ---------- */
    function setLevel(lv, focusPill) {
      if (LEVELS.indexOf(lv) === -1 || lv === state.level) return;
      var zoomIn = LEVELS.indexOf(lv) < LEVELS.indexOf(state.level);
      // zooming into the day targets today unless an arrow-picked anchor
      // is already set (buildDay clamps it into the fetched range)
      state.level = lv;
      persistState();
      syncPills();
      if (focusPill) { try { pillBtns[lv].focus(); } catch (_) {} }
      renderInto(spare(), true, zoomIn);
    }
    function stepLevel(dir) {          // dir -1 zooms in (toward day)
      var i = LEVELS.indexOf(state.level) + dir;
      if (i >= 0 && i < LEVELS.length) setLevel(LEVELS[i]);
    }
    var wheelAcc = 0, wheelCool = 0;
    if (canvasEl) canvasEl.addEventListener("wheel", function (e) {
      if (!e.ctrlKey) return;          // plain wheel keeps scrolling the plan
      e.preventDefault();
      var now = Date.now();
      if (now < wheelCool) return;
      wheelAcc += e.deltaY;
      if (Math.abs(wheelAcc) < 24) return;
      stepLevel(wheelAcc > 0 ? 1 : -1);
      wheelAcc = 0; wheelCool = now + 350;
    }, { passive: false });
    var pinchD = null;
    if (canvasEl) {
      canvasEl.addEventListener("touchstart", function (e) {
        pinchD = e.touches.length === 2 ? touchDist(e.touches) : null;
      }, { passive: true });
      canvasEl.addEventListener("touchmove", function (e) {
        if (pinchD == null || e.touches.length !== 2) return;
        e.preventDefault();            // the pinch is ours, not the page's
        var d = touchDist(e.touches);
        if (Math.abs(d - pinchD) < 48) return;
        stepLevel(d > pinchD ? -1 : 1);  // fingers spreading = zoom in
        pinchD = d;
      }, { passive: false });
      canvasEl.addEventListener("touchend", function () { pinchD = null; }, { passive: true });
    }
    function touchDist(t) {
      var dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }

    /* ---------- the replan diff (P9-01) ----------
       stageDiff(res) parks a /turn `replanned` payload; the NEXT week render
       plays it: ghost chips fade out at the cancelled slots, the re-placed
       chips spring in with a glow pulse, and a summary chip counts up what
       moved. GSAP choreographs when present; without it the ghosts still
       fade via CSS and the chips simply appear glowing — the DOM ends
       correct either way (frontend-standards fallback rule). */
    var pendingDiff = null;
    function stageDiff(res) {
      if (!res || ((res.cancelled_blocks || 0) + (res.rescheduled_blocks || 0)) === 0) return;
      pendingDiff = res;
      // the diff reads on the week canvas — steer the next open there
      if (state.level !== "week") { state.level = "week"; persistState(); }
    }

    function diffSummaryText(d) {
      var moved = d.rescheduled_blocks || 0, cancelled = d.cancelled_blocks || 0;
      if (cancelled === 0) return moved + " re-placed · today untouched";
      if (moved >= cancelled) return moved + (moved === 1 ? " session" : " sessions") + " moved · nothing lost";
      return moved + " moved · " + (cancelled - moved) + " still need room";
    }

    function animateDiff(layer, d, m) {
      var g = window.gsap;
      // 1. summary chip, counted up over ~600ms, self-fading
      var chipEl = el("div", "hz-diff-chip");
      var target = d.rescheduled_blocks || 0;
      chipEl.textContent = diffSummaryText(d);
      var wk = layer.querySelector(".hz-week");
      if (wk) wk.insertBefore(chipEl, wk.firstChild);
      requestAnimationFrame(function () { chipEl.classList.add("show"); });
      if (!reduce && target > 1) {
        var t0 = null;
        var step = function (ts) {
          if (t0 === null) t0 = ts;
          var p = Math.min(1, (ts - t0) / 600);
          chipEl.textContent = diffSummaryText(
            Object.assign({}, d, { rescheduled_blocks: Math.max(1, Math.round(target * p)) }));
          if (p < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      }
      setTimeout(function () { chipEl.classList.remove("show"); }, 7000);
      setTimeout(function () { if (chipEl.parentNode) chipEl.parentNode.removeChild(chipEl); }, 7800);

      // 2. ghosts at the cancelled slots
      (d.cancelled_blocks_detail || []).forEach(function (c, i) {
        var date = (c.starts_at || "").slice(0, 10);
        var card = layer.querySelector('.hz-daycard[data-date="' + date + '"]');
        if (!card) return;
        var body = card.querySelector(".hz-dc-body");
        if (!body) return;
        var t = (m.tasks && m.tasks[c.task_id]) || {};
        // P11-03: the week is a run now, so a ghost sits at the SLOT the
        // cancelled block used to hold, on the same shared axis as everything
        // else. Same geometry the run itself was drawn with.
        var ghost = el("div", "hz-span hz-ghost");
        ghost.appendChild(el("span", "hz-span-title", t.title || "Session"));
        ghost.appendChild(el("span", "hz-span-time", fmtTime(c.starts_at) + "–" + fmtTime(c.ends_at)));
        var gs = minOf(c.starts_at), ge = minOf(c.ends_at);
        if (lastGeom && gs != null && ge != null) {
          ghost.style.left = lastGeom.pct(gs) + "%";
          ghost.style.width = Math.max(0.6, lastGeom.pct(ge) - lastGeom.pct(gs)) + "%";
        }
        body.appendChild(ghost);
        if (g && !reduce) {
          g.to(ghost, { opacity: 0, scaleX: 0.55,
                        duration: 0.8, delay: 1.1 + i * 0.12, ease: "power2.in",
                        onComplete: function () { if (ghost.parentNode) ghost.parentNode.removeChild(ghost); } });
        } else {
          setTimeout(function () { ghost.classList.add("hz-ghost-out"); }, 1100 + i * 120);
          setTimeout(function () { if (ghost.parentNode) ghost.parentNode.removeChild(ghost); }, 2200 + i * 120);
        }
      });

      // 3. the re-placed chips spring in and pulse
      (d.moved_blocks_detail || []).forEach(function (mv, i) {
        var chip2 = layer.querySelector('[data-block-id="' + mv.id + '"]');
        if (!chip2) return;
        chip2.classList.add("hz-moved");
        if (g && !reduce) {
          g.from(chip2, { y: -16, opacity: 0, scale: 0.92,
                          duration: 0.55, delay: 0.35 + i * 0.1, ease: "back.out(1.7)" });
        }
        setTimeout(function () { chip2.classList.remove("hz-moved"); }, 4200 + i * 100);
      });
    }

    /* ---------- render pipeline + the 260ms shared-axis swap ---------- */
    function spare() { return active === layerA ? layerB : layerA; }

    function render() {                // fresh paint of the current level
      restoreOnce();
      syncPills();
      renderInto(active, false, false);
    }
    function refresh() { render(); }

    /* P11-05: the level change no longer waits on the network.
       It used to: renderInto(…, viaSwap) skipped the loading state and awaited
       the fetch with the OLD level still on screen under the NEW pill, so any
       width change (week→month is 7 days→35, month→quarter 35→92, and a first
       visit is a cold cache) showed a dead gap with no sign that anything was
       happening. Now the new level's own FRAME goes up on this frame — its
       real grid, at its real proportions — the cross-fade starts against that,
       and the detail resolves into it when the data lands. When the 30s cache
       already holds the answer (day↔week, and anything zoomed back to) the
       promise settles in a microtask, before the browser has painted once, so
       the frame is never SEEN either: no gap, and no flash of a loading state.
       The scaffold is scenery, not data — it claims nothing about your plan. */
    function renderInto(layer, viaSwap, zoomIn) {
      var seq = ++renderSeq;
      var lv = state.level;
      layer.textContent = "";
      layer.appendChild(buildScaffold(lv));
      renderChrome(lv, null, true);      // pending chrome: what we know already
      if (viaSwap) swapLayers(layer, zoomIn);
      // Did the frame ever reach the screen? On a warm cache the fetch settles
      // in a microtask, before the browser paints, so the scaffold is never
      // seen and the shared-axis swap alone carries the level change. Only a
      // frame the user actually SAW gets the detail's settle-in beat.
      var framePainted = false;
      requestAnimationFrame(function () { framePainted = true; });
      fetchDetails(WIDTH[lv])
        .then(function (d) {
          if (seq !== renderSeq) return;         // a newer render superseded us
          var m = scopeToLevel(buildModel(d), lv);   // P11-13: this level's own window
          layer.textContent = "";
          layer.appendChild(buildLevel(lv, m));
          paintLive(layer);              // P11-04: a running timer fills its own span
          renderChrome(lv, m);
          if (framePainted) settleIn(layer);   // the detail resolves into the frame
          // play a staged replan diff once the week canvas is really in the DOM
          if (pendingDiff && lv === "week") {
            var pd = pendingDiff; pendingDiff = null;
            requestAnimationFrame(function () { animateDiff(layer, pd, m); });
          }
        })
        .catch(function () {
          if (seq !== renderSeq) return;
          layer.textContent = "";
          layer.appendChild(emptyCard(
            "Couldn't reach your plan just now.",
            "The plan will be here when the connection is back."));
          renderChrome(lv, null);
          if (framePainted) settleIn(layer);
        });
    }

    /* The detail arriving is its own small beat: the frame is already there,
       so the content only has to come UP into it. Opacity + transform, one
       class, no GSAP — if the class never lands the DOM is still correct. */
    function settleIn(layer) {
      if (instant()) return;
      layer.classList.add("hz-settle");
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { layer.classList.remove("hz-settle"); });
      });
    }

    /* The level's frame, with its data still pending. Deliberately structural
       and deliberately mute: the right grid at the right proportions and not
       one label, count or date, so it can never imply a plan that has not
       arrived (degrade, never fabricate). */
    function buildScaffold(lv) {
      var wrap = el("div", "hz-scaffold hz-scaffold-" + lv);
      wrap.setAttribute("aria-hidden", "true");
      drew = {};      // nothing is drawn yet, so the key promises nothing yet
      var i;
      if (lv === "week" || lv === "day") {
        var rows = el("div", "hz-sk-rows");
        for (i = 0; i < (lv === "week" ? 7 : 5); i++) {
          var row = el("div", "hz-sk-row");
          row.style.setProperty("--i", i);
          rows.appendChild(row);
        }
        wrap.appendChild(el("div", "hz-sk-axis"));
        wrap.appendChild(rows);
      } else if (lv === "month") {
        for (i = 0; i < 5; i++) {
          var mrow = el("div", "hz-sk-mrow");
          mrow.style.setProperty("--i", i);
          for (var c = 0; c < 7; c++) mrow.appendChild(el("div", "hz-sk-cell"));
          wrap.appendChild(mrow);
        }
      } else {
        // quarter and year both read as a band with lanes under it: the
        // quarter's band is its dated ruler and its lanes carry a measured
        // strip each, the year's band is the four quarter arcs and its lanes
        // are the commitment runs. Heights are set per level in horizon.css
        // (.hz-scaffold-quarter / .hz-scaffold-year) so the frame that goes up
        // first is the proportion the content really lands at (P11-05/P11-06).
        wrap.appendChild(el("div", "hz-sk-band"));
        for (i = 0; i < (lv === "quarter" ? 4 : 2); i++) {
          var lane = el("div", "hz-sk-lane");
          lane.style.setProperty("--i", i);
          wrap.appendChild(lane);
        }
      }
      return wrap;
    }

    function swapLayers(incoming, zoomIn) {
      dismissPopover();
      var outgoing = active;
      active = incoming;
      clearTimeout(swapTimer);
      if (instant()) {                 // reduced motion / hidden tab: no beats
        outgoing.className = "hz-layer hz-hidden";
        outgoing.textContent = "";
        incoming.className = "hz-layer";
        return;
      }
      // Shared axis: the outgoing scales toward the zoom direction (in:
      // 1->1.06, out: 1->0.95) while the incoming runs the inverse. The
      // outgoing goes absolute so the canvas height belongs to the incoming
      // from the first frame.
      incoming.className = "hz-layer " + (zoomIn ? "hz-start-small" : "hz-start-big");
      outgoing.className = "hz-layer hz-abs";
      void incoming.offsetWidth;       // commit the start frames
      incoming.className = "hz-layer";
      outgoing.className = "hz-layer hz-abs " + (zoomIn ? "hz-end-big" : "hz-end-small");
      swapTimer = setTimeout(function () {
        outgoing.className = "hz-layer hz-hidden";
        outgoing.textContent = "";
      }, 300);
    }

    /* ---------- chrome: title, sub, meta, utilization gauge ----------
       P11-05: the headline, the support line and the meta line used to
       hard-cut via bare textContent while the canvas was still cross-fading,
       so the words changed a beat before the picture did. setText eases the
       old words out (130ms) and the new ones in (220ms) on opacity and a 4px
       translate, which composites and never reflows the header. Same value =
       no beat at all, so the pending pass followed by the real pass only
       animates the lines that genuinely changed. */
    function setText(node, text) {
      if (!node) return;
      text = text || "";
      if (node.textContent === text) return;
      if (instant()) { node.textContent = text; node.classList.remove("hz-txt-out"); return; }
      clearTimeout(node.__hzFade);
      node.classList.add("hz-txt-out");
      node.__hzFade = setTimeout(function () {
        node.textContent = text;
        node.classList.remove("hz-txt-out");
      }, 130);
    }

    // `pending` = the frame is up and the data is still in flight. Everything
    // the level knows WITHOUT the ledger is set now (the static titles and
    // support lines); everything that needs the ledger stays blank rather
    // than showing the level you just left.
    function renderChrome(lv, m, pending) {
      // P11-03: week and day lead with the FINDING, in words, computed from
      // the ledger. Every branch below is true by construction and degrades
      // to a plainer honest sentence when the data is thin.
      var titles = {
        day: fmtDayLong(state.anchorDate), week: "The next seven days",
        month: "The month ahead", quarter: "The quarter ahead", year: "The year ahead",
      };
      if (m && lv === "week") titles.week = weekFinding(m);
      setText(titleEl, titles[lv]);
      if (canvasEl) canvasEl.setAttribute("aria-label", "Plan: " + (titles[lv] || lv));
      renderLegend(lv);
      if (pending) {
        // the static half of the header, now; the ledger half when it lands
        if (streakEl) streakEl.hidden = true;
        setText(subEl,
          lv === "month" ? "Five weeks of pressure and room, with open horizon past the frontier."
          : lv === "quarter" ? "Each commitment on its own lane, paced toward its milestones."
          : lv === "year" ? "The long arc, and every milestone on the way to the goal."
          : "");
        setText(metaEl, "");
        // the gauge is left exactly as it is: it is the one piece of chrome
        // whose old value is not misleading, and holding it steady is what
        // lets it ANIMATE to the new level's number instead of restarting
        return;
      }
      // Streak counter (P9-03b): calm, flame-free, and only when it's real.
      if (streakEl) {
        var st = (m && m.data && typeof m.data.streak === "number") ? m.data.streak : 0;
        if (st >= 1) {
          streakEl.textContent = "Day " + st;
          streakEl.hidden = false;
        } else {
          streakEl.hidden = true;
        }
      }
      if (!m) {
        setText(subEl, "Couldn't reach your plan just now.");
        setText(metaEl, "");
        renderGauge(null);
        return;
      }
      var days = m.data.ledger_days || [];
      // P11-13 belt and braces: the model is already trimmed to this level's
      // window, and every day-count sentence below is additionally clamped to
      // it, so no sentence can quote a wider payload even if the trim is ever
      // bypassed. Never MORE than the level owns, never more than we fetched.
      var winDays = Math.min(days.length, LEVEL_DAYS[lv] || days.length);
      // sessions INSIDE this level's window. `data.blocks` is the whole store
      // at every width, so counting it made the week claim sessions that sit
      // months away; m.byDate is keyed by the window's own days (P11-13).
      var blockCount = 0;
      days.forEach(function (d2) { blockCount += (m.byDate[d2.date] || []).length; });
      if (subEl) {
        if (lv === "day") setText(subEl, dayFinding(m));
        else if (lv === "month") setText(subEl, "Five weeks of pressure and room, with open horizon past the frontier.");
        else if (lv === "quarter") setText(subEl, "Each commitment on its own lane, paced toward its milestones.");
        else if (lv === "year") setText(subEl, "The long arc, and every milestone on the way to the goal.");
        else setText(subEl, weekSupport(m, blockCount, winDays));
      }
      if (metaEl) {
        if (lv === "day") {
          var day = null;
          days.forEach(function (d2) { if (d2.date === state.anchorDate) day = d2; });
          var n = (m.byDate[state.anchorDate] || []).length;
          // names the day it is actually describing — this used to say
          // "today" whichever day the arrows had stepped to (P11-03)
          setText(metaEl, day
            ? n + " session" + (n === 1 ? "" : "s") +
              (state.anchorDate === m.today ? " today" : " on " + fmtDay(state.anchorDate)) +
              " · " + hrs(openMinutes(day, m)) + "h open"
            : "");
        } else if (lv === "year") {
          // the year never claims a ledger width it didn't fetch — count arcs
          var mile = m.data.milestones || [];
          var hit = mile.filter(function (x) { return x.status === "achieved"; }).length;
          setText(metaEl, mile.length
            ? mile.length + " milestone" + (mile.length === 1 ? "" : "s") + " · " + hit + " achieved"
            : "");
        } else if (lv === "quarter") {
          var act = (m.data.commitments || []).filter(function (c) { return c.status === "active"; }).length;
          var freeQ = days.slice(0, winDays).reduce(function (s, d2) { return s + openMinutes(d2, m); }, 0);
          setText(metaEl,
            act + " active commitment" + (act === 1 ? "" : "s") + " · " +
            hrs(freeQ) + "h free across " + winDays + " days");
        } else {
          // the same honest figure the headline quotes: room left AFTER the
          // sessions already sitting in it (P11-03)
          var totalFree = days.slice(0, winDays).reduce(function (s, d2) { return s + openMinutes(d2, m); }, 0);
          setText(metaEl,
            blockCount + " session" + (blockCount === 1 ? "" : "s") + " placed · " +
            hrs(totalFree) + "h open across " + winDays + " days");
        }
      }
      renderGauge(m.data.schedule_report || null);
    }

    /* ---------- the headline: the finding, in words (P11-03) ----------
       The spine's job is to make one fact obvious; the headline says that
       fact out loud so nobody has to squint for it. Every sentence here is
       derived deterministically from the ledger the canvas just drew, and
       each branch only claims what its own condition proves. Thin data gets
       a plainer sentence, never a louder one. */

    // "Thursday is your open day." — but only when one day genuinely stands
    // out: at least two free hours AND half again the typical day's room.
    function weekFinding(m) {
      var days = m.data.ledger_days || [];
      if (!days.length) return "Your week, waiting for a plan";
      var frees = days.map(function (d2) { return openMinutes(d2, m); }).slice().sort(function (a, b) { return a - b; });
      var median = frees[Math.floor(frees.length / 2)];
      var best = null, bestOpen = -1;
      days.forEach(function (d2) {
        var o = openMinutes(d2, m);
        if (o > bestOpen) { bestOpen = o; best = d2; }
      });
      var total = frees.reduce(function (s, x) { return s + x; }, 0);
      if (total < 60) return "This week is fully spoken for";
      if (best && bestOpen >= 120 && bestOpen >= median * 1.5) {
        var name = best.date === m.today ? "Today" : weekdayName(best.date);
        return name + " is your open day";
      }
      // no single day leads, so count the ones nothing has been placed on —
      // still a finding, and one the runs prove at a glance
      var clear = days.filter(function (d2) { return !(m.byDate[d2.date] || []).length; }).length;
      if (clear >= 2) return clear + " of your " + days.length + " days are wide open";
      return "Your week is evenly spread";
    }

    // `winDays` is the WEEK's own window, handed in by the caller (P11-13) —
    // never `ledger_days.length`, which is whatever the shared cache holds.
    function weekSupport(m, blockCount, winDays) {
      var days = ((m && m.data.ledger_days) || []).slice(0, winDays || 7);
      if (!days.length) return "Tell Blink what you're working toward, and this fills in.";
      var free = hrs(days.reduce(function (s, d2) { return s + openMinutes(d2, m); }, 0));
      if (!blockCount) return "Nothing placed yet, and " + free + "h of room waiting for you.";
      return blockCount + (blockCount === 1 ? " session placed" : " sessions placed") +
        ", and " + free + "h still open across the next " + days.length + " days.";
    }

    // The day's finding: what you're in, what's next, or how open it is.
    function dayFinding(m) {
      if (!m) return "";
      var days = m.data.ledger_days || [];
      var day = null;
      days.forEach(function (d2) { if (d2.date === state.anchorDate) day = d2; });
      var blocks = m.byDate[state.anchorDate] || [];
      var isToday = state.anchorDate === m.today;
      var free = hrs(day ? openMinutes(day, m) : 0);
      if (!blocks.length) {
        return isToday
          ? "Nothing placed for today, and " + free + "h of it is yours to spend."
          : "Nothing placed here yet, and " + free + "h of room to work with.";
      }
      var hero = heroBlock(blocks, m, isToday);
      var t = (hero && m.tasks[hero.block.task_id]) || {};
      var title = t.title || "your session";
      if (hero.kicker === "Right now") return "Right now: " + title + ", until " + fmtTime(hero.block.ends_at) + ".";
      if (hero.kicker === "Next up") return "Next up at " + fmtTime(hero.block.starts_at) + ": " + title + ".";
      if (hero.kicker === "Earlier today") {
        return "The day's sessions are behind you, and " + free + "h is still yours.";
      }
      return blocks.length + (blocks.length === 1 ? " session" : " sessions") +
        " waiting, starting at " + fmtTime(blocks[0].starts_at) + ".";
    }

    function weekdayName(iso) {
      var d = new Date(iso + "T00:00:00");
      return isNaN(d) ? iso : d.toLocaleDateString(undefined, { weekday: "long" });
    }

    /* ---------- the legend (P11-03): the key, in words ----------
       Meaning used to live only in `title` tooltips, which a touch device
       never shows. This names every mark out loud — and lists ONLY the marks
       the render actually drew (the `drew` flags), so the key can never
       promise something that is not on the canvas. */
    var LEGEND = {
      open:     ["hz-lg-open", "Open time"],
      // P11-04: the one convention, named in the same words at every level
      planned:  ["hz-lg-planned", "Placed, nothing recorded yet"],
      timer:    ["hz-lg-timer", "Time your timer measured"],
      reported: ["hz-lg-reported", "Time you told me about"],
      busy:     ["hz-lg-busy", "Already spoken for"],
      now:      ["hz-lg-now", "Right now"],
      press:    ["hz-lg-press", "How full the day is"],
      due:      ["hz-lg-due", "Something due"],
      hot:      ["hz-lg-hot", "More planned than there is room for"],
      today:    ["hz-lg-now", "Today"],
      dia:      ["hz-lg-dia", "A milestone, filled by measured hours"],
      flag:     ["hz-lg-flag", "A deadline"],
      star:     ["hz-lg-star", "Where the goal lands"],
    };
    var LEGEND_ORDER = {
      week: ["open", "planned", "timer", "reported", "busy", "now"],
      day: ["open", "planned", "timer", "reported", "busy", "now"],
      month: ["press", "timer", "reported", "due", "hot", "today"],
      // P11-06: the wide levels carry the same measured-time vocabulary as
      // the spine, so they list it in the same words
      quarter: ["dia", "flag", "planned", "timer", "reported", "today"],
      year: ["dia", "flag", "star", "planned", "timer", "reported", "today"],
    };
    function renderLegend(lv) {
      if (!legendEl) return;
      legendEl.textContent = "";
      var keys = (LEGEND_ORDER[lv] || []).filter(function (k) { return drew[k]; });
      if (!keys.length) { legendEl.hidden = true; return; }
      keys.forEach(function (k) {
        var spec = LEGEND[k];
        var item = el("span", "hz-lg");
        var mark = el("span", "hz-lg-mark " + spec[0], k === "star" ? "✦" : null);
        mark.setAttribute("aria-hidden", "true");
        item.appendChild(mark);
        item.appendChild(el("span", null, spec[1]));
        legendEl.appendChild(item);
      });
      legendEl.hidden = false;
    }

    // The slim utilization gauge: a small bar + mono % — calm accent under
    // 85, warm 85–100, alert over 100 with a "didn't fit" chip that toggles
    // the unplaced-reasons list. Absent report = no gauge at all.
    /* P11-05: the gauge is UPDATED, never rebuilt. It used to be torn down and
       reconstructed on every render, which is why the `width .4s` transition on
       .hz-g-fill could never once fire: a brand-new node has no previous value
       to travel from. The bar and the number are now built once and kept, so
       the fill really does travel between two levels' utilization and the
       number counts to it. The fill moves on transform: scaleX (not width) so
       the travel is a compositor job and costs no layout. */
    var gaugeParts = null;   // { bar, fill, num }
    var gaugePct = null;     // the number currently ON SCREEN
    var gaugeRaf = null;
    function buildGauge() {
      var bar = el("span", "hz-g-bar");
      var fill = el("span", "hz-g-fill");
      bar.appendChild(fill);
      var num = el("span", "hz-g-num", "");
      gaugeEl.appendChild(bar);
      gaugeEl.appendChild(num);
      return { bar: bar, fill: fill, num: num };
    }
    function countTo(num, from, to) {
      if (gaugeRaf) cancelAnimationFrame(gaugeRaf);
      gaugeRaf = null;
      if (instant() || from == null || from === to) { num.textContent = to + "%"; return; }
      // quick and calm: ~420ms, eased out, so it settles rather than spins
      var t0 = null, span = to - from;
      var step = function (ts) {
        if (t0 === null) t0 = ts;
        var p = Math.min(1, (ts - t0) / 420);
        var e = 1 - Math.pow(1 - p, 3);
        num.textContent = Math.round(from + span * e) + "%";
        if (p < 1) gaugeRaf = requestAnimationFrame(step);
        else { num.textContent = to + "%"; gaugeRaf = null; }
      };
      gaugeRaf = requestAnimationFrame(step);
    }
    function renderGauge(report) {
      if (!gaugeEl) return;
      if (unplacedEl) { unplacedEl.hidden = true; unplacedEl.textContent = ""; }
      // an absent report means no gauge at all — and the next one that arrives
      // starts from nothing rather than pretending to travel from a stale bar
      if (!report || report.utilization_pct == null) {
        if (gaugeRaf) { cancelAnimationFrame(gaugeRaf); gaugeRaf = null; }
        gaugeEl.textContent = "";
        gaugeParts = null; gaugePct = null;
        return;
      }
      var pct = Math.round(report.utilization_pct);
      var tone = pct > 100 ? "alert" : (pct >= 85 ? "warm" : "calm");
      if (!gaugeParts || !gaugeParts.bar.parentNode) {
        gaugeEl.textContent = "";
        gaugeParts = buildGauge();
        gaugePct = null;
      } else {
        // keep the bar + number, drop only the chip the old report owned
        var stale = gaugeEl.querySelector(".hz-g-chip");
        if (stale) gaugeEl.removeChild(stale);
      }
      var bar = gaugeParts.bar, fill = gaugeParts.fill, num = gaugeParts.num;
      bar.className = "hz-g-bar hz-g-" + tone;
      fill.style.transform = "scaleX(" + (Math.max(2, Math.min(100, pct)) / 100).toFixed(4) + ")";
      bar.title = hrs(report.total_planned_minutes || 0) + "h planned · " +
        (report.blocks_scheduled || 0) + " blocks scheduled";
      countTo(num, gaugePct, pct);
      gaugePct = pct;
      var un = report.unplaced || [];
      if (pct > 100 && un.length && unplacedEl) {
        var chip = el("button", "hz-g-chip", un.length + " didn't fit");
        chip.type = "button";
        chip.setAttribute("aria-expanded", "false");
        chip.addEventListener("click", function () {
          var show = unplacedEl.hidden;
          unplacedEl.hidden = !show;
          chip.setAttribute("aria-expanded", show ? "true" : "false");
        });
        gaugeEl.appendChild(chip);
        un.forEach(function (u) {
          var row = el("p", "hz-unplaced-row");
          row.appendChild(el("span", "hz-unplaced-title", u.title || u.task_id || "A task"));
          row.appendChild(el("span", "hz-unplaced-reason", u.reason || ""));
          unplacedEl.appendChild(row);
        });
      }
    }

    /* ---------- level bodies ---------- */
    function buildLevel(lv, m) {
      drew = {};                 // the legend lists only what this render draws
      if (lv === "week") return buildWeek(m);
      if (lv === "day") return buildDay(m);
      if (lv === "month") return buildMonth(m);
      if (lv === "quarter") return buildQuarter(m);
      return buildYear(m);
    }

    function emptyCard(title, sub) {
      var card = el("div", "hz-empty");
      card.appendChild(el("p", "hz-empty-title", title));
      card.appendChild(el("p", "hz-empty-sub", sub));
      return card;
    }

    /* ---------- shared helpers for the wide levels (P7-08) ---------- */
    // planned minutes inside one block (actuals win once they exist)
    function blockMinutes(b) {
      var s = new Date(b.starts_at), e = new Date(b.ends_at);
      if (isNaN(s) || isNaN(e)) return 0;
      return Math.max(0, (e - s) / 60000);
    }
    // "Nov 3" — the shape the pacing sentence and flags speak in
    function fmtMD(d) {
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }
    // one decimal, but "6" not "6.0" — hours as a person would say them
    function fmtH(h) {
      var r = Math.round(h * 10) / 10;
      return String(r % 1 ? r : Math.round(r));
    }

    /* --- measured hours behind a milestone (P11-04) ---
       Timer actuals only, summed per commitment from the blocks the browser
       already has, then poured into that commitment's milestones the same
       way src/core/progress.py pours derived hours: milestones in target-date
       order absorb their own target_hours, the last one takes the remainder.
       The server's `completed_hours` mixes in what you SAID you did and
       `derived_completed_hours` counts an un-recorded past block at its full
       planned length, so neither can fill a node under this convention. --- */
    function measuredHoursByCommitment(m) {
      var out = {};
      (m.data.blocks || []).forEach(function (b) {
        if (b.status === "cancelled" || b.status === "missed") return;
        if (b.actual_source !== "timer" || b.actual_minutes == null) return;
        var t = m.tasks[b.task_id];
        if (!t || !t.commitment_id) return;
        out[t.commitment_id] = (out[t.commitment_id] || 0) + b.actual_minutes / 60;
      });
      return out;
    }
    function measuredMilestoneHours(m) {
      var byC = measuredHoursByCommitment(m), groups = {}, out = {};
      (m.data.milestones || []).forEach(function (ms) {
        out[ms.id] = 0;
        if (ms.commitment_id) (groups[ms.commitment_id] = groups[ms.commitment_id] || []).push(ms);
      });
      Object.keys(groups).forEach(function (cid) {
        var remaining = byC[cid] || 0;
        if (remaining <= 0) return;
        var ordered = groups[cid].slice().sort(function (a, b) {
          var ad = a.target_date || "", bd = b.target_date || "";
          if (!ad !== !bd) return ad ? -1 : 1;      // dateless milestones last
          if (ad !== bd) return ad < bd ? -1 : 1;
          return a.id < b.id ? -1 : 1;
        });
        ordered.forEach(function (ms, i) {
          if (remaining <= 0) return;
          var take = (i === ordered.length - 1)
            ? remaining
            : Math.min(remaining, Math.max(ms.target_hours || 0, 0));
          out[ms.id] = take;
          remaining -= take;
        });
      });
      return out;
    }
    // one milestone mark's fill + its tooltip, shared by quarter and year so
    // the two levels can never disagree about what a filled node means
    function milestoneMark(node, ms, measured, dt) {
      var target = ms.target_hours || 0;
      var fill = target > 0 ? Math.min(1, measured / target) : 0;
      node.style.setProperty("--fill", Math.round(fill * 100) + "%");
      var parts = [ms.title || "Milestone"];
      parts.push(target > 0
        ? fmtH(measured) + "h measured of " + fmtH(target) + "h"
        : fmtH(measured) + "h measured, no target hours set yet");
      var claimed = (ms.completed_hours || 0) - measured;
      if (claimed >= 0.1) parts.push(fmtH(claimed) + "h more you told me about");
      if (dt) parts.push(fmtMD(new Date(dt)));
      node.title = parts.join(" · ");
      if (fill > 0) drew.timer = true;
    }

    /* --- one commitment's placed-vs-recorded time inside a window (P11-06) ---
       The wide levels used to carry the measured convention only inside a
       milestone node's fill, so a commitment with no milestones showed no
       evidence at all. This sums the SAME three states the spine draws, over
       the blocks the browser already has, for one commitment and one window.
       `data.blocks` is the whole store at every fetch width, so this figure
       does not move when the cache width does. --- */
    function commitmentMeasured(m, cid, fromMs, toMs) {
      var sum = { planned: 0, timer: 0, reported: 0, blocks: 0 };
      (m.data.blocks || []).forEach(function (b) {
        if (b.status === "cancelled") return;
        var t = m.tasks[b.task_id];
        if (!t || t.commitment_id !== cid) return;
        var s = new Date(b.starts_at).getTime();
        if (isNaN(s) || s < fromMs || s >= toMs) return;
        sum.blocks += 1;
        sum.planned += blockMinutes(b);
        var a = measuredOf(b);
        if (!a) return;
        if (a.source === "timer") sum.timer += a.minutes;
        else sum.reported += a.minutes;
      });
      return sum;
    }

    /* The strip that says it: the outline is the placed time, the solid share
       is the timer, the hatched share is what you reported. Nothing placed at
       all draws nothing, because there is nothing to be a share OF. The words
       beside it are the same words the legend uses. */
    function measuredStrip(sum, cls) {
      if (!sum.blocks || !(sum.planned > 0)) return null;
      var box = el("div", cls || "hz-ms");
      var bar = el("span", "hz-ms-bar");
      var tPct = Math.min(100, sum.timer / sum.planned * 100);
      var rPct = Math.min(100 - tPct, sum.reported / sum.planned * 100);
      if (tPct > 0) {
        var tf = el("span", "hz-ms-fill hz-fill-timer");
        tf.style.width = tPct + "%";
        bar.appendChild(tf);
        drew.timer = true;
      }
      if (rPct > 0) {
        var rf = el("span", "hz-ms-fill hz-fill-reported");
        rf.style.width = rPct + "%";
        bar.appendChild(rf);
        drew.reported = true;
      }
      if (!(tPct > 0) && !(rPct > 0)) drew.planned = true;
      var said = [];
      if (sum.timer) said.push(dur(sum.timer) + " your timer measured");
      if (sum.reported) said.push(dur(sum.reported) + " you told me about");
      var words = said.length
        ? said.join(" and ") + " of " + dur(sum.planned) + " placed"
        : dur(sum.planned) + " placed, waiting on its first session";
      bar.title = words;
      bar.setAttribute("aria-hidden", "true");
      box.appendChild(bar);
      box.appendChild(el("span", "hz-ms-words", words));
      return box;
    }

    /* --- month (P7-08): a 5-week calendar grid, Monday start --- */
    function buildMonth(m) {
      var days = m.data.ledger_days || [];
      var wrap = el("div", "hz-month");
      if (!days.length) {
        wrap.appendChild(emptyCard("Nothing to map yet.",
          "Tell Blink about a goal and the month will fill in here."));
        return wrap;
      }
      var byLedger = {};
      days.forEach(function (d2) { byLedger[d2.date] = d2; });

      // deadlines per date: tasks + live commitments, names kept for tooltips
      var deadlines = {};
      function noteDeadline(iso, name) {
        (deadlines[iso] = deadlines[iso] || []).push(name);
      }
      (m.data.tasks || []).forEach(function (t) {
        if (t.deadline && t.status !== "dropped") noteDeadline(t.deadline.slice(0, 10), t.title || "A task");
      });
      (m.data.commitments || []).forEach(function (c) {
        if (c.deadline && (c.status === "active" || c.status === "paused")) {
          noteDeadline(c.deadline.slice(0, 10), c.title || "A commitment");
        }
      });

      var today = m.today;   // the server's clock, same as the ledger (P11-03)
      var firstDate = days[0].date, lastDate = days[days.length - 1].date;
      // the grid anchors on the Monday on/before the data frontier's first day
      var start = new Date(firstDate + "T00:00:00");
      start.setDate(start.getDate() - ((start.getDay() + 6) % 7));

      // weekday header — the "weeks start Monday" label IS the header order
      var head = el("div", "hz-m-row hz-m-head");
      head.appendChild(el("span", "hz-m-gut", "wk"));
      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach(function (w) {
        head.appendChild(el("span", "hz-m-wd", w));
      });
      wrap.appendChild(head);

      /* INK WEIGHT ON THE SHARED AXIS (P11-06). The month is the spine seen
         from far enough away that an hour is no longer a length, so pressure
         becomes ink instead: a heavier day is a darker cell, a heavier week
         is a longer bar in the gutter that every row shares. The week bars
         are relative to the HEAVIEST week in the grid, which is why the
         totals are counted in a first pass before anything is drawn. Nothing
         placed anywhere means no bars at all rather than five empty ones. */
      var weekMins = [];
      for (var pr = 0; pr < 5; pr++) {
        var wm = 0;
        for (var pc = 0; pc < 7; pc++) {
          var pd = new Date(start);
          pd.setDate(start.getDate() + pr * 7 + pc);
          var piso = pd.getFullYear() + "-" + pad2(pd.getMonth() + 1) + "-" + pad2(pd.getDate());
          (m.byDate[piso] || []).forEach(function (b) { wm += blockMinutes(b); });
        }
        weekMins.push(wm);
      }
      var heaviest = weekMins.reduce(function (a, b) { return Math.max(a, b); }, 0);

      for (var r = 0; r < 5; r++) {
        var row = el("div", "hz-m-row");
        var weekMin = weekMins[r];
        var cells = [];
        for (var c = 0; c < 7; c++) {
          var d = new Date(start);
          d.setDate(start.getDate() + r * 7 + c);
          var iso = d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
          var led = byLedger[iso];
          var list = m.byDate[iso] || [];

          var cell;
          if (!led) {
            // outside the data frontier: before it, a faded past; beyond it,
            // the quiet "open horizon" wash — designed, never broken-looking
            cell = el("div", "hz-m-cell " + (iso < firstDate ? "hz-m-past" : "hz-m-out"));
            cell.appendChild(el("span", "hz-m-num", String(d.getDate())));
            cells.push(cell);
            continue;
          }
          cell = el("button", "hz-m-cell" + (iso === today ? " hz-m-today" : ""));
          if (iso === today) drew.today = true;
          cell.type = "button";
          cell.setAttribute("aria-label", fmtDay(iso) + ", open this day");
          cell.appendChild(el("span", "hz-m-num", String(d.getDate())));

          // density glyph: commitment pressure = (gross − available) / gross,
          // accent-tinted once blocks actually land that day
          if (led.gross > 0) {
            var press = Math.max(0, Math.min(1, (led.gross - led.available) / led.gross));
            // the same pressure as ink on the cell itself (P11-06)
            cell.style.setProperty("--ink", press.toFixed(3));
            var glyph = el("span", "hz-m-glyph" + (list.length ? " hz-m-on" : ""));
            var gfill = el("span", "hz-m-gfill");
            gfill.style.height = Math.round(press * 100) + "%";
            glyph.appendChild(gfill);
            glyph.title = "committed " + hrs(led.gross - led.available) + "h of " + hrs(led.gross) + "h";
            cell.appendChild(glyph);
            drew.press = true;
          }
          // P11-04: what this day actually RECORDED, as a share of what was
          // planned into it — the spine's convention, shrunk to a cell. Solid
          // is the timer, hatched is what you told me. A day with blocks but
          // nothing recorded draws no bar at all: an empty bar would read as
          // "zero measured" when the truth is "not measured".
          var mSum = measuredSummary(m, [iso]);
          if (mSum.planned > 0 && (mSum.timer || mSum.reported)) {
            var tPct = Math.min(100, mSum.timer / mSum.planned * 100);
            var rPct = Math.min(100 - tPct, mSum.reported / mSum.planned * 100);
            var bar = el("span", "hz-m-bar");
            if (tPct > 0) {
              var bf = el("span", "hz-m-bfill hz-fill-timer");
              bf.style.width = tPct + "%";
              bar.appendChild(bf);
              drew.timer = true;
            }
            if (rPct > 0) {
              var rf = el("span", "hz-m-bfill hz-fill-reported");
              rf.style.width = rPct + "%";
              bar.appendChild(rf);
              drew.reported = true;
            }
            var said = [];
            if (mSum.timer) said.push(dur(mSum.timer) + " measured");
            if (mSum.reported) said.push(dur(mSum.reported) + " you reported");
            bar.title = said.join(" · ") + " of " + dur(mSum.planned) + " planned";
            cell.appendChild(bar);
            cell.setAttribute("aria-label",
              fmtDay(iso) + ", " + bar.title + ", open this day");
          }
          // deadline ticks: one warm mark per due task/commitment, named
          var due = deadlines[iso] || [];
          if (due.length) {
            var ticks = el("span", "hz-m-ticks");
            due.slice(0, 4).forEach(function (name) {
              var tick = el("span", "hz-m-tick");
              tick.title = "Due: " + name;
              ticks.appendChild(tick);
            });
            cell.appendChild(ticks);
            drew.due = true;
          }
          // overcommit: findings touch this day, or it's full AND booked
          if (m.findingsByDate[iso] || (led.available === 0 && list.length)) {
            cell.classList.add("hz-m-hot");
            drew.hot = true;
          }
          // one gesture (P11-03): a single tap opens the day, here and on the
          // week alike — the same openDay() both call
          (function (dayIso) {
            cell.addEventListener("click", function () { openDay(dayIso); });
          })(iso);
          cells.push(cell);
        }
        // left gutter: the hours Blink has planned into that week's blocks,
        // plus the shared-axis ink bar that weighs this week against the
        // heaviest one on the grid
        var gut = el("span", "hz-m-gut");
        gut.appendChild(el("span", "hz-m-guth", weekMin ? hrs(weekMin) + "h" : "free"));
        if (heaviest > 0) {
          var gbar = el("span", "hz-m-gbar");
          gbar.style.setProperty("--w", Math.round(weekMin / heaviest * 100) + "%");
          gbar.title = weekMin
            ? hrs(weekMin) + "h placed this week, against " + hrs(heaviest) + "h in your heaviest"
            : "Nothing placed this week";
          gut.appendChild(gbar);
        }
        row.appendChild(gut);
        cells.forEach(function (cc) { row.appendChild(cc); });
        wrap.appendChild(row);
      }
      void lastDate;   // frontier end is implicit — every un-ledgered later cell washes out
      return wrap;
    }

    /* --- quarter (P7-08): one lane per active commitment + the pacing line --- */
    function buildQuarter(m) {
      var days = m.data.ledger_days || [];
      var wrap = el("div", "hz-quarter");

      // the 92-day window the lanes span; an empty ledger still gets a frame
      var startMs = days.length
        ? new Date(days[0].date + "T00:00:00").getTime()
        : new Date(todayISO() + "T00:00:00").getTime();
      var endMs = days.length
        ? new Date(days[days.length - 1].date + "T00:00:00").getTime() + 86400000
        : startMs + 92 * 86400000;
      function pos(t) {
        return Math.max(0, Math.min(100, (t - startMs) / (endMs - startMs) * 100));
      }

      var activeC = (m.data.commitments || []).filter(function (c) { return c.status === "active"; });
      var lanes = activeC.slice(0, 6);
      var measuredMs = measuredMilestoneHours(m);   // P11-04: timer hours only
      var msByCommit = {};
      (m.data.milestones || []).forEach(function (ms) {
        if (!ms.commitment_id) return;
        (msByCommit[ms.commitment_id] = msByCommit[ms.commitment_id] || []).push(ms);
      });

      var scroller = el("div", "hz-q-scroll");
      var lanesEl = el("div", "hz-q-lanes");

      // THE SPINE, ZOOMED OUT (P11-06). Every lane is drawn against the same
      // window, so the quarter gets the week's furniture: one dated ruler over
      // the top and ONE now-column straight down every lane, instead of a
      // today tick repeated per lane with nothing tying them together.
      var todayMs = new Date(m.today + "T00:00:00").getTime();
      if (isNaN(todayMs)) todayMs = Date.now();
      var nowFrac = null;
      if (todayMs >= startMs && todayMs <= endMs) {
        nowFrac = (todayMs - startMs) / (endMs - startMs);
        lanesEl.style.setProperty("--hz-qnow", nowFrac.toFixed(4));
        lanesEl.classList.add("has-now");
        drew.today = true;
      }

      // the ruler: one tick per month start inside the window, named
      function buildRuler() {
        var row = el("div", "hz-q-ruler");
        row.appendChild(el("span", "hz-q-title", ""));
        var bar = el("div", "hz-q-rbar");
        var cur = new Date(startMs);
        cur.setDate(1);
        cur.setHours(0, 0, 0, 0);
        if (cur.getTime() < startMs) cur.setMonth(cur.getMonth() + 1);
        for (var guard = 0; guard < 8 && cur.getTime() <= endMs; guard++) {
          var tick = el("span", "hz-q-rtick",
            cur.toLocaleDateString(undefined, { month: "short" }));
          tick.style.left = pos(cur.getTime()) + "%";
          bar.appendChild(tick);
          cur.setMonth(cur.getMonth() + 1);
        }
        if (nowFrac != null) {
          var lbl = el("span", "hz-q-rnow", "Today");
          lbl.style.left = pos(todayMs) + "%";
          if (nowFrac > 0.86) lbl.style.transform = "translateX(-92%)";
          bar.appendChild(lbl);
        }
        row.appendChild(bar);
        return row;
      }
      lanesEl.appendChild(buildRuler());

      function buildAxis() { return el("div", "hz-q-axis"); }

      if (!lanes.length) {
        // sparse-but-never-empty: one quiet lane holds the frame with intent
        var ghost = el("div", "hz-q-lane");
        ghost.appendChild(el("span", "hz-q-title", "The quarter, open"));
        ghost.appendChild(buildAxis());
        lanesEl.appendChild(ghost);
      }
      lanes.forEach(function (c) {
        var lane = el("div", "hz-q-lane");
        var laneName = commitmentName(c) || "A commitment";
        var title = el("span", "hz-q-title", laneName);
        title.title = laneName;
        lane.appendChild(title);
        var axis = buildAxis();
        // milestone diamonds: placed at target_date, filled by progress
        (msByCommit[c.id] || []).forEach(function (ms) {
          if (!ms.target_date) return;
          var dt = new Date(ms.target_date).getTime();
          if (isNaN(dt) || dt < startMs || dt > endMs) return;
          var dia = el("span", "hz-q-dia");
          dia.style.left = pos(dt) + "%";
          dia.tabIndex = 0;
          milestoneMark(dia, ms, measuredMs[ms.id] || 0, dt);
          axis.appendChild(dia);
          drew.dia = true;
        });
        // the commitment deadline flies a warm flag on the lane
        if (c.deadline) {
          var dl = new Date(c.deadline).getTime();
          if (!isNaN(dl) && dl >= startMs && dl <= endMs) {
            var flag = el("span", "hz-q-flag");
            flag.style.left = pos(dl) + "%";
            flag.title = "Deadline · " + fmtMD(new Date(dl));
            axis.appendChild(flag);
            drew.flag = true;
          }
        }
        lane.appendChild(axis);
        // P11-06: the three measured-time states, per lane. A commitment with
        // no milestones used to show no evidence whatever it had recorded.
        var strip = measuredStrip(commitmentMeasured(m, c.id, startMs, endMs), "hz-ms hz-q-ms");
        if (strip) lane.appendChild(strip);
        lanesEl.appendChild(lane);
      });
      if (activeC.length > lanes.length) {
        lanesEl.appendChild(el("p", "hz-q-more", "+" + (activeC.length - lanes.length) + " more"));
      }
      scroller.appendChild(lanesEl);
      wrap.appendChild(scroller);

      /* THE PACING SENTENCE SITS UNDER THE LANE IT DESCRIBES (P11-06).
         It is computed from ONE weekly-hours figure on the profile and from
         every block in the plan, so it describes the whole plan, not any one
         commitment. With a single lane those are the same thing and it reads
         as that lane's sentence, directly beneath it. With several lanes it
         would silently look like a caption for the last one, so it says whose
         sentence it is. Per-lane pacing is deliberately NOT invented: there
         is no per-commitment weekly target in the data to pace against. */
      var pace = el("div", "hz-q-pacebox");
      if (lanes.length > 1) {
        pace.appendChild(el("p", "hz-q-pacefor",
          "Across all " + lanes.length + " commitments"));
      }
      pace.appendChild(pacingLine(m, days));
      pace.appendChild(pacingEvidence(m));
      wrap.appendChild(pace);
      var whatIf = whatIfControl(m);
      if (whatIf) wrap.appendChild(whatIf);
      return wrap;
    }

    /* --- the pacing sentence: required pace vs the pace you're actually
       keeping, and where that pace lands you. Actual pace = hours in past
       blocks (actuals win) / weeks since the FIRST past block (min 1 week
       so a young plan can't flatter itself). Finish = today + remaining
       milestone hours / actual pace. Each degradation is still a sentence
       with intent — never a blank. --- */
    function pacingLine(m, days) {
      var p = el("p", "hz-q-pace");
      var profile = m.data.profile || {};
      var need = profile.hours_per_week;
      if (need == null) {
        p.textContent = "Tell me your weekly hours and I'll pace you.";
        return p;
      }
      var now = Date.now(), WEEK = 7 * 86400000;
      var pastMin = 0, firstPast = null, futureMin = 0;
      (m.data.blocks || []).forEach(function (b) {
        if (b.status === "cancelled") return;
        var s = new Date(b.starts_at).getTime(), e = new Date(b.ends_at).getTime();
        if (isNaN(s) || isNaN(e)) return;
        if (e <= now) {
          pastMin += (b.actual_minutes != null ? b.actual_minutes : (e - s) / 60000);
          if (firstPast == null || s < firstPast) firstPast = s;
        } else {
          futureMin += (e - s) / 60000;
        }
      });
      var remaining = 0, hasMs = false;
      (m.data.milestones || []).forEach(function (ms) {
        hasMs = true;
        remaining += Math.max(0, (ms.target_hours || 0) - (ms.completed_hours || 0));
      });
      var weeksAhead = Math.max(1, (days.length || 92) / 7);
      var freeH = days.reduce(function (s, d2) { return s + d2.available; }, 0) / 60;

      var text = "You need " + fmtH(need) + "h a week.";
      if (!hasMs) {
        // no milestones yet: planned-vs-free framing still says something true
        text += " " + fmtH(futureMin / 60) + "h on the calendar against " +
          fmtH(freeH) + "h free over the next " + Math.round(weeksAhead) + " weeks.";
      } else if (pastMin > 0 && firstPast != null) {
        var weeks = Math.max(1, (now - firstPast) / WEEK);
        var pace = pastMin / 60 / weeks;
        text += " You're averaging " + pace.toFixed(1) + ".";
        if (remaining > 0 && pace > 0) {
          var finish = new Date(now + remaining / pace * WEEK);
          text += pace >= need
            ? " On pace for " + fmtMD(finish) + "."
            : " That lands " + fmtMD(finish) + ", a touch behind.";
        } else if (remaining === 0) {
          text += " Every milestone hour is already banked.";
        }
      } else {
        // milestones set, nothing logged yet: pace off what's on the calendar
        var planned = futureMin / 60 / weeksAhead;
        text += " Nothing logged yet, and " + fmtH(planned) + "h a week is already on the calendar.";
        if (remaining > 0 && planned > 0) {
          text += " At that pace: " + fmtMD(new Date(now + remaining / planned * WEEK)) + ".";
        }
      }
      p.textContent = text;
      return p;
    }

    /* --- the evidence behind that average (P11-04) ---
       The pacing sentence above averages PAST blocks, and where a block has
       no actual it counts the slot at its planned length. That assumption is
       invisible in a single sentence, so this line puts the three states of
       the same convention into words: what the timer measured, what you
       reported, and how much of that average is still an assumption. It
       computes nothing new, it just names what the sentence above already
       counted. --- */
    function pacingEvidence(m) {
      var p = el("p", "hz-q-evidence");
      var now = Date.now();
      var timer = 0, reported = 0, assumed = 0, assumedBlocks = 0, notYet = 0, notYetBlocks = 0;
      (m.data.blocks || []).forEach(function (b) {
        if (b.status === "cancelled") return;
        var e = new Date(b.ends_at).getTime();
        if (isNaN(e)) return;
        var a = measuredOf(b);
        // the sentence above averages blocks that have already ENDED, so the
        // evidence keeps exactly that scope. Time recorded against a block
        // still in progress is real, and is named separately rather than
        // folded into an average that has not counted it.
        if (e > now) { if (a) { notYet += a.minutes; notYetBlocks += 1; } return; }
        if (!a) { assumed += blockMinutes(b); assumedBlocks += 1; return; }
        if (a.source === "timer") timer += a.minutes;
        else reported += a.minutes;
      });
      var text = "";
      if (!timer && !reported && !assumed) {
        text = "Nothing has finished yet, so the average has nothing to count.";
      } else {
        var said = [];
        if (timer) said.push(dur(timer) + " your timer measured");
        if (reported) said.push(dur(reported) + " you told me about");
        if (said.length) text = "That counts " + said.join(" and ") + ".";
        if (assumed) {
          text += (text ? " " : "") + dur(assumed) + " sits in " + assumedBlocks +
            " past " + (assumedBlocks === 1 ? "block" : "blocks") +
            " with nothing recorded, counted at the length they were planned.";
        }
      }
      if (notYet) {
        text += " " + dur(notYet) + " is already on today's clock and joins the average once " +
          (notYetBlocks === 1 ? "that block ends." : "those blocks end.");
      }
      p.textContent = text;
      return p;
    }

    /* --- the what-if slider (P9-05): drag a hypothetical weekly pace and the
       server's PURE core answers with landing dates (/whatif — arithmetic in
       src/core/pacing.py, no LLM). The real pacing sentence above never
       changes; this renders as a clearly hypothetical second line. Dates
       number-morph as they change (GSAP when present, CSS fallback,
       reduced-motion = instant). Nothing to project = no slider at all. --- */
    function whatIfControl(m) {
      var profile = m.data.profile || {};
      var hasMs = (m.data.milestones || []).length > 0;
      var hasEst = (m.data.tasks || []).some(function (t) {
        return t.estimate_minutes &&
          (t.status === "ready" || t.status === "scheduled" || t.status === "in_progress");
      });
      if (!hasMs && !hasEst) return null;   // honest silence: nothing to project

      var box = el("div", "hz-q-whatif");
      var initial = Math.max(1, Math.min(20, Math.round(profile.hours_per_week || 6)));

      var lbl = el("label", "hz-q-wlabel");
      lbl.appendChild(document.createTextNode("What if I did "));
      var num = el("span", "hz-q-wnum", String(initial));
      lbl.appendChild(num);
      lbl.appendChild(document.createTextNode(" hours a week?"));

      var slider = document.createElement("input");
      slider.type = "range";
      slider.min = "1"; slider.max = "20"; slider.step = "1";
      slider.value = String(initial);
      slider.className = "hz-q-wslider";
      slider.setAttribute("aria-label", "What if I did this many hours a week");

      var line = el("p", "hz-q-whatif-line");
      line.hidden = true;
      var msList = el("div", "hz-q-whatif-ms");

      // number-morph: the changing text slides up into place. GSAP owns the
      // moment when it's loaded; the hz-morph keyframe is the fallback; the
      // reduced-motion path just sets the text.
      function morph(elm, text) {
        if (elm.textContent === text) return;
        elm.textContent = text;
        if (reduce) return;
        if (window.gsap) {
          window.gsap.fromTo(elm, { y: 6, opacity: 0 },
            { y: 0, opacity: 1, duration: 0.28, ease: "power2.out", overwrite: true });
        } else {
          elm.classList.remove("hz-morph");
          void elm.offsetWidth;              // restart the CSS animation
          elm.classList.add("hz-morph");
        }
      }

      var seq = 0, debounceTimer = null;
      function refresh(v) {
        var mySeq = ++seq;
        api("/whatif?hours_per_week=" + encodeURIComponent(v)).then(function (w) {
          if (mySeq !== seq) return;         // a newer drag superseded this one
          line.hidden = false;
          msList.textContent = "";
          if (w.basis === "none" || w.remaining_hours == null) {
            morph(line, "Nothing to project yet.");
            return;
          }
          var h = fmtH(w.hours_per_week);
          if (!w.projected_finish) {
            // the honest never-finishes case: no invented date
            morph(line, "At " + h + "h a week the remaining " +
              fmtH(w.remaining_hours) + "h never lands.");
          } else {
            var t = "At " + h + "h a week you'd land " +
              fmtMD(new Date(w.projected_finish));
            if (w.delta_days != null && Math.abs(w.delta_days) >= 1) {
              var dd = Math.round(Math.abs(w.delta_days));
              t += " (" + dd + (dd === 1 ? " day " : " days ") +
                (w.delta_days > 0 ? "later" : "sooner") + ")";
            }
            morph(line, t + ".");
          }
          (w.milestones || []).forEach(function (ms) {
            if (!(ms.remaining_hours > 0)) return;   // banked milestones are done
            var row = el("span", "hz-q-wms");
            row.appendChild(el("span", "hz-q-wms-t", ms.title || "Milestone"));
            var dateEl = el("span", "hz-q-wms-d");
            morph(dateEl, ms.projected_finish
              ? fmtMD(new Date(ms.projected_finish))
              : "never at this pace");
            row.appendChild(dateEl);
            msList.appendChild(row);
          });
        }).catch(function () {
          if (mySeq !== seq) return;
          line.hidden = false;
          morph(line, "Couldn't run that projection just now.");
        });
      }

      slider.addEventListener("input", function () {
        num.textContent = slider.value;      // the label tracks instantly
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () { refresh(slider.value); }, 150);
      });

      box.appendChild(lbl);
      box.appendChild(slider);
      box.appendChild(line);
      box.appendChild(msList);
      return box;
    }

    /* --- year (P7-08): four quarter arcs, milestone nodes, the goal star.
       No ledger math at all — milestones + profile + commitments only. --- */
    function buildYear(m) {
      var wrap = el("div", "hz-year");
      // the server's clock, the same one the ledger and the blocks were dated
      // by (P11-03) — the year must not open on a different day to the week
      var now = new Date(m.today + "T00:00:00");
      if (isNaN(now)) now = new Date();
      var y = now.getFullYear();
      var startMs = new Date(y, 0, 1).getTime();
      var endMs = new Date(y + 1, 0, 1).getTime();
      function pos(t) {
        return Math.max(0, Math.min(100, (t - startMs) / (endMs - startMs) * 100));
      }

      var band = el("div", "hz-y-band");
      // the segmented track: four quarter arcs, the current one lit
      var track = el("div", "hz-y-track");
      var curQ = Math.floor(now.getMonth() / 3);
      for (var q = 0; q < 4; q++) {
        var seg = el("span", "hz-y-q" + (q === curQ ? " hz-y-qnow" : ""));
        seg.appendChild(el("span", "hz-y-qlbl", "Q" + (q + 1)));
        track.appendChild(seg);
      }
      band.appendChild(track);

      var overlay = el("div", "hz-y-nodes");
      var tMark = el("span", "hz-y-today");
      tMark.style.left = pos(now.getTime()) + "%";
      tMark.title = "Today";
      overlay.appendChild(tMark);
      drew.today = true;

      // milestone nodes: glowing dots along the band, filled by measured hours
      var placed = 0;
      var measuredMs = measuredMilestoneHours(m);
      (m.data.milestones || []).forEach(function (ms) {
        if (!ms.target_date) return;
        var dt = new Date(ms.target_date).getTime();
        if (isNaN(dt) || dt < startMs || dt >= endMs) return;
        placed++;
        var node = el("span", "hz-y-node");
        node.style.left = pos(dt) + "%";
        node.tabIndex = 0;
        // P11-04: the node fills on hours the timer measured, and nothing else
        milestoneMark(node, ms, measuredMs[ms.id] || 0, dt);
        overlay.appendChild(node);
        drew.dia = true;
      });

      // the projected goal star: profile.target_timeline ("6 months") added
      // to the EARLIEST commitment's created_at; unparseable = quietly absent
      var span = parseTimeline(m.data.profile && m.data.profile.target_timeline);
      var origin = null;
      (m.data.commitments || []).forEach(function (c) {
        var ct = new Date(c.created_at).getTime();
        if (!isNaN(ct) && (origin == null || ct < origin)) origin = ct;
      });
      if (span != null && origin != null) {
        var goal = origin + span;
        var gp = pos(goal);   // beyond Dec 31 the star rests at the band's end
        var star = el("span", "hz-y-star", "✦");
        star.style.left = gp + "%";
        var goalDate = new Date(goal);
        var lblText = "~" + goalDate.toLocaleDateString(undefined, { month: "short", year: "numeric" }) + " · projected";
        star.title = lblText;
        var lbl = el("span", "hz-y-starlbl", lblText);
        lbl.style.left = gp + "%";
        if (gp > 82) lbl.style.transform = "translateX(-92%)";
        else if (gp < 12) lbl.style.transform = "none";
        overlay.appendChild(star);
        overlay.appendChild(lbl);
        drew.star = true;
      }
      band.appendChild(overlay);
      wrap.appendChild(band);

      /* THE WHOLE RUN, WITH THE GOAL AT THE END (P11-06).
         The year is the sparsest level, and one commitment with no milestones
         used to leave it as a bare band and a whisper. Every active
         commitment now gets its own run on the SAME axis the band uses: it
         starts where the commitment did, it ends at its deadline, and where
         there is no deadline it runs to the projected goal star and stops
         there rather than pretending to an end date nobody set. Milestone
         nodes sit on the run at their target dates, and the measured strip
         under it carries the same three states as every other level. */
      var runsEl = el("div", "hz-y-runs");
      var activeY = (m.data.commitments || []).filter(function (c) { return c.status === "active"; });
      var shown = activeY.slice(0, 4);
      var goalMs = (span != null && origin != null) ? origin + span : null;
      shown.forEach(function (c) {
        var row = el("div", "hz-y-run");
        var nm = commitmentName(c) || "A commitment";
        var lbl = el("span", "hz-y-rlabel", nm);
        lbl.title = nm;
        row.appendChild(lbl);

        var track2 = el("div", "hz-y-rtrack");
        var cs = new Date(c.created_at).getTime();
        var from = (!isNaN(cs) && cs > startMs) ? cs : startMs;
        var endsAt = null, endWord = "";
        var dl = c.deadline ? new Date(c.deadline).getTime() : NaN;
        if (!isNaN(dl)) { endsAt = dl; endWord = "due " + fmtMD(new Date(dl)); }
        else if (goalMs != null && goalMs > from) { endsAt = goalMs; endWord = "runs to the projected goal"; }
        var to = endsAt == null ? endMs : Math.min(endsAt, endMs);
        var bar = el("span", "hz-y-rbar");
        bar.style.left = pos(from) + "%";
        bar.style.width = Math.max(1.5, pos(to) - pos(from)) + "%";
        bar.title = nm + (endWord ? " · " + endWord : "") +
          (endsAt == null ? " · no end date set yet" : "");
        track2.appendChild(bar);
        // the run's own end cap, only where the data really names an end
        if (endsAt != null && endsAt <= endMs) {
          var cap = el("span", "hz-y-rcap" + (!isNaN(dl) ? " hz-y-rdue" : ""));
          cap.style.left = pos(endsAt) + "%";
          cap.title = endWord;
          track2.appendChild(cap);
          if (!isNaN(dl)) drew.flag = true;
        }
        // this commitment's milestones, on its own run
        (m.data.milestones || []).forEach(function (ms) {
          if (ms.commitment_id !== c.id || !ms.target_date) return;
          var dt2 = new Date(ms.target_date).getTime();
          if (isNaN(dt2) || dt2 < startMs || dt2 >= endMs) return;
          var dot = el("span", "hz-y-rnode");
          dot.style.left = pos(dt2) + "%";
          dot.tabIndex = 0;
          milestoneMark(dot, ms, measuredMs[ms.id] || 0, dt2);
          track2.appendChild(dot);
          drew.dia = true;
        });
        row.appendChild(track2);
        var strip = measuredStrip(commitmentMeasured(m, c.id, startMs, endMs), "hz-ms hz-y-ms");
        if (strip) row.appendChild(strip);
        runsEl.appendChild(row);
      });
      if (shown.length) {
        // the shared now-column, the same furniture the quarter's lanes get
        if (now.getTime() >= startMs && now.getTime() < endMs) {
          runsEl.style.setProperty("--hz-ynow",
            ((now.getTime() - startMs) / (endMs - startMs)).toFixed(4));
          runsEl.classList.add("has-now");
        }
        wrap.appendChild(runsEl);
      }
      if (activeY.length > shown.length) {
        wrap.appendChild(el("p", "hz-y-more", "+" + (activeY.length - shown.length) + " more"));
      }

      if (!placed && !shown.length) {
        wrap.appendChild(el("p", "hz-y-whisper",
          "The whole year, one goal. Everything else stays out of your way."));
      }
      return wrap;
    }

    // "6 months" / "12 weeks" / "1 year" -> a millisecond span; anything the
    // regex can't read returns null and the star simply doesn't rise.
    function parseTimeline(s) {
      if (!s) return null;
      var mt = /(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mos?|weeks?|wks?)/i.exec(s);
      if (!mt) return null;
      var n = parseFloat(mt[1]), u = mt[2].toLowerCase();
      var daysPer = /^y/.test(u) ? 365.25 : /^m/.test(u) ? 30.44 : 7;
      return n * daysPer * 86400000;
    }

    /* =================================================================
       THE SPINE (P11-03) — one clock, drawn the same way at both levels.

       A RUN is one day laid out left to right: the window's first hour at
       0%, its last at 100%, everything in between a straight percentage of
       the minutes. spineGeom() computes that window ONCE per render from
       every fetched day, so the seven runs stacked in the week are
       pixel-aligned and free time reads as a gap straight down the column,
       and so the day level is literally the same axis, expanded.

       Three marks, identical at both levels:
         .hz-open  open time            .hz-span  a placed session
         .hz-busy  time already spoken for
       plus the now-marker. #hz-legend names each one in words.
       ================================================================= */

    // what the current render actually drew — the legend lists these and
    // nothing else, so it can never promise a mark that is not on screen
    var drew = {};
    var lastGeom = null;      // the replan diff places its ghosts on this axis

    /* =================================================================
       MEASURED TIME (P11-04) — ONE convention, every level.

         outline only   the plan. Nothing has been recorded against it.
         solid fill     minutes the TIMER measured.
         hatched fill   minutes you told me about yourself.

       The outline is always the planned span; the fill inside it is the
       share of that plan something was actually recorded for. A block with
       no actual gets NO fill element at all — not a zero-width one, not a
       faint one — because a drawn fill is a claim that time was measured,
       and we never make a claim the data doesn't carry. Every figure below
       is summed from the blocks the canvas is drawing, never from a
       default and never from a server estimate of "probably".
       ================================================================= */

    // What a block can honestly claim, or null. A stored actual with no
    // source is treated as SELF-REPORTED: that is the weaker claim, and
    // guessing "timer" would invent a measurement.
    function measuredOf(b) {
      if (!b || b.actual_minutes == null) return null;
      var mins = Number(b.actual_minutes);
      if (!isFinite(mins) || mins <= 0) return null;
      return { minutes: mins, source: b.actual_source === "timer" ? "timer" : "reported" };
    }

    // the fill inside one span. Returns nothing when there is nothing to say.
    function paintSpan(span, b) {
      var planned = blockMinutes(b);
      span.dataset.plannedMinutes = String(Math.round(planned));
      var a = measuredOf(b);
      if (!a) { drew.planned = true; return null; }
      if (!(planned > 0)) return null;   // no span to be a share OF
      var fill = el("span", "hz-fill hz-fill-" + a.source);
      fill.style.width = Math.max(2, Math.min(100, a.minutes / planned * 100)) + "%";
      fill.setAttribute("aria-hidden", "true");
      span.insertBefore(fill, span.firstChild);
      drew[a.source] = true;
      return a;
    }

    // hours read badly under an hour ("0.1h tracked"), so short stretches are
    // said in minutes, the way a person would say them
    function dur(min) {
      var m = Math.round(min);
      return m < 60 ? m + " min" : hrs(m) + "h";
    }

    // the same three states, in words, for the screen reader and the popover
    function measuredPhrase(a) {
      if (!a) return "nothing recorded yet";
      return dur(a.minutes) + " " + (a.source === "timer" ? "measured" : "you reported");
    }

    // THE LIVE BLOCK: while a focus session is actually running, its span
    // fills as the clock ticks. The minutes come from FocusNow, which is the
    // timer itself, so this is measurement in progress and not a projection.
    // No session, or a session on a block this level isn't drawing, paints
    // nothing at all.
    function liveSession() {
      try {
        return (window.FocusNow && window.FocusNow.live) ? window.FocusNow.live() : null;
      } catch (_) { return null; }
    }
    function paintLive(root) {
      if (!root) return;
      Array.prototype.forEach.call(root.querySelectorAll(".hz-span.is-live"), function (s) {
        s.classList.remove("is-live");
      });
      var live = liveSession();
      if (!live || !live.block_id || !(live.minutes > 0)) return;
      var span = root.querySelector('.hz-span[data-block-id="' + String(live.block_id).replace(/"/g, '\\"') + '"]');
      if (!span) return;
      var planned = Number(span.dataset.plannedMinutes || 0);
      if (!(planned > 0)) return;
      var fill = span.querySelector(".hz-fill");
      if (!fill) {
        fill = el("span", "hz-fill hz-fill-timer");
        fill.setAttribute("aria-hidden", "true");
        span.insertBefore(fill, span.firstChild);
      }
      fill.className = "hz-fill hz-fill-timer";
      fill.style.width = Math.max(2, Math.min(100, live.minutes / planned * 100)) + "%";
      span.classList.add("is-live");
    }

    // What the canvas can honestly total for a set of dates: planned minutes
    // from the placed spans themselves, measured minutes from the timer,
    // reported minutes from what you said. Blocks with nothing recorded are
    // counted in `planned` and nowhere else.
    function measuredSummary(m, dates) {
      var sum = { planned: 0, timer: 0, reported: 0, blocks: 0, recorded: 0 };
      dates.forEach(function (date) {
        (m.byDate[date] || []).forEach(function (b) {
          sum.planned += blockMinutes(b);
          sum.blocks += 1;
          var a = measuredOf(b);
          if (!a) return;
          sum.recorded += 1;
          if (a.source === "timer") sum.timer += a.minutes;
          else sum.reported += a.minutes;
        });
      });
      return sum;
    }

    // the tracked line, in the same words as the legend. Nothing placed at
    // all = no line; placed but nothing run yet = an invitation, not a zero.
    function trackedLine(sum, whenWord) {
      if (!sum.blocks || !(sum.planned > 0)) return null;
      var p = el("p", "hz-tracked");
      if (!sum.timer && !sum.reported) {
        p.appendChild(el("span", "hz-tracked-lead",
          dur(sum.planned) + " planned " + whenWord + "."));
        p.appendChild(el("span", "hz-tracked-rest",
          " Start a session and the time you actually put in shows up right here."));
        return p;
      }
      var lead = dur(sum.timer) + " tracked of " + dur(sum.planned) + " planned " + whenWord;
      p.appendChild(el("span", "hz-tracked-lead", lead + (sum.reported ? "," : ".")));
      if (sum.reported) {
        p.appendChild(el("span", "hz-tracked-rest",
          " plus " + dur(sum.reported) + " you told me about."));
      }
      return p;
    }

    // How much of the day is really still OPEN: the free windows the ledger
    // published, minus the sessions already sitting inside them. The ledger's
    // own `available` counts capacity BEFORE placement, so quoting it beside
    // a full run would promise room that the picture plainly contradicts —
    // the no-phantom-supply rule. This is the number the run actually draws.
    // A day with no published windows keeps the ledger figure rather than
    // inventing one.
    function openMinutes(day, m) {
      var wins = (day.free_windows || []).map(function (w) {
        return [minOf(w.start), minOf(w.end)];
      }).filter(function (iv) { return iv[0] != null && iv[1] != null && iv[1] > iv[0]; });
      if (!wins.length) return day.available || 0;
      var blocks = (m.byDate[day.date] || []).map(function (b) {
        return [minOf(b.starts_at), minOf(b.ends_at)];
      }).filter(function (iv) { return iv[0] != null && iv[1] != null && iv[1] > iv[0]; });
      var total = 0;
      wins.forEach(function (w) {
        var cuts = [];
        blocks.forEach(function (b) {
          var s = Math.max(w[0], b[0]), e = Math.min(w[1], b[1]);
          if (e > s) cuts.push([s, e]);
        });
        cuts.sort(function (a, b) { return a[0] - b[0]; });
        var taken = 0, edge = w[0];
        cuts.forEach(function (iv) {
          if (iv[1] <= edge) return;
          taken += iv[1] - Math.max(edge, iv[0]);
          edge = iv[1];
        });
        total += (w[1] - w[0]) - taken;
      });
      return Math.max(0, Math.round(total));
    }

    function spineGeom(m) {
      var lo = null, hi = null;
      function note(s, e) {
        if (s == null || e == null || e <= s) return;
        lo = (lo == null) ? s : Math.min(lo, s);
        hi = (hi == null) ? e : Math.max(hi, e);
      }
      (m.data.ledger_days || []).forEach(function (d2) {
        (d2.free_windows || []).forEach(function (w) { note(minOf(w.start), minOf(w.end)); });
      });
      Object.keys(m.byDate).forEach(function (k) {
        m.byDate[k].forEach(function (b) { note(minOf(b.starts_at), minOf(b.ends_at)); });
      });
      var start, end;
      if (lo == null) { start = 7 * 60; end = 22 * 60; }   // a plan-less day still gets a real waking window
      else {
        start = Math.max(0, Math.floor(lo / 60) * 60);
        end = Math.min(24 * 60, Math.ceil(hi / 60) * 60);
      }
      if (end - start < 360) {           // never squeeze the axis below six hours
        end = Math.min(24 * 60, start + 360);
        start = Math.max(0, end - 360);
      }
      var span = end - start;
      return {
        start: start, end: end, span: span,
        pct: function (min) { return Math.max(0, Math.min(100, (min - start) / span * 100)); },
        frac: function (min) { return Math.max(0, Math.min(1, (min - start) / span)); },
      };
    }

    // "9 AM" in the viewer's own conventions, from a minute-of-day
    function fmtHour(min) {
      var d = new Date(2000, 0, 1, Math.floor(min / 60) % 24, min % 60);
      return d.toLocaleTimeString(undefined, { hour: "numeric" });
    }

    // the axis header: hour labels over the runs column. `gutterWord` fills
    // the label column at week level and is omitted at day level, where the
    // run owns the full width.
    function axisHeader(geom, gutterWord) {
      var axis = el("div", "hz-axis");
      if (gutterWord != null) axis.appendChild(el("span", "hz-axis-gut", gutterWord));
      var ruler = el("div", "hz-ruler");
      // labels thin out as the axis gets shorter, so they never collide
      var narrow = (window.innerWidth || 1024) < 560;
      var step = geom.span <= 480 ? 60 : (geom.span <= 900 ? 120 : 180);
      if (narrow) step = Math.max(step, 180);
      var first = Math.ceil(geom.start / step) * step;
      for (var t = first; t <= geom.end; t += step) {
        var tick = el("span", "hz-tick", fmtHour(t));
        tick.style.left = geom.pct(t) + "%";
        ruler.appendChild(tick);
      }
      axis.appendChild(ruler);
      return axis;
    }

    // one placed session as a span of the run. `big` (day level) makes it a
    // real button with its title and time showing; the week's thin spans are
    // decorative and the row button carries the accessible name.
    function blockSpan(b, m, big) {
      var t = m.tasks[b.task_id] || {};
      var span = big ? el("button", "hz-span") : el("div", "hz-span");
      if (big) span.type = "button";
      if (b.id) span.dataset.blockId = b.id;   // identity for the replan diff (P9-01)
      span.style.setProperty("--span-dot", commitmentColor(t.commitment_id));
      var title = t.title || "Blink block";
      var when = fmtTime(b.starts_at) + "–" + fmtTime(b.ends_at);
      span.appendChild(el("span", "hz-span-title", title));
      span.appendChild(el("span", "hz-span-time", when));
      // P11-04: the fill inside the outline — measured, reported, or nothing
      var a = paintSpan(span, b);
      if (big) span.setAttribute("aria-label", title + ", " + when + ", " + measuredPhrase(a));
      else span.setAttribute("aria-hidden", "true");
      return span;
    }

    // place one absolutely-positioned band on a run, in the shared geometry
    function placeOn(run, geom, s, e, cls) {
      if (s == null || e == null || e <= s) return false;
      var band = el("div", cls);
      band.style.left = geom.pct(s) + "%";
      band.style.width = Math.max(0.3, geom.pct(e) - geom.pct(s)) + "%";
      run.appendChild(band);
      return true;
    }

    // ONE run: the shared drawing, used by week and day alike.
    function buildRun(day, m, geom, big) {
      var run = el("div", "hz-run" + (big ? " hz-run-big" : ""));
      var wins = day.free_windows || [];
      var blocks = m.byDate[day.date] || [];
      // time already spoken for: the window minus free time minus placed work
      busyGaps(wins, blocks, geom.start, geom.end).forEach(function (g) {
        if (placeOn(run, geom, g[0], g[1], "hz-busy")) drew.busy = true;
      });
      wins.forEach(function (w) {
        if (placeOn(run, geom, minOf(w.start), minOf(w.end), "hz-open")) drew.open = true;
      });
      blocks.forEach(function (b) {
        var s = minOf(b.starts_at), e = minOf(b.ends_at);
        if (s == null || e == null) return;
        var span = blockSpan(b, m, big);
        span.style.left = geom.pct(s) + "%";
        span.style.width = Math.max(0.4, geom.pct(e) - geom.pct(s)) + "%";
        run.appendChild(span);
        drew.placed = true;
      });
      return run;
    }

    // ONE GESTURE (P11-03): a single tap opens a day, from the week and from
    // the month alike. Nothing needs a double-click any more.
    function openDay(date) {
      state.anchorDate = date;
      persistState();
      if (state.level === "day") renderInto(active, false, false);
      else setLevel("day");
    }

    /* --- week: seven runs on the one axis --- */
    function buildWeek(m) {
      var days = m.data.ledger_days || [];
      var wrap = el("div", "hz-week");
      if (!days.length) {
        wrap.appendChild(emptyCard("Your week is ready whenever you are.",
          "Tell Blink what you're working toward and the seven days will fill in right here."));
        return wrap;
      }
      var geom = spineGeom(m);
      lastGeom = geom;
      wrap.appendChild(axisHeader(geom, ""));

      var rows = el("div", "hz-rows");
      days.forEach(function (day, i) {
        var isToday = day.date === m.today;
        var list = m.byDate[day.date] || [];
        // .h-card + --i keep the CSS open-stagger (~45ms apart) from P7-05
        var card = el("button", "h-card hz-daycard" + (isToday ? " hz-today" : ""));
        card.type = "button";
        card.style.setProperty("--i", i);
        card.dataset.date = day.date;   // lets the replan diff find the day (P9-01)

        var head = el("div", "hz-dc-head");
        head.appendChild(el("span", "hz-dc-date", isToday ? "Today" : fmtDay(day.date)));
        head.appendChild(el("span", "hz-dc-free", hrs(openMinutes(day, m)) + "h open"));
        var fCount = m.findingsByDate[day.date];
        if (fCount) head.appendChild(el("span", "hz-badge", fCount + " to review"));
        card.appendChild(head);

        var run = buildRun(day, m, geom, false);
        run.classList.add("hz-dc-body");   // the replan diff drops ghosts here (P9-01)
        card.appendChild(run);
        card.setAttribute("aria-label", weekRowLabel(day, list, m, isToday));
        card.addEventListener("click", function () { openDay(day.date); });
        rows.appendChild(card);
      });

      // the now-marker rides the shared axis: ONE line straight down every
      // run, drawn only when the clock really sits inside the window.
      var hasToday = days.some(function (d2) { return d2.date === m.today; });
      if (hasToday && m.nowMin >= geom.start && m.nowMin <= geom.end) {
        var col = el("div", "hz-nowcol");
        col.style.setProperty("--hz-now", geom.frac(m.nowMin).toFixed(4));
        rows.appendChild(col);
        drew.now = true;
      }
      wrap.appendChild(rows);
      // the tracked line (P11-04): the same three states the runs draw, said
      // out loud and totalled from the very blocks above it
      var tl = trackedLine(measuredSummary(m, days.map(function (d2) { return d2.date; })),
        "over these seven days");
      if (tl) wrap.appendChild(tl);
      return wrap;
    }

    // The row's accessible name: everything the thin run says visually, in
    // words, because the spans are too narrow to carry their own labels.
    function weekRowLabel(day, list, m, isToday) {
      var when = (isToday ? "Today, " : "") + fmtDay(day.date);
      if (!list.length) {
        return when + ": nothing placed, " + hrs(openMinutes(day, m)) + " hours open. Open this day.";
      }
      var names = list.slice(0, 3).map(function (b) {
        var t = m.tasks[b.task_id] || {};
        return (t.title || "a session") + " at " + fmtTime(b.starts_at);
      });
      // P11-04: the runs are too thin to label, so the row says which of the
      // three states its spans are in — and only when something was recorded
      var sum = measuredSummary(m, [day.date]);
      var rec = [];
      if (sum.timer) rec.push(dur(sum.timer) + " measured");
      if (sum.reported) rec.push(dur(sum.reported) + " you reported");
      return when + ": " + list.length + (list.length === 1 ? " session, " : " sessions, ") +
        names.join(", ") + (list.length > 3 ? ", and more" : "") +
        (rec.length ? ", " + rec.join(" and ") : "") + ". Open this day.";
    }

    /* --- day: the SAME run, expanded --- */
    function buildDay(m) {
      var days = m.data.ledger_days || [];
      var dates = days.map(function (d2) { return d2.date; });
      // clamp the anchor into the fetched range. "Today" here is the SERVER's
      // today (P11-03), the same clock that dated the blocks and the ledger,
      // so the day the week draws work on is the day this opens on.
      if (dates.indexOf(state.anchorDate) === -1) {
        state.anchorDate = dates.indexOf(m.today) !== -1 ? m.today : (dates[0] || m.today);
        persistState();
      }
      var idx = dates.indexOf(state.anchorDate);
      var day = days[idx] || { date: state.anchorDate, available: 0, free_windows: [] };
      var blocks = m.byDate[day.date] || [];
      var isToday = day.date === m.today;
      var geom = spineGeom(m);

      var wrap = el("div", "hz-dayview");

      // canvas header: ◀ date ▶ steps days inside the fetched range
      var head = el("div", "hz-day-head");
      head.appendChild(navBtn("◀", "Previous day", idx > 0, function () { stepDay(-1, dates); }));
      head.appendChild(el("span", "hz-day-title", isToday ? "Today · " + fmtDay(day.date) : fmtDay(day.date)));
      head.appendChild(navBtn("▶", "Next day", idx >= 0 && idx < dates.length - 1, function () { stepDay(1, dates); }));
      wrap.appendChild(head);

      // the hero: the session you are in, or the one coming next. Drawn only
      // when there really is one, so it never announces work you don't have.
      var hero = heroBlock(blocks, m, isToday);
      if (hero) {
        var t = m.tasks[hero.block.task_id] || {};
        var card = el("div", "hz-hero");
        card.appendChild(el("span", "hz-hero-kicker", hero.kicker));
        card.appendChild(el("span", "hz-hero-title", t.title || "Blink block"));
        card.appendChild(el("span", "hz-hero-time",
          fmtTime(hero.block.starts_at) + "–" + fmtTime(hero.block.ends_at)));
        wrap.appendChild(card);
      }

      // the run, expanded: the same axis, wide enough for hour labels and
      // block titles. It scrolls sideways on a narrow screen and opens
      // scrolled to the now-marker.
      var scroll = el("div", "hz-day-scroll");
      var track = el("div", "hz-day-track");
      track.appendChild(axisHeader(geom, null));
      var run = buildRun(day, m, geom, true);
      track.appendChild(run);

      // the block popover, positioned against the track
      Array.prototype.forEach.call(run.querySelectorAll("button.hz-span"), function (span) {
        var id = span.dataset.blockId;
        var b = null;
        blocks.forEach(function (x) { if (x.id === id) b = x; });
        if (!b) return;
        span.addEventListener("click", function (ev) {
          ev.stopPropagation();
          showPopover(span, b, m, track, run);
        });
      });

      // the now-marker, only when this IS today and the clock sits inside
      if (isToday && m.nowMin >= geom.start && m.nowMin <= geom.end) {
        var nowEl = el("div", "hz-nowline");
        nowEl.style.left = geom.pct(m.nowMin) + "%";
        nowEl.setAttribute("aria-hidden", "true");
        run.appendChild(nowEl);
        drew.now = true;
        // nothing to scroll to: the run always fits the width it is given, so
        // the marker is on screen wherever the clock is (P11-03)
      }

      // an empty day says so warmly, over its own open water
      if (!blocks.length) {
        run.appendChild(el("p", "hz-whisper", isToday
          ? "Today is all yours, " + hrs(openMinutes(day, m)) + "h of it."
          : "Nothing placed here yet, and " + hrs(openMinutes(day, m)) + "h of room."));
      }
      scroll.appendChild(track);
      wrap.appendChild(scroll);
      // the same tracked line as the week, for this one day (P11-04)
      var dayTl = trackedLine(measuredSummary(m, [day.date]), isToday ? "today" : "here");
      if (dayTl) wrap.appendChild(dayTl);

      // and offers the nearest day that isn't empty, if there is one
      if (!blocks.length) {
        var near = nearestBusyDate(dates, day.date, m);
        if (near) {
          var n = (m.byDate[near] || []).length;
          var jump = el("button", "hz-nearest",
            "Your next sessions are " + fmtDay(near) + ". Take a look?");
          jump.type = "button";
          jump.setAttribute("aria-label",
            "Open " + fmtDay(near) + ", where " + n + (n === 1 ? " session is" : " sessions are") + " placed");
          jump.addEventListener("click", function () { openDay(near); });
          wrap.appendChild(jump);
        }
      }
      return wrap;
    }

    // The current block, else the next one. Past-only days get their first
    // block back as "earlier today" rather than a claim about right now.
    function heroBlock(blocks, m, isToday) {
      if (!blocks.length) return null;
      if (!isToday) return { kicker: "First up", block: blocks[0] };
      var now = m.nowMin, cur = null, next = null;
      blocks.forEach(function (b) {
        var s = minOf(b.starts_at), e = minOf(b.ends_at);
        if (s == null || e == null) return;
        if (s <= now && now < e && !cur) cur = b;
        else if (s > now && !next) next = b;
      });
      if (cur) return { kicker: "Right now", block: cur };
      if (next) return { kicker: "Next up", block: next };
      return { kicker: "Earlier today", block: blocks[blocks.length - 1] };
    }

    // the closest day in the fetched range that has work on it — forward
    // first, because a plan points ahead
    function nearestBusyDate(dates, from, m) {
      var i = dates.indexOf(from);
      if (i === -1) return null;
      for (var step = 1; step < dates.length; step++) {
        var fwd = dates[i + step], back = dates[i - step];
        if (fwd && (m.byDate[fwd] || []).length) return fwd;
        if (back && (m.byDate[back] || []).length) return back;
      }
      return null;
    }

    function navBtn(sym, label, enabled, onClick) {
      var b = el("button", "hz-nav", sym);
      b.type = "button";
      b.setAttribute("aria-label", label);
      b.disabled = !enabled;
      b.addEventListener("click", onClick);
      return b;
    }
    function stepDay(dir, dates) {
      var i = dates.indexOf(state.anchorDate) + dir;
      if (i < 0 || i >= dates.length) return;
      state.anchorDate = dates[i];
      persistState();
      renderInto(active, false, false);   // same level, cached data — instant
    }

    // The gaps the day grid hatches: the bounds minus the union of free
    // windows and blocks (slivers under 5 minutes are ignored).
    function busyGaps(wins, blocks, startMin, endMin) {
      var covered = [];
      function push(s, e) {
        if (s == null || e == null) return;
        s = Math.max(startMin, s); e = Math.min(endMin, e);
        if (e > s) covered.push([s, e]);
      }
      wins.forEach(function (w) { push(minOf(w.start), minOf(w.end)); });
      blocks.forEach(function (b) { push(minOf(b.starts_at), minOf(b.ends_at)); });
      covered.sort(function (a, b) { return a[0] - b[0]; });
      var gaps = [], cur = startMin;
      covered.forEach(function (iv) {
        if (iv[0] > cur) gaps.push([cur, iv[0]]);
        cur = Math.max(cur, iv[1]);
      });
      if (cur < endMin) gaps.push([cur, endMin]);
      return gaps.filter(function (g) { return g[1] - g[0] >= 5; });
    }

    /* ---------- the block popover (day level) ---------- */
    var popEl = null;
    function onDocClick(e) {
      if (popEl && !popEl.contains(e.target)) dismissPopover();
    }
    function dismissPopover() {
      if (!popEl) return false;
      if (popEl.parentNode) popEl.parentNode.removeChild(popEl);
      popEl = null;
      document.removeEventListener("click", onDocClick, true);
      return true;
    }
    function showPopover(chip, b, m, host, run) {
      dismissPopover();
      var t = m.tasks[b.task_id] || {};
      var c = t.commitment_id ? m.commitments[t.commitment_id] : null;
      var pop = el("div", "hz-pop");
      pop.setAttribute("role", "dialog");
      pop.setAttribute("aria-label", t.title || "Block details");
      pop.appendChild(el("p", "hz-pop-title", t.title || "Blink block"));
      var cName = commitmentName(c);
      if (cName) {
        var row = el("p", "hz-pop-row");
        var dot = el("span", "hz-dot");
        dot.style.background = commitmentColor(t.commitment_id);
        row.appendChild(dot);
        row.appendChild(document.createTextNode(cName));
        pop.appendChild(row);
      }
      pop.appendChild(el("p", "hz-pop-row", fmtTime(b.starts_at) + "–" + fmtTime(b.ends_at)));
      // P11-04: the popover says which of the three states this block is in,
      // in the legend's own words. "So far" used to be silent about whether
      // anyone had measured anything; now it names the source or says plainly
      // that nothing has been recorded.
      var mine = measuredOf(b);
      var planLine = dur(t.estimate_minutes != null ? t.estimate_minutes : blockMinutes(b)) + " planned";
      pop.appendChild(el("p", "hz-pop-row hz-pop-faint", planLine + " · " + measuredPhrase(mine)));
      var st = b.status || t.status;
      if (st) pop.appendChild(el("p", "hz-pop-status", st));
      // Focus sessions (P9-07): a planned block offers "Start now" — the one
      // extra affordance; the chip click itself stays a details popover.
      if (b.status === "planned" && window.FocusNow) {
        var startBtn = el("button", "hz-pop-start", "Start now");
        startBtn.type = "button";
        startBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          dismissPopover();
          var span = (new Date(b.ends_at) - new Date(b.starts_at)) / 60000;
          window.FocusNow.start({
            id: b.id,
            task_id: b.task_id,
            title: t.title || "This session",
            planned_minutes: isNaN(span) ? 0 : Math.max(0, Math.round(span)),
            estimate_minutes: t.estimate_minutes != null ? t.estimate_minutes : null,
            commitment_id: t.commitment_id || null,
            // prior measured stints ride along so the meta line stays honest
            accumulated_minutes:
              (b.actual_source === "timer" && b.actual_minutes) ? b.actual_minutes : 0,
          });
        });
        pop.appendChild(startBtn);
      }
      // P11-03: the spans run horizontally now, so the popover hangs BELOW the
      // run at the span's own x. It never sits on top of the run it describes,
      // and it is clamped to the canvas on both axes — the old vertical clamp
      // measured the grid, which is shorter than the popover, so every popover
      // flipped up and covered the morning.
      var hostEl = host || chip.parentNode;
      var runEl = run || chip.parentNode;
      // the span's x within the host: the run is the span's offset parent
      var x = runEl.offsetLeft + chip.offsetLeft;
      pop.style.left = "0px";
      pop.style.top = (runEl.offsetTop + runEl.offsetHeight + 10) + "px";
      hostEl.appendChild(pop);
      pop.style.left = Math.max(0, Math.min(x, hostEl.clientWidth - pop.offsetWidth)) + "px";
      // the horizon scrolls vertically, so bring the whole card into view
      // rather than folding it back over the spine
      if (pop.scrollIntoView) {
        try { pop.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch (_) {}
      }
      popEl = pop;
      // click-away arms on the next tick so the opening click doesn't count
      setTimeout(function () {
        if (popEl === pop) document.addEventListener("click", onDocClick, true);
      }, 0);
    }

    // P11-08: a typed date/zone reference in a reply opens the plan exactly
    // where it points. Nothing new is computed here — this is openDay/setLevel,
    // the same two moves a tap on the canvas already makes.
    function goTo(level, date) {
      if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) state.anchorDate = date;
      if (level && LEVELS.indexOf(level) !== -1 && level !== state.level) {
        setLevel(level);
        return;
      }
      persistState();
      render();
    }

    // P11-04: while a session is genuinely running, its span keeps growing
    // between renders. This reads FocusNow's own clock — no session means no
    // work and no paint, so nothing can creep forward on its own.
    setInterval(function () {
      if (!liveSession()) return;
      paintLive(active);
    }, 20000);

    return { render: render, refresh: refresh, dismissPopover: dismissPopover, stepLevel: stepLevel, stageDiff: stageDiff, goTo: goTo };
  }

  /* =====================================================================
     FocusSettings — the in-session settings store + persistence.

     A tiny observable store, persisted to localStorage under "focus.settings".
     Shape: { voiceEnabled: boolean, autoSend: boolean,
              remindersEnabled: boolean, face: "capsule"|"lumen"|"folio",
              horizonLevel?: object }.

     PUBLIC CONTRACT (window.FocusSettings), stable for later work items:
       FocusSettings.get(key)          -> current value for key
       FocusSettings.set(key, value)   -> update + persist + notify subscribers
       FocusSettings.all()             -> shallow copy of the whole object
       FocusSettings.onChange(fn)      -> subscribe; fn(key, value, all) fires on
                                          every set; returns an unsubscribe fn.

     e.g. P5-04 (agent voice) will read `FocusSettings.get("voiceEnabled")`
     before speaking a reply, and may `FocusSettings.onChange(...)` to react
     when the user flips the switch mid-session. P5-05 (calendar) will add its
     own keys through the same store.

     Blink is Nocturne-only (P10-00): there is no theme setting. <html> ships
     with a permanent `class="dark"` in index.html (Tailwind's darkMode:'class'
     and the `dark:` utilities in markup key off it) and nothing may remove it.
     Old persisted payloads that still carry a `theme` key are tolerated: the
     key is ignored and dropped on the next persist.
     ===================================================================== */
  function createSettingsStore() {
    var KEY = "focus.settings";
    var defaults = {
      voiceEnabled: true,   // Blink speaks its replies by default (user, 2026-08-30);
                            // the Settings toggle still turns it off, and that
                            // saved choice is honoured over this default on load.
      autoSend: true,   // P8-01c: release the mic -> the transcript sends itself
      remindersEnabled: false,   // P9-03d: local block reminders, opt-in only
      // P12-02: deeper reasoning on the steps that DECIDE (goal classification,
      // plan synthesis, reading a photographed timetable). Off by default, and
      // it never changes what is true, only how carefully Blink judges it.
      deepThinking: false,
      face: "capsule",  // P10-01/02: the active face ("capsule" | "lumen" | "folio")
    };
    var FACES = ["capsule", "lumen", "folio"];   // junk/absent values fall back
    var state = load();
    var subs = [];

    function load() {
      var out = { voiceEnabled: defaults.voiceEnabled, autoSend: defaults.autoSend, remindersEnabled: defaults.remindersEnabled, deepThinking: defaults.deepThinking, face: defaults.face };
      try {
        var raw = localStorage.getItem(KEY);
        if (raw) {
          var saved = JSON.parse(raw);
          // P10-00: a stale `theme` key from an old payload is ignored here —
          // the app is Nocturne-only and <html class="dark"> is permanent.
          if (saved && typeof saved.voiceEnabled === "boolean") out.voiceEnabled = saved.voiceEnabled;
          if (saved && typeof saved.autoSend === "boolean") out.autoSend = saved.autoSend;
          if (saved && typeof saved.remindersEnabled === "boolean") out.remindersEnabled = saved.remindersEnabled;
          // P12-02: only a real boolean counts. Anything else (an old payload
          // with no key at all, or junk) leaves deep thinking off.
          if (saved && typeof saved.deepThinking === "boolean") out.deepThinking = saved.deepThinking;
          // P10-01: the face identity — only known names count, junk is ignored
          if (saved && FACES.indexOf(saved.face) !== -1) out.face = saved.face;
          // P7-06: the horizon's {level, anchorDate} rides along so a
          // reopened app lands on the zoom level you left it at
          if (saved && saved.horizonLevel && typeof saved.horizonLevel === "object" &&
              typeof saved.horizonLevel.level === "string") {
            out.horizonLevel = saved.horizonLevel;
          }
        }
      } catch (_) { /* corrupt/unavailable storage — fall back to defaults */ }
      return out;
    }
    function persist() {
      try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (_) {}
    }
    function notify(key) {
      var snapshot = all();
      subs.forEach(function (fn) { try { fn(key, state[key], snapshot); } catch (_) {} });
    }
    function all() {
      return { voiceEnabled: state.voiceEnabled, autoSend: state.autoSend, remindersEnabled: state.remindersEnabled, deepThinking: state.deepThinking, face: state.face, horizonLevel: state.horizonLevel };
    }

    function get(key) { return state[key]; }
    function set(key, value) {
      if (state[key] === value) return;
      state[key] = value;
      persist();
      notify(key);
    }
    function onChange(fn) {
      subs.push(fn);
      return function () { subs = subs.filter(function (f) { return f !== fn; }); };
    }

    return { get: get, set: set, all: all, onChange: onChange };
  }

  /* =====================================================================
     Settings — the gear button (top-right) + the modal it opens.
     Sections: Face (segmented Capsule | Lumen | Folio, P10-01/02 — applied as
     data-face on <html>; index.html re-applies it before first paint),
     Agent voice (on/off switch, read later by P5-04), Auto-send
     voice, Reminders, Google Calendar (placeholder entry point, wired in
     P5-05). No Theme section: Blink is Nocturne-only (P10-00). Backdrop dim, click-outside / Esc / X to close, focus-visible
     rings. Reads + writes through the FocusSettings store, so every control
     reflects and persists the live setting.
     Maps to <SettingsModal open={...} onClose={...} /> in React.
     ===================================================================== */
  function createSettings(settings) {
    var btn = document.getElementById("settings-btn");
    var overlay = null;
    var lastFocused = null;

    function buildModal() {
      overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true" aria-label="Settings">' +
          '<div class="modal-head">' +
            '<h2 class="modal-title">Settings</h2>' +
            '<button class="modal-x" id="settings-close" aria-label="Close settings" type="button">' +
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>' +
            '</button>' +
          '</div>' +

          // --- Face (P10-01) ---
          '<div class="setting-row col">' +
            '<div class="setting-meta">' +
              '<p class="setting-label">Face</p>' +
              '<p class="setting-caption">How Blink looks. Same agent, same moods, a different presence.</p>' +
            '</div>' +
            '<div class="segmented" id="face-seg" role="radiogroup" aria-label="Face">' +
              '<button class="seg" type="button" role="radio" data-face-opt="capsule">Capsule</button>' +
              '<button class="seg" type="button" role="radio" data-face-opt="lumen">Lumen</button>' +
              '<button class="seg" type="button" role="radio" data-face-opt="folio">Folio</button>' +
            '</div>' +
          '</div>' +

          // --- Agent voice ---
          '<div class="setting-row">' +
            '<div class="setting-meta">' +
              '<p class="setting-label">Agent voice</p>' +
              '<p class="setting-caption">Speak responses aloud, in Blink\'s voice.</p>' +
            '</div>' +
            '<button class="switch" id="voice-switch" role="switch" type="button" aria-label="Speak responses aloud">' +
              '<span class="switch-knob"></span>' +
            '</button>' +
          '</div>' +

          // --- Auto-send voice (P8-01c) ---
          '<div class="setting-row">' +
            '<div class="setting-meta">' +
              '<p class="setting-label">Auto-send voice</p>' +
              '<p class="setting-caption">Send as soon as you release the mic. Off = review first.</p>' +
            '</div>' +
            '<button class="switch" id="autosend-switch" role="switch" type="button" aria-label="Send as soon as you release the mic">' +
              '<span class="switch-knob"></span>' +
            '</button>' +
          '</div>' +

          // --- Reminders (P9-03d) ---
          '<div class="setting-row col">' +
            '<div class="setting-meta-row">' +
              '<div class="setting-meta">' +
                '<p class="setting-label">Reminders</p>' +
                '<p class="setting-caption">A quiet nudge 10 minutes before a session, while a tab is open.</p>' +
              '</div>' +
              '<button class="switch" id="reminders-switch" role="switch" type="button" aria-label="Notify before upcoming sessions">' +
                '<span class="switch-knob"></span>' +
              '</button>' +
            '</div>' +
            '<div class="cal-row">' +
              '<button class="btn ghost" id="rem-gcal" type="button">Add today\'s reminders to Google Calendar</button>' +
              '<span class="cal-note" id="rem-note" aria-live="polite"></span>' +
            '</div>' +
          '</div>' +

          // --- Google account + Calendar (P14: one consent covers both) ---
          '<div class="setting-row col">' +
            '<div class="setting-meta">' +
              '<p class="setting-label">Google account</p>' +
              '<p class="setting-caption" id="acct-caption">One sign-in brings your name and your calendar along, and keeps your plans on this account.</p>' +
            '</div>' +
            '<div class="cal-row">' +
              '<button class="btn ghost" id="acct-btn" type="button">Continue with Google</button>' +
              '<span class="cal-note" id="acct-note" aria-live="polite"></span>' +
            '</div>' +
            '<div class="cal-row">' +
              '<button class="btn ghost" id="cal-connect" type="button">Connect Google Calendar</button>' +
              '<span class="cal-note" id="cal-note" aria-live="polite"></span>' +
            '</div>' +
          '</div>' +

          // --- Deep thinking (P12-02) ---
          // The caption states the tradeoff plainly. It promises slower and
          // more careful, and nothing about being more correct, because the
          // profile changes judgment quality and never what is true.
          '<div class="setting-row">' +
            '<div class="setting-meta">' +
              '<p class="setting-label">Deep thinking</p>' +
              '<p class="setting-caption">Blink takes a little longer and reasons more carefully before answering.</p>' +
            '</div>' +
            '<button class="switch" id="deep-switch" role="switch" type="button" aria-label="Reason more carefully before answering">' +
              '<span class="switch-knob"></span>' +
            '</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(overlay);

      // Wire controls -----------------------------------------------------
      // Face picker (P10-01/02): a three-option segmented radio group. The
      // store subscriber in main() applies the value as data-face on <html>.
      var segs = overlay.querySelectorAll("#face-seg .seg");
      function paintFace() {
        var cur = settings.get("face");
        if (cur !== "lumen" && cur !== "folio") cur = "capsule";
        segs.forEach(function (b) {
          var on = b.getAttribute("data-face-opt") === cur;
          b.classList.toggle("on", on);
          b.setAttribute("aria-checked", on ? "true" : "false");
        });
      }
      segs.forEach(function (b) {
        b.addEventListener("click", function () {
          settings.set("face", b.getAttribute("data-face-opt"));
          paintFace();
        });
      });
      paintFace();

      var vsw = overlay.querySelector("#voice-switch");
      function paintVoice() {
        var on = !!settings.get("voiceEnabled");
        vsw.classList.toggle("on", on);
        vsw.setAttribute("aria-checked", on ? "true" : "false");
      }
      vsw.addEventListener("click", function () {
        settings.set("voiceEnabled", !settings.get("voiceEnabled"));
        paintVoice();
      });
      paintVoice();

      var asw = overlay.querySelector("#autosend-switch");
      function paintAutoSend() {
        var on = !!settings.get("autoSend");
        asw.classList.toggle("on", on);
        asw.setAttribute("aria-checked", on ? "true" : "false");
      }
      asw.addEventListener("click", function () {
        settings.set("autoSend", !settings.get("autoSend"));
        paintAutoSend();
      });
      paintAutoSend();

      /* --- Reminders (P9-03d) -------------------------------------------
         The switch asks for Notification permission on enable; the actual
         scheduling lives in createReminders (main()), which subscribes to
         this setting. The Google Calendar button is a two-click confirm
         gate: first click states exactly what would be written, second
         click sends confirm:true to the confirm-gated /calendar/events
         route. Nothing touches the real calendar without that second yes. */
      var rsw = overlay.querySelector("#reminders-switch");
      var remNote = overlay.querySelector("#rem-note");
      function remSay(t) {
        remNote.textContent = t || "";
        remNote.classList.toggle("show", !!t);
      }
      function paintReminders() {
        var on = !!settings.get("remindersEnabled");
        rsw.classList.toggle("on", on);
        rsw.setAttribute("aria-checked", on ? "true" : "false");
      }
      rsw.addEventListener("click", function () {
        if (settings.get("remindersEnabled")) {
          settings.set("remindersEnabled", false);
          remSay("");
          paintReminders();
          return;
        }
        if (!("Notification" in window)) {
          remSay("This browser can't show notifications.");
          return;
        }
        Notification.requestPermission().then(function (perm) {
          if (perm === "granted") {
            settings.set("remindersEnabled", true);
            remSay("");
          } else {
            remSay("Notifications are blocked in the browser.");
          }
          paintReminders();
        }).catch(function () { remSay("Notifications are blocked in the browser."); });
      });
      paintReminders();

      /* Deep thinking (P12-02). Plain switch, same pattern as the others: no
         permission to ask for, nothing to arm. The value rides out on the next
         request as `mode`, so flipping it mid-session takes effect on the very
         next turn and nothing already in flight changes meaning. */
      var dsw = overlay.querySelector("#deep-switch");
      function paintDeep() {
        var on = !!settings.get("deepThinking");
        dsw.classList.toggle("on", on);
        dsw.setAttribute("aria-checked", on ? "true" : "false");
      }
      if (dsw) {
        dsw.addEventListener("click", function () {
          settings.set("deepThinking", !settings.get("deepThinking"));
          paintDeep();
        });
        paintDeep();
      }

      var remBtn = overlay.querySelector("#rem-gcal");
      var remPending = null;    // today's upcoming blocks awaiting the confirm click
      function fmtNaive(d) {    // naive local ISO, matching the app's wall-clock frame
        function p2(n) { return (n < 10 ? "0" : "") + n; }
        return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()) +
               "T" + p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":00";
      }
      remBtn.addEventListener("click", function () {
        if (remPending) {
          // Second click = the explicit yes: write the reminder events.
          var list = remPending;
          remPending = null;
          remBtn.textContent = "Add today's reminders to Google Calendar";
          remSay("Adding…");
          var okCount = 0;
          var chain = Promise.resolve();
          list.forEach(function (item) {
            chain = chain.then(function () {
              return api("/calendar/events", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  action: "create", confirm: true,
                  summary: "Blink reminder: " + item.title,
                  start: fmtNaive(new Date(item.start.getTime() - 10 * 60000)),
                  end: fmtNaive(item.start),
                }),
              }).then(function () { okCount++; }).catch(function () { /* counted below */ });
            });
          });
          chain.then(function () {
            remSay(okCount === list.length
              ? "Added " + okCount + " reminder" + (okCount === 1 ? "" : "s")
              : "Added " + okCount + " of " + list.length + " reminders");
          });
          return;
        }
        // First click: gather today's upcoming blocks and ASK before writing.
        api("/calendar/status").then(function (status) {
          if (!(status && status.connected && status.calendar_granted)) {
            remSay("Connect Google Calendar below first.");
            return;
          }
          return api("/details").then(function (d) {
            var now = new Date();
            var todayKey = fmtNaive(now).slice(0, 10);
            var upcoming = (d.blocks || []).filter(function (b) {
              if (b.status !== "planned") return false;
              var s = new Date(b.starts_at);
              return !isNaN(s) && (b.starts_at || "").slice(0, 10) === todayKey && s > now;
            }).map(function (b) {
              var t = (d.tasks || []).filter(function (x) { return x.id === b.task_id; })[0];
              return { title: (t && t.title) || "Focus session", start: new Date(b.starts_at) };
            });
            if (!upcoming.length) {
              remSay("Nothing upcoming today to remind about.");
              return;
            }
            remPending = upcoming;
            remBtn.textContent = "Yes, create " + upcoming.length + " event" +
              (upcoming.length === 1 ? "" : "s");
            remSay("This writes " + upcoming.length + " reminder event" +
              (upcoming.length === 1 ? "" : "s") + " to your real calendar.");
          });
        }).catch(function () { remSay("Couldn't check the calendar just now."); });
      });

      var calNote = overlay.querySelector("#cal-note");
      var calBtn = overlay.querySelector("#cal-connect");

      /* --- Google account (P14): sign-in IS the signup. -----------------
         Signed out: "Continue with Google" starts the one-consent flow
         (identity + calendar together). Signed in: the stored name + email
         show, and the button becomes Sign out. Sign-out clears the session
         cookie server-side, then this browser mints a fresh guest
         workspace on reload. */
      var acctBtn = overlay.querySelector("#acct-btn");
      var acctNote = overlay.querySelector("#acct-note");
      var acctCaption = overlay.querySelector("#acct-caption");
      var acctSignedIn = false;
      function paintAccount(s) {
        acctSignedIn = !!(s && s.signed_in);
        if (acctSignedIn) {
          var who = s.name || "";
          if (s.email) who += (who ? " · " : "") + s.email;
          acctCaption.textContent = "Signed in. Your plans, your name, and your calendar stay together on this account.";
          acctNote.textContent = who || "Signed in";
          acctNote.classList.add("show");
          acctBtn.textContent = "Sign out";
        } else {
          acctCaption.textContent = "One sign-in brings your name and your calendar along, and keeps your plans on this account.";
          acctBtn.textContent = "Continue with Google";
          acctNote.textContent = "";
          acctNote.classList.remove("show");
        }
      }
      function refreshAccount() {
        fetch("/v1/session").then(function (r) { return r.json(); })
          .then(paintAccount).catch(function () {});
      }
      acctBtn.addEventListener("click", function () {
        if (acctSignedIn) {
          fetch("/v1/session/signout", { method: "POST" }).then(function () {
            try { localStorage.removeItem("focus.workspace"); } catch (_) {}
            window.location.replace(window.location.pathname);
          }).catch(function () {
            acctNote.textContent = "Couldn't sign out just now. Try again in a moment.";
            acctNote.classList.add("show");
          });
          return;
        }
        api("/auth/signin").then(function (r) {
          if (r && r.auth_url) window.location.href = r.auth_url;
        }).catch(function () {
          acctNote.textContent = "Sign-in isn't available right now. Guest mode keeps working.";
          acctNote.classList.add("show");
        });
      });
      refreshAccount();

      // Start the Google consent flow (full-page redirect to Google).
      // P14: prefer the sign-in flow, since the SAME consent grants calendar
      // and brings identity along; fall back to the calendar-only connect
      // when sign-in is disabled on the server.
      function startConnect() {
        api("/auth/signin").then(function (r) {
          if (r && r.auth_url) { window.location.href = r.auth_url; return; }
          throw new Error("no auth url");
        }).catch(function () {
          api("/calendar/connect").then(function (r) {
            if (r && r.auth_url) window.location.href = r.auth_url;
          }).catch(function () {
            calNote.textContent = "Connect unavailable";
            calNote.classList.add("show");
          });
        });
      }

      function paintCalendar(status) {
        var connected = status && status.connected;
        var granted = status && status.calendar_granted;
        if (connected && granted) {
          calBtn.textContent = "Sync Google Calendar";
          calNote.textContent = status.email ? "Connected as " + status.email : "Connected";
          calNote.classList.add("show");
        } else if (connected && !granted) {
          // Signed in, but the user unchecked the Calendar box on Google's screen.
          calBtn.textContent = "Reconnect (keep Calendar checked)";
          calNote.textContent = "Signed in, but Calendar permission wasn't granted.";
          calNote.classList.add("show");
        } else {
          calBtn.textContent = "Connect Google Calendar";
          calNote.classList.remove("show");
        }
      }
      function refreshCalendar() {
        api("/calendar/status").then(paintCalendar).catch(function () {});
      }

      // If we just came back from Google without the Calendar scope, say so.
      if (window.__focusCalendarFlash === "missing_scope") {
        calNote.textContent = "Signed in, but Calendar permission wasn't granted. Reconnect and keep the Calendar box checked.";
        calNote.classList.add("show");
        window.__focusCalendarFlash = null;
      }

      calBtn.addEventListener("click", function () {
        api("/calendar/status").then(function (status) {
          var connected = status && status.connected;
          var granted = status && status.calendar_granted;
          if (connected && granted) {
            // Fully connected: pull the latest events into capacity.
            calNote.textContent = "Syncing…";
            calNote.classList.add("show");
            api("/calendar/sync-google", { method: "POST" }).then(function (r) {
              calNote.textContent = "Synced " + (r.events_count || 0) + " events";
              if (window.FocusRefresh) window.FocusRefresh();
              // a successful sync earns the celebrate bounce (P9-00a)
              if (window.__emote) { try { window.__emote("celebrate", 1400); } catch (_) {} }
            }).catch(function () { calNote.textContent = "Sync failed"; });
          } else {
            // Not connected, or connected without Calendar permission: (re)consent.
            startConnect();
          }
        }).catch(function () {});
      });

      refreshCalendar();

      // Close affordances -------------------------------------------------
      overlay.querySelector("#settings-close").addEventListener("click", close);
      overlay.addEventListener("mousedown", function (e) {
        if (e.target === overlay) close();     // click-outside (on backdrop)
      });
      overlay.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { e.stopPropagation(); close(); return; }
        // Focus trap (P7-09): Tab cycles inside the dialog while it's open.
        if (e.key !== "Tab") return;
        var items = Array.prototype.filter.call(
          overlay.querySelectorAll("button, [href], input, select, textarea"),
          function (n) { return !n.disabled && n.offsetParent !== null; });
        if (!items.length) return;
        var first = items[0], last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      });
    }

    function open() {
      if (!overlay) buildModal();
      lastFocused = document.activeElement;
      overlay.classList.add("show");
      // focus the close button so Esc/Tab work and the ring lands somewhere sane
      var c = overlay.querySelector("#settings-close");
      setTimeout(function () { if (c) c.focus(); }, 30);
    }
    function close() {
      if (!overlay) return;
      overlay.classList.remove("show");
      if (lastFocused && lastFocused.focus) lastFocused.focus();
    }

    btn.addEventListener("click", open);
    return { open: open, close: close };
  }

  /* =====================================================================
     Account affordance (P14b) — sign-in made discoverable, never a wall.

     Guest-first stays: a first-time visitor still gets full value with no
     account, and `migrate_guest_workspace` folds that guest workspace into
     the account on the first sign-in. Two quiet entry points, no gate:

       1. The chip beside the gear. Signed out it reads "Sign in"; signed in
          it reads the stored first name and just opens Settings, where the
          real account row (sign out, calendar) already lives.
       2. One offer, once, after the FIRST plan that actually placed blocks —
          the moment there is something worth keeping and cross-device
          suddenly means something. Dismiss is remembered forever, exactly
          like the insight consent: a no is a no.

     Both reuse the existing /auth/signin flow; there is no second OAuth path.
     ===================================================================== */
  function createAccount(settingsUi) {
    var OFFER_KEY = "focus.signin.offerDone";
    var chip = document.getElementById("acct-chip");
    var signedIn = false;
    var settled = false;
    var offer = null;
    var pendingOffer = false;

    function done() {
      try { return localStorage.getItem(OFFER_KEY) === "1"; } catch (_) { return true; }
    }
    function markDone() {
      try { localStorage.setItem(OFFER_KEY, "1"); } catch (_) {}
    }

    function startSignin(onFail) {
      api("/auth/signin").then(function (r) {
        if (r && r.auth_url) { window.location.href = r.auth_url; return; }
        throw new Error("no auth url");
      }).catch(function () {
        if (onFail) onFail();
      });
    }

    function paintChip(s) {
      signedIn = !!(s && s.signed_in);
      settled = true;
      chip.hidden = false;
      if (signedIn) {
        var name = (s.name || "").trim().split(/\s+/)[0] || "Account";
        chip.innerHTML = '<span class="acct-dot"></span>';
        chip.appendChild(document.createTextNode(name));
        chip.setAttribute("aria-label", "Account settings for " + name);
        chip.title = (s.email || name) + " · signed in";
        hideOffer();
      } else {
        chip.textContent = "Sign in";
        chip.setAttribute("aria-label", "Sign in with Google");
        chip.title = "Sign in with Google to keep your plan on every device";
      }
      if (!signedIn && pendingOffer) { pendingOffer = false; showOffer(); }
    }

    function refresh() {
      fetch("/v1/session").then(function (r) { return r.json(); })
        .then(paintChip).catch(function () { paintChip(null); });
    }

    chip.addEventListener("click", function () {
      if (signedIn) { settingsUi.open(); return; }
      markDone();                       // acting on it retires the offer too
      startSignin(function () {
        chip.textContent = "Sign in unavailable";
        setTimeout(function () { if (!signedIn) chip.textContent = "Sign in"; }, 2600);
      });
    });

    function buildOffer() {
      offer = document.createElement("div");
      offer.className = "signin-offer";
      offer.setAttribute("role", "region");
      offer.setAttribute("aria-label", "Sign in with Google");
      offer.innerHTML =
        '<p>Sign in with Google to keep this plan on your account, so your ' +
        'phone sees the same week and Blink can reach you there. The same ' +
        'sign-in carries the calendar permission, so nothing extra to grant.</p>' +
        '<div class="signin-row">' +
          '<button class="btn ghost" id="offer-signin" type="button">Continue with Google</button>' +
          '<button class="btn ghost" id="offer-later" type="button">Not now</button>' +
          '<span class="signin-note" id="offer-note" aria-live="polite"></span>' +
        '</div>';
      document.body.appendChild(offer);
      offer.querySelector("#offer-later").addEventListener("click", function () {
        markDone();                     // "not now" means never again
        hideOffer();
      });
      offer.querySelector("#offer-signin").addEventListener("click", function () {
        markDone();
        startSignin(function () {
          offer.querySelector("#offer-note").textContent =
            "Sign-in isn't available right now. Your plan is safe as a guest.";
        });
      });
    }

    function hideOffer() {
      if (offer) offer.classList.remove("show");
    }

    function showOffer() {
      if (signedIn || done()) return;
      if (!offer) buildOffer();
      // Never steal focus: the card fades in where it stands and the user
      // can keep typing straight through it.
      offer.classList.add("show");
    }

    /* Called when a plan lands with blocks > 0. Waits for the reply to have
       finished before it appears, and stays silent for a signed-in user, a
       user who already dismissed it, or a session that hasn't settled yet. */
    function offerAfterPlan() {
      if (done()) return;
      if (!settled) { pendingOffer = true; return; }
      if (signedIn) return;
      showOffer();
    }

    refresh();
    return { refresh: refresh, offerAfterPlan: offerAfterPlan, hideOffer: hideOffer };
  }

  /* =====================================================================
     Reminders (P9-03d) — local notifications ~10 minutes before today's
     upcoming planned blocks, while a tab is open. Opt-in via the Settings
     toggle (which owns the Notification permission ask). setTimeout against
     /details data, re-armed on refresh; a hard client-side cap of 5 fires a
     day mirrors the server's absolute notification budget, and a per-day
     fired-id ledger keeps a re-arm from double-firing the same block.
     Maps to a useReminders() effect in React.
     ===================================================================== */
  function createReminders(settings) {
    var KEY = "focus.reminders";
    var MAX_PER_DAY = 5;              // mirror of the notification-budget invariant
    var LEAD_MS = 10 * 60000;         // ~10 minutes before the block
    var timers = [];

    function todayKey() {
      var d = new Date();
      function p2(n) { return (n < 10 ? "0" : "") + n; }
      return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate());
    }
    function ledger() {
      try {
        var raw = JSON.parse(localStorage.getItem(KEY) || "null");
        if (raw && raw.date === todayKey() && typeof raw.fired === "number") {
          return { date: raw.date, fired: raw.fired, ids: raw.ids || [] };
        }
      } catch (_) { /* corrupt storage — start fresh */ }
      return { date: todayKey(), fired: 0, ids: [] };
    }
    function saveLedger(l) {
      try { localStorage.setItem(KEY, JSON.stringify(l)); } catch (_) {}
    }

    function clearAll() {
      timers.forEach(clearTimeout);
      timers = [];
    }

    function canNotify() {
      return !!settings.get("remindersEnabled") &&
             ("Notification" in window) && Notification.permission === "granted";
    }

    function fire(blockId, title, timeText) {
      if (!canNotify()) return;
      var l = ledger();
      if (l.fired >= MAX_PER_DAY) return;              // budget is absolute
      if (l.ids.indexOf(blockId) !== -1) return;       // already reminded
      try {
        new Notification("Blink", {
          body: title + " starts at " + timeText + ".",
          tag: "focus-block-" + blockId,               // browsers coalesce dupes too
        });
      } catch (_) { return; }
      l.fired++; l.ids.push(blockId);
      saveLedger(l);
    }

    function arm() {
      clearAll();
      if (!canNotify()) return;
      api("/details").then(function (d) {
        if (!canNotify()) return;                      // toggled off mid-fetch
        var now = new Date();
        var tk = todayKey();
        var l = ledger();
        var scheduled = 0;
        (d.blocks || []).forEach(function (b) {
          if (b.status !== "planned") return;
          if ((b.starts_at || "").slice(0, 10) !== tk) return;
          var start = new Date(b.starts_at);
          if (isNaN(start) || start <= now) return;    // already started
          if (l.ids.indexOf(b.id) !== -1) return;      // fired earlier today
          if (scheduled + l.fired >= MAX_PER_DAY) return;
          var t = (d.tasks || []).filter(function (x) { return x.id === b.task_id; })[0];
          var title = (t && t.title) || "Focus session";
          var timeText = start.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
          var delay = Math.max(0, start.getTime() - LEAD_MS - now.getTime());
          timers.push(setTimeout(function () { fire(b.id, title, timeText); }, delay));
          scheduled++;
        });
      }).catch(function () { /* quiet — reminders are a nicety, never load-bearing */ });
    }

    settings.onChange(function (key) {
      if (key !== "remindersEnabled") return;
      if (settings.get("remindersEnabled")) arm(); else clearAll();
    });

    arm();   // armed on load when the toggle was already on
    return { arm: arm };
  }

  /* =====================================================================
     Sleep — after a stretch of no interaction the presence rests: eyes
     lower and dim. Any interaction, or the agent leaving idle, wakes it.
     Maps to a useIdleTimer effect in React.
     ===================================================================== */
  function createSleep(appEl, agent, eyes) {
    var TIMEOUT = 35000;                 // ~35s of stillness -> sleep
    var timer = null;
    // Never doze off while the horizon is open (P7-05): reading the plan is
    // stillness, but the parked eyes should stay lit above it.
    function sleep() {
      if (agent.get() === "idle" && !appEl.classList.contains("viewing")) {
        // drowsy beat (P9-00a): lids get heavy for a moment, then sleep lands
        eyes.emote("sleepy", 900);
        setTimeout(function () {
          if (agent.get() === "idle" && !appEl.classList.contains("viewing")) {
            appEl.classList.add("sleeping");
          }
        }, 850);
      }
    }
    function reset() {
      clearTimeout(timer);
      appEl.classList.remove("sleeping");
      if (!reduce) timer = setTimeout(sleep, TIMEOUT);
    }
    function wake() {
      if (appEl.classList.contains("sleeping")) {
        appEl.classList.remove("sleeping");
        // warm wake: a surprised open-up + a double blink as the eyes come
        // back — the "oh, you're here" beat (P9-00a)
        appEl.classList.add("waking");
        eyes.emote("surprised", 650);
        setTimeout(function () { eyes.blink(true); }, 700);
        setTimeout(function () { appEl.classList.remove("waking"); }, 720);
      }
      reset();
    }
    function pause() { clearTimeout(timer); appEl.classList.remove("sleeping"); }
    // Moving the mouse counts as presence: Focus wakes and starts tracking you.
    ["pointerdown", "pointermove", "mousemove", "keydown", "touchstart"].forEach(function (ev) {
      document.addEventListener(ev, wake, { passive: true });
    });
    return { reset: reset, pause: pause, wake: wake };
  }

  /* =====================================================================
     Voice (P7-01) — prepares Focus's reply as decoded audio via the backend
     /tts endpoint, gated by the FocusSettings "voiceEnabled" toggle.

     prepare(text) -> Promise resolving {audio, duration} once metadata is
     loaded, or null when voice is off, TTS is unavailable, decode fails, or
     the request went stale (a stop() happened in between). The caller races
     this against a timeout and, if audio wins, hands it to
     surface.speakSynced() and adopt()s it so stop() can cut it off.

     stop(): pauses + drops the current audio and bumps the token so any
     in-flight prepare resolves null (never plays late).

     P12-03b: the fast path is now /tts/stream, which hands back raw PCM the
     moment Chirp 3 HD produces it (measured ~0.4s to first byte against ~0.9s
     for the whole MP3). createPcmStream below turns that byte stream into an
     object that looks exactly like an <audio> element to speakSynced, so the
     word-by-word reveal keeps driving off currentTime/duration untouched. Any
     browser or server that can't do it falls through to the original
     whole-file /tts path, which is unchanged.

     Maps to a useVoice() hook / <Voice /> effect in React.
     ===================================================================== */
  /* --- createPcmStream (P12-03b) --------------------------------------
     An <audio>-shaped wrapper around a growing stream of headerless LINEAR16
     PCM chunks. It exposes exactly the surface speakSyncedNow uses (play,
     pause, currentTime, duration, ended, addEventListener("ended")), so the
     caption reveal and the eye amplitude keep reading the same two numbers
     they always have.

     THE DURATION PROBLEM: a stream has no header, so the total length of the
     reply is unknown until the last chunk lands. The reveal divides
     currentTime by duration, and a duration that is too SHORT would reveal
     words the voice has not spoken yet, which is the one thing that must
     never happen. So while the stream is still arriving, duration reports a
     deliberate OVER-estimate: the longer of a slow-speech guess from the
     text length and the audio already in hand plus a margin. Words then lag
     the voice slightly and catch up. Once the last chunk lands (which in
     practice is about half a second into a five second reply) duration
     becomes the exact measured length and the sync is precise from there.
     Reveal position only ever moves forward, so the correction is a catch-up,
     never a rewind.

     Returns null when the browser has no Web Audio, which sends the caller
     back to the whole-file path. */
  function createPcmStream(sampleRate, text) {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    var ctx;
    try { ctx = new AC(); } catch (_) { return null; }

    var pending = [];        // AudioBuffers waiting for play()
    var sources = [];        // scheduled nodes, so pause() can cut them
    var startedAt = null;    // ctx time the first buffer was scheduled for
    var nextAt = 0;          // ctx time the next buffer goes at
    var bufferedSec = 0;     // audio received so far, in seconds
    var carry = null;        // odd trailing byte of a chunk (samples are 2 bytes)
    var complete = false;
    var stopped = false;
    var endedFired = false;
    var endTimer = null;
    var endedFns = [];
    var onPause = null;      // set by the feeder, so pause() can cut the socket

    // Charon speaks near 16 characters a second. Guessing 11 over-estimates on
    // purpose, per the duration note above. The floor covers very short replies.
    var estimate = Math.max(1.5, (text || "").length / 11);

    function scheduleEnd() {
      if (!complete || startedAt === null || endTimer || endedFired || stopped) return;
      var ms = Math.max(0, (nextAt - ctx.currentTime) * 1000) + 60;
      endTimer = setTimeout(function () {
        endTimer = null;
        if (endedFired || stopped) return;
        endedFired = true;
        endedFns.forEach(function (fn) { try { fn(); } catch (_) {} });
      }, ms);
    }

    function pump() {
      if (startedAt === null || stopped) return;
      while (pending.length) {
        var buf = pending.shift();
        var src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        // Underrun (chunks arriving slower than realtime): start from now
        // rather than in the past, which would drop the head of the buffer.
        if (nextAt < ctx.currentTime) nextAt = ctx.currentTime;
        try { src.start(nextAt); } catch (_) { return; }
        nextAt += buf.duration;
        sources.push(src);
      }
      scheduleEnd();
    }

    function push(bytes) {
      if (stopped || !bytes || !bytes.length) return;
      var u8 = bytes;
      if (carry) {                                  // re-join a split sample
        var joined = new Uint8Array(carry.length + u8.length);
        joined.set(carry, 0);
        joined.set(u8, carry.length);
        u8 = joined;
        carry = null;
      }
      var n = u8.length >> 1;
      if (u8.length & 1) carry = u8.subarray(u8.length - 1);
      if (!n) return;
      var buf;
      try { buf = ctx.createBuffer(1, n, sampleRate); } catch (_) { return; }
      var ch = buf.getChannelData(0);
      var dv = new DataView(u8.buffer, u8.byteOffset, n * 2);
      for (var i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
      bufferedSec += buf.duration;
      pending.push(buf);
      pump();
    }

    function markComplete() {
      complete = true;
      scheduleEnd();
    }

    function play() {
      if (stopped) return Promise.reject(new Error("stopped"));
      if (startedAt !== null) return Promise.resolve();
      function begin() {
        if (stopped) return;
        startedAt = ctx.currentTime + 0.05;   // small lead so nothing starts late
        nextAt = startedAt;
        pump();
      }
      var p = null;
      // Suspended until a gesture on some browsers. The user just submitted or
      // held the mic, so this resolves; if it doesn't, the caller types instead.
      try { p = ctx.resume ? ctx.resume() : null; } catch (_) { p = null; }
      if (p && p.then) return p.then(begin);
      begin();
      return Promise.resolve();
    }

    function pause() {
      if (stopped) return;
      stopped = true;
      if (endTimer) { clearTimeout(endTimer); endTimer = null; }
      sources.forEach(function (s) { try { s.stop(); } catch (_) {} });
      sources = [];
      pending = [];
      // Whoever is feeding this player gets to shut the socket too, so an
      // interrupted or unused reply stops downloading instead of draining.
      if (onPause) { var f = onPause; onPause = null; try { f(); } catch (_) {} }
      try { if (ctx.close) ctx.close(); } catch (_) {}
    }

    var api = {
      play: play,
      pause: pause,
      push: push,
      markComplete: markComplete,
      onPause: function (fn) { onPause = fn; },
      hasAudio: function () { return bufferedSec > 0; },
      addEventListener: function (name, fn) { if (name === "ended" && fn) endedFns.push(fn); },
      removeEventListener: function () {},
    };
    Object.defineProperty(api, "currentTime", {
      get: function () {
        if (startedAt === null) return 0;
        var t = ctx.currentTime - startedAt;
        if (!(t > 0)) return 0;
        var max = nextAt - startedAt;
        return t > max ? max : t;
      },
    });
    Object.defineProperty(api, "duration", {
      get: function () {
        if (complete) return bufferedSec;
        return Math.max(estimate, bufferedSec * 1.2);
      },
    });
    Object.defineProperty(api, "ended", {
      get: function () {
        if (endedFired) return true;
        return complete && startedAt !== null && ctx.currentTime >= nextAt;
      },
    });
    return api;
  }

  function createVoice() {
    var currentAudio = null;   // so a new reply / interrupt cuts off the previous one
    var token = 0;             // bumped by stop(); stale prepares resolve null

    function stop() {
      token++;
      if (currentAudio) {
        try { currentAudio.pause(); } catch (_) {}
        currentAudio = null;
      }
    }

    // Register the audio the controller decided to play, so stop() owns it.
    function adopt(audio) { currentAudio = audio; }

    function voiceOn() {
      return !!(window.FocusSettings && window.FocusSettings.get("voiceEnabled"));
    }

    /* Fast path (P12-03b): stream PCM and resolve on the FIRST chunk, so audio
       starts while the rest is still being synthesized. Resolves null on any
       reason to fall back (old browser, 503 from the server, an error before a
       single byte landed), and the caller then takes the whole-file path. An
       error AFTER audio has started just ends the stream early: the reveal
       completes and the full text still lands, so the reply is never lost. */
    function prepareStreamed(text, tok) {
      if (!window.fetch || !window.ReadableStream) return Promise.resolve(null);
      if (!(window.AudioContext || window.webkitAudioContext)) return Promise.resolve(null);
      return fetch("/v1/workspaces/" + WS + "/tts/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      }).then(function (r) {
        if (!r.ok || !r.body || !r.body.getReader) {
          if (r.body && r.body.cancel) { try { r.body.cancel(); } catch (_) {} }
          return null;
        }
        if (tok !== token || !voiceOn()) { try { r.body.cancel(); } catch (_) {} return null; }
        var rate = parseInt(r.headers.get("X-Sample-Rate"), 10) || 24000;
        var player = createPcmStream(rate, text);
        if (!player) { try { r.body.cancel(); } catch (_) {} return null; }
        var reader = r.body.getReader();
        player.onPause(function () { try { reader.cancel(); } catch (_) {} });
        return new Promise(function (resolve) {
          var settled = false;
          function settle(v) { if (!settled) { settled = true; resolve(v); } }
          function abandon() {
            player.pause();
            try { reader.cancel(); } catch (_) {}
            settle(null);
          }
          function loop() {
            reader.read().then(function (res) {
              if (tok !== token || !voiceOn()) { abandon(); return; }
              if (res.done) {
                player.markComplete();
                settle(player.hasAudio() ? { audio: player, duration: player.duration } : null);
                if (!player.hasAudio()) player.pause();
                return;
              }
              player.push(res.value);
              // First real audio: hand it over now, keep filling in behind.
              if (player.hasAudio()) settle({ audio: player, duration: player.duration });
              loop();
            }).catch(function () {
              if (settled) { player.markComplete(); return; }
              abandon();
            });
          }
          loop();
        });
      }).catch(function (e) {
        console.debug("[voice] /tts/stream unavailable, falling back", e);
        return null;
      });
    }

    function prepare(text) {
      var tok = ++token;
      if (!text) return Promise.resolve(null);
      // Gate on the live setting; read it at call time so mid-session flips apply.
      if (!voiceOn()) return Promise.resolve(null);
      return prepareStreamed(text, tok).then(function (res) {
        if (res) return res;
        return prepareWhole(text, tok);
      }).catch(function () { return prepareWhole(text, tok); });
    }

    /* Original whole-file path, unchanged: synthesize the entire MP3, wait for
       loadedmetadata so duration is exact, then play. Still the fallback. */
    function prepareWhole(text, tok) {
      if (tok !== token) return Promise.resolve(null);
      if (!voiceOn()) return Promise.resolve(null);
      return api("/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      }).then(function (res) {
        if (tok !== token) return null;          // interrupted while in flight
        if (!res || !res.audio_base64) {
          console.debug("[voice] /tts returned no audio — reply stays text-only");
          return null;                           // TTS unavailable -> text only
        }
        // Setting may have been switched off during the request; respect it.
        if (!window.FocusSettings || !window.FocusSettings.get("voiceEnabled")) return null;
        return new Promise(function (resolve) {
          try {
            var audio = new Audio("data:audio/mpeg;base64," + res.audio_base64);
            audio.addEventListener("loadedmetadata", function () {
              resolve(tok === token ? { audio: audio, duration: audio.duration } : null);
            });
            audio.addEventListener("error", function () { resolve(null); });
          } catch (_) { resolve(null); /* no Audio support — text only */ }
        });
      }).catch(function (e) {
        console.debug("[voice] /tts request failed — reply stays text-only", e);
        return null;                             /* TTS failed — text only, unchanged */
      });
    }

    return { prepare: prepare, stop: stop, adopt: adopt };
  }

  /* =====================================================================
     Controller — wires the components together and owns the chat flow.
     ===================================================================== */
  /* =====================================================================
     ImageIngest (P9-02) — photo-to-plan entry points. Owns the drop-glow
     on the stage while a file drags over, the clipboard paste hook, and
     the thumbnail chip that sits in the echo position while an image is
     being read. All three entry points (drop, paste, the compose "+")
     funnel into onImage(file); the network/dispatch side lives in main()'s
     sendImage. Maps to <ImageIngest onImage={...} /> in React.
     ===================================================================== */
  function createImageIngest(appEl, onImage, setHint, restoreHint) {
    var stageEl = appEl.querySelector(".stage");

    /* --- thumbnail chip ("Reading…") in the echo position --- */
    var chipEl = null;
    function chipShow(src, label) {
      chipHide();
      if (!stageEl) return;
      chipEl = document.createElement("div");
      chipEl.className = "img-chip";
      var img = document.createElement("img");
      img.alt = "";
      img.src = src;
      var span = document.createElement("span");
      span.className = "img-chip-label";
      span.textContent = label || "Reading…";
      chipEl.appendChild(img);
      chipEl.appendChild(span);
      stageEl.appendChild(chipEl);
      if (!reduce) void chipEl.offsetWidth;   // restart the entrance slide
      chipEl.classList.add("show");
    }
    function chipHide() {
      if (chipEl && chipEl.parentNode) chipEl.parentNode.removeChild(chipEl);
      chipEl = null;
    }

    function hasFiles(e) {
      var types = e.dataTransfer && e.dataTransfer.types;
      if (!types) return false;
      for (var i = 0; i < types.length; i++) {
        if (types[i] === "Files") return true;
      }
      return false;
    }

    /* --- drag-and-drop onto the stage. depth pairs enter/leave events so
       crossing child elements never flickers the glow off. --- */
    var depth = 0;
    function endDrag() {
      depth = 0;
      if (appEl.classList.contains("dropping")) {
        appEl.classList.remove("dropping");
        if (restoreHint) restoreHint();
      }
    }
    document.addEventListener("dragenter", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth++;
      if (!appEl.classList.contains("dropping")) {
        appEl.classList.add("dropping");
        if (setHint) setHint("Show me a syllabus");
      }
    });
    document.addEventListener("dragover", function (e) {
      if (hasFiles(e)) e.preventDefault();   // required, or the browser opens the file
    });
    document.addEventListener("dragleave", function () {
      if (!appEl.classList.contains("dropping")) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) endDrag();
    });
    document.addEventListener("drop", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      var f = e.dataTransfer.files && e.dataTransfer.files[0];
      endDrag();
      if (f) onImage(f);
    });

    /* --- paste from clipboard (a screenshot straight off the clipboard) --- */
    document.addEventListener("paste", function (e) {
      var items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        if (it && it.kind === "file" && (it.type || "").indexOf("image/") === 0) {
          var f = it.getAsFile();
          if (f) { e.preventDefault(); onImage(f); }
          return;
        }
      }
    });

    return { chipShow: chipShow, chipHide: chipHide };
  }

  /* =====================================================================
     The Now (P9-07) — focus sessions. The stage becomes the current task:
     title + commitment dot + a big calm mono timer running against the
     block. Entered via the `focus` intent ("start", "let's work"), a
     "Start now" in the day-view block popover, or the quiet start-time
     hint. Esc exits without judgment. Stopping records MEASURED minutes
     through POST /blocks/{id}/log-time (source "timer" — beats any later
     self-report, server-side). Maps to <NowSession /> in React.
     ===================================================================== */

  // Pure idle-gap arithmetic for the Now timer. Given the previous tick time
  // and now, decide how much of the delta COUNTS toward the session: a delta
  // over gapMs means the clock wasn't ticking (hidden tab, sleep, no
  // activity), so NONE of it counts — absence is never silently measured.
  // Keep this function dependency-free: tests/unit/test_focus_sessions.py
  // extracts its source and runs it under node.
  function nowTickDelta(prevMs, nowMs, gapMs) {
    var d = nowMs - prevMs;
    if (!(d > 0)) return { counted: 0, gap: 0 };
    if (d > gapMs) return { counted: 0, gap: d };
    return { counted: d, gap: 0 };
  }

  function createNow(appEl, agent, hint, deps) {
    var wrap = document.getElementById("now");
    var elTitle = document.getElementById("now-title");
    var elDot = document.getElementById("now-dot");
    var elTimer = document.getElementById("now-timer");
    var elMeta = document.getElementById("now-meta");
    var elPause = document.getElementById("now-pause");
    var elStop = document.getElementById("now-stop");
    var elPrompt = document.getElementById("now-prompt");
    var elYes = document.getElementById("now-yes");
    var elNo = document.getElementById("now-no");

    var GAP_MS = 5 * 60000;            // > 5 min without a tick = idle
    var KEY = "focus.nowSession";      // {ws, block, accumulatedMs, lastSeen}
    var OFFER_KEY = "focus.nowOffers"; // {date, ids} — quiet offer once/block

    var sess = null;   // {block, accumulatedMs, running, lastTickMs}
    var rafId = null, intId = null, lastPersist = 0, lastShown = null;
    var offerTimers = [];

    /* ---------- session persistence (reload resumes, ask-don't-assume) --- */
    function persist() {
      if (!sess) return;
      try {
        localStorage.setItem(KEY, JSON.stringify({
          ws: WS, block: sess.block,
          accumulatedMs: sess.accumulatedMs, lastSeen: Date.now(),
        }));
      } catch (_) { /* storage full/blocked — the server still gets stints */ }
    }
    function clearSaved() {
      try { localStorage.removeItem(KEY); } catch (_) {}
    }

    /* ---------- the clock ---------- */
    function fmt(ms) {
      var s = Math.max(0, Math.floor(ms / 1000));
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
      function p2(n) { return (n < 10 ? "0" : "") + n; }
      return h > 0 ? h + ":" + p2(m) + ":" + p2(r) : p2(m) + ":" + p2(r);
    }
    function renderTime() {
      if (!sess) return;
      var t = fmt(sess.accumulatedMs);
      if (t !== lastShown) { elTimer.textContent = t; lastShown = t; }
    }
    function tick() {
      if (!sess) return;
      var nowMs = Date.now();
      if (sess.running) {
        var d = nowTickDelta(sess.lastTickMs, nowMs, GAP_MS);
        if (d.gap > 0) askStillOn();      // the gap itself never counts
        else sess.accumulatedMs += d.counted;
      }
      sess.lastTickMs = nowMs;
      renderTime();
      if (nowMs - lastPersist > 5000) { lastPersist = nowMs; persist(); }
    }
    function loop() {
      tick();
      rafId = requestAnimationFrame(loop);
    }
    function startLoops() {
      stopLoops();
      if (!reduce) rafId = requestAnimationFrame(loop);
      // 1s fallback carries hidden tabs and reduced motion (text-only updates)
      intId = setInterval(tick, 1000);
    }
    function stopLoops() {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = null;
      if (intId) clearInterval(intId);
      intId = null;
    }

    /* ---------- UI states ---------- */
    function isActive() { return !!sess; }
    function setPaused(paused) {
      if (!sess) return;
      sess.running = !paused;
      sess.lastTickMs = Date.now();
      wrap.classList.toggle("paused", paused);
      elPause.textContent = paused ? "Resume" : "Pause";
      persist();
    }
    function showUi() {
      var b = sess.block;
      elTitle.textContent = b.title || "This session";
      elDot.style.background = commitmentColor(b.commitment_id);
      var meta = [];
      if (b.planned_minutes) meta.push(b.planned_minutes + " min planned");
      if (b.accumulated_minutes) meta.push(b.accumulated_minutes + " min already on the clock");
      elMeta.textContent = meta.join(" · ");
      elPrompt.hidden = true;
      wrap.classList.remove("paused");
      elPause.textContent = "Pause";
      lastShown = null;
      renderTime();
      wrap.hidden = false;
      appEl.classList.add("now-focused");   // the eyes settle (face.css)
      if (deps.onEnter) deps.onEnter();
      hint.set("Esc leaves the session. Stop records the time.");
    }
    function hideUi() {
      wrap.hidden = true;
      elPrompt.hidden = true;
      wrap.classList.remove("paused");
      appEl.classList.remove("now-focused");
      agent.set(agent.get());               // re-apply the state's own hint
    }

    /* ---------- idle honesty: "Still on this?" ---------- */
    function askStillOn() {
      if (!sess) return;
      sess.running = false;
      sess.lastTickMs = Date.now();
      wrap.classList.add("paused");
      elPause.textContent = "Resume";
      elPrompt.hidden = false;
      persist();
    }
    if (elYes) elYes.addEventListener("click", function () {
      if (!sess) return;
      elPrompt.hidden = true;
      setPaused(false);                     // resume; the gap never counted
    });
    if (elNo) elNo.addEventListener("click", function () {
      exitQuiet();                          // stop here; the gap never counts
    });

    /* ---------- recording ---------- */
    function elapsedMinutes() {
      return sess ? Math.round(sess.accumulatedMs / 60000) : 0;
    }
    function postTime(blockId, minutes, complete) {
      return api("/blocks/" + encodeURIComponent(blockId) + "/log-time", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ elapsed_minutes: minutes, complete: !!complete }),
      });
    }
    function stopAndRecord() {
      if (!sess) return;
      var b = sess.block, mins = elapsedMinutes();
      setPaused(true);
      postTime(b.id, mins, true).then(function (res) {
        stopLoops();
        sess = null;
        clearSaved();
        hideUi();
        if (deps.onRecorded) deps.onRecorded(res, b);
        if (window.FocusRefresh) window.FocusRefresh();
      }).catch(function () {
        // the measurement is not lost: the session (and localStorage) keep it
        hint.pulse("Couldn't record that just now. The time is safe here; try Stop again.");
      });
    }
    // Esc / "no": leave without judgment. A stint of a minute or more is
    // still measured fact, so it accumulates server-side (complete=false);
    // under a minute there is nothing honest to write.
    function exitQuiet() {
      if (!sess) return;
      var b = sess.block, mins = elapsedMinutes();
      stopLoops();
      sess = null;
      clearSaved();
      hideUi();
      if (mins >= 1) {
        postTime(b.id, mins, false).catch(function () { /* quiet — best effort */ });
        if (window.FocusRefresh) window.FocusRefresh();
      }
    }

    /* ---------- entry points ---------- */
    // start(block): block = {id, task_id, title, planned_minutes,
    // estimate_minutes, commitment_id, accumulated_minutes} (the /turn focus
    // payload, or the popover's assembled equivalent). The timer counts THIS
    // stint from 00:00; prior measured minutes ride in the meta line and the
    // server adds stints up.
    function start(block) {
      if (!block || !block.id) return;
      if (sess && sess.block.id !== block.id) exitQuiet();  // one clock at a time
      if (sess) { showUi(); return; }        // same block: just re-show
      sess = { block: block, accumulatedMs: 0, running: true, lastTickMs: Date.now() };
      showUi();
      startLoops();
      persist();
    }

    // Reload resume: a saved session comes back PAUSED with the question up —
    // the away-time is an idle gap (ask, don't assume; it never counts).
    function restore() {
      var raw = null;
      try { raw = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (_) {}
      if (!raw || raw.ws !== WS || !raw.block || !raw.block.id) return;
      sess = {
        block: raw.block,
        accumulatedMs: raw.accumulatedMs || 0,
        running: false,
        lastTickMs: Date.now(),
      };
      showUi();
      startLoops();
      askStillOn();
    }

    // Start-time arrival (quiet offer): while the app is open and idle, the
    // moment a planned block's start arrives the HINT — nothing louder —
    // offers the session, once per block ever (localStorage ledger).
    function offerLedger() {
      function todayKey() {
        var d = new Date();
        function p2(n) { return (n < 10 ? "0" : "") + n; }
        return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate());
      }
      try {
        var raw = JSON.parse(localStorage.getItem(OFFER_KEY) || "null");
        if (raw && raw.date === todayKey()) return raw;
      } catch (_) {}
      return { date: todayKey(), ids: [] };
    }
    function armOffers() {
      offerTimers.forEach(clearTimeout);
      offerTimers = [];
      api("/details").then(function (d) {
        var titles = {};
        (d.tasks || []).forEach(function (t) { titles[t.id] = t.title; });
        var nowMs = Date.now();
        (d.blocks || []).forEach(function (b) {
          if (b.status !== "planned") return;
          var s = new Date(b.starts_at).getTime();
          if (isNaN(s)) return;
          var delay = s - nowMs;
          if (delay < -90000 || delay > 6 * 3600000) return;   // long past / far off
          offerTimers.push(setTimeout(function () {
            if (sess) return;                          // already timing
            if (agent.get() !== "idle") return;        // never interrupt
            if (document.visibilityState === "hidden") return;
            var led = offerLedger();
            if (led.ids.indexOf(b.id) !== -1) return;  // once per block
            led.ids.push(b.id);
            try { localStorage.setItem(OFFER_KEY, JSON.stringify(led)); } catch (_) {}
            var title = titles[b.task_id] || "Your session";
            hint.set('Time for "' + title + '". Say "start" when you\'re ready.');
          }, Math.max(0, delay)));
        });
      }).catch(function () { /* quiet — the offer is a nicety */ });
    }

    /* ---------- controls ---------- */
    if (elPause) elPause.addEventListener("click", function () {
      if (!sess) return;
      if (!elPrompt.hidden) { elPrompt.hidden = true; setPaused(false); return; }
      setPaused(sess.running);
    });
    if (elStop) elStop.addEventListener("click", stopAndRecord);
    // Esc exits without judgment. Capture phase so an active session owns the
    // key before the horizon/surface handlers see it.
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !sess) return;
      e.stopPropagation();
      exitQuiet();
    }, true);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden" && sess) persist();
      // returning: the next tick's nowTickDelta sees the real gap and either
      // counts it (short) or pauses + asks (over 5 min). Nothing to do here.
    });

    // P11-04: what the timer can honestly say about the block it is on right
    // now — the stint on the clock plus any earlier TIMER minutes the server
    // already holds. No session means null, and the canvas paints nothing.
    function live() {
      if (!sess) return null;
      var prior = sess.block.accumulated_minutes || 0;
      return {
        block_id: sess.block.id,
        minutes: prior + elapsedMinutes(),
        running: !!sess.running,
      };
    }

    return {
      start: start, restore: restore, armOffers: armOffers,
      isActive: isActive, exit: exitQuiet, live: live,
    };
  }

  function main() {
    reportTimezone();   // P15-00: before anything asks the server what "today" is
    var appEl = document.getElementById("app");
    var hintEl = document.getElementById("hint");
    var history = [];   // [{role, content}] passed to POST /turn (message/planned turns)
    var session = null; // {commitment_id, goal} carried through an elicitation
    // Auth gate (2026-08-30): the interactive surface is walled behind Google
    // sign-in. Locked until authGate() below resolves /v1/session — held true
    // through the "pending" beat so a stray keypress can't open the compose
    // field before we know who this is. Every input entry point (mic hold,
    // spacebar, keyboard button, first-keystroke) consults this via isLocked.
    var interactionLocked = true;
    function isInteractionLocked() { return interactionLocked; }

    var sleepCtl = null;
    // Hint line is debounced + cross-faded (P7-02) so rapid state churn
    // (mic release, mode swaps) settles on exactly one final hint.
    var hint = createHint(hintEl);
    var agent = createAgentState(appEl, hint.set, function (s) {
      // Emotion lifecycle (P7-03): entering `thinking` clears any held
      // emotion (curious ends when an answer submits, and nothing can
      // strand a class across a new request); entering `listening` opens
      // with a brief alert widen, then relaxes to the normal listening look.
      if (eyes) {
        if (s === "thinking") eyes.clearEmote();
        else if (s === "listening") eyes.emote("wide", 600);
      }
      // conversation activity cancels sleep; returning to idle re-arms it
      if (!sleepCtl) return;
      if (s === "idle") sleepCtl.reset(); else sleepCtl.pause();
    });
    var eyes = createEyes(agent, appEl);
    // P11-08: what a typed inline reference (or the one prominent action) DOES
    // when tapped. Every branch calls a capability that already existed — open
    // the plan at a level+date, start a focus session on a real block, open a
    // cited course URL. Nothing here can invent a new one, and outward-facing
    // writes keep their own confirm gates untouched. `stage` is declared just
    // below (hoisted var), and a click always happens long after that.
    function handleReplyAction(a) {
      if (!a || !a.action) return;
      if (a.action === "open_plan") { stage.openAt(a.level, a.date); return; }
      if (a.action === "start_focus" && a.block && window.FocusNow) {
        window.FocusNow.start(a.block);
        return;
      }
      if (a.action === "open_course" && a.url) {
        window.open(a.url, "_blank", "noopener");
      }
    }
    var surface = createSurface(handleReplyAction);
    // The stage orchestrator (P7-05): park morph + horizon. Opening the plan
    // is a look, not an interrupt, so the stage no longer touches the voice.
    var stage = createStage(appEl, agent);
    // Settings store is the source of truth for voice + reminders; expose it
    // as a small global so later components (P5-04 voice, P5-05 calendar) can
    // read and subscribe.
    var settings = createSettingsStore();
    window.FocusSettings = settings;
    // Face identity (P10-01/02): the store's `face` value IS <html data-face>.
    // index.html applies the persisted value before first paint; this keeps
    // it live for the session (the Settings picker flips it in place).
    function applyFace(v) {
      document.documentElement.setAttribute("data-face",
        (v === "lumen" || v === "folio") ? v : "capsule");
    }
    applyFace(settings.get("face"));
    settings.onChange(function (k, v) { if (k === "face") applyFace(v); });
    /* P15-08 — the face preference lives on the account now, so the phone and
       this page wear the same skin. localStorage stays the FAST PATH (index.html
       applies it before first paint); the server field reconciles after load.
       Conflict rule: the server's value wins when it exists and differs,
       because it is the newest pick made on ANY device; a pick made HERE goes
       straight to the server below, so "server wins on load" can only ever
       replay someone's own latest choice. Both requests degrade silently:
       offline, this page simply keeps the face it already had. */
    (function syncFace() {
      var FACE_NAMES = ["capsule", "lumen", "folio"];
      var adopting = false;   // adopting the server's value must not echo it back
      settings.onChange(function (k, v) {
        if (k !== "face" || adopting) return;
        api("/profile/face", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ face: v }),
        }).catch(function () { /* offline pick stays local; next load reconciles */ });
      });
      api("/profile").then(function (profile) {
        var remote = profile && profile.face;
        if (FACE_NAMES.indexOf(remote) === -1) return;   // null/junk: keep local
        if (remote === settings.get("face")) return;
        adopting = true;
        try { settings.set("face", remote); } finally { adopting = false; }
      }).catch(function () { /* unreachable server changes nothing */ });
    })();
    var settingsUi = createSettings(settings);
    // The quiet account chip beside the gear, plus the one-time post-plan
    // offer (P14b). Guest mode is untouched; this only makes the door visible.
    var account = createAccount(settingsUi);

    // Google OAuth returns land here: sign-in (?signin=connected|error, plus
    // ?ws= which the boot block already consumed) and the calendar-only
    // connect (?calendar=connected|missing_scope|error). Capture the
    // outcomes, then clean the URL either way.
    (function handleOAuthReturn() {
      var qs = window.location.search || "";
      var sm = /[?&]signin=([a-z_]+)/.exec(qs);
      var cm = /[?&]calendar=([a-z_]+)/.exec(qs);
      if (!sm && !cm) return;
      if (sm) window.__focusSigninFlash = sm[1];
      if (cm && cm[1] === "missing_scope") {
        window.__focusCalendarFlash = "missing_scope";
        if (settingsUi && settingsUi.open) settingsUi.open();
      }
      try {
        var clean = window.location.pathname + window.location.hash;
        window.history.replaceState({}, document.title, clean);
      } catch (_) { /* history unavailable — leave the URL as-is */ }
    })();
    var voice = createVoice();
    sleepCtl = createSleep(appEl, agent, eyes);

    function setHint(t) { hint.set(t); }

    /* --- User echo (P7-02): one quiet line of what you just said. --------
       A compact right-aligned chip above the surface. Shows on every send /
       clarify answer, dims to ~40% when the reply starts rendering, and is
       replaced on the next turn (or cleared on dismiss). One turn only —
       no history list. */
    var echoEl = document.getElementById("echo");
    function showEcho(text) {
      if (!echoEl) return;
      echoEl.textContent = text || "";
      echoEl.classList.remove("dim", "show");
      if (!reduce) void echoEl.offsetWidth;   // restart the entrance slide
      echoEl.classList.add("show");
    }
    function dimEcho() { if (echoEl) echoEl.classList.add("dim"); }
    function clearEcho() {
      if (!echoEl) return;
      echoEl.classList.remove("show", "dim");
      echoEl.textContent = "";
    }
    // Clarify answers can be strings, arrays (multi_select), or range objects.
    function echoValue(v) {
      if (v == null) return "";
      if (Array.isArray(v)) return v.join(", ");
      if (typeof v === "object") {
        return Object.keys(v).map(function (k) { return v[k]; }).join(" to ");
      }
      return String(v);
    }

    /* --- Double-submit guard (P7-02): one request in flight at a time. ---
       busy covers /turn and /elicit/answer. While busy, Enter/Send do
       nothing but pulse the hint; the mic hold is still an interrupt (it
       cuts speech) but its commit lands back in sendMessage's guard, so it
       can never queue a second request. */
    var busy = false;
    function setSendsDisabled(on) {
      ["send", "ask-send"].forEach(function (id) {
        var b = document.getElementById(id);
        if (b) b.disabled = !!on;
      });
    }
    /* --- P12-02: the thinking beat in deep mode. -------------------------
       Deep mode genuinely takes longer, and the existing `thinking` eye state
       is what that wait should look like (latency as character). So on a deep
       turn the squint is held to a FLOOR of
       DEEP_THINK_HOLD_MS, which stops a quick deep reply from snapping back
       before the beat has read as consideration. It is a floor, never an added
       delay: a turn that already took longer waits zero extra, and fast mode
       never waits at all. No new animation, no claim about the reply, and
       reduced-motion skips the hold entirely. */
    var DEEP_THINK_HOLD_MS = 900;
    var thinkStartedAt = 0;
    function beginRequest() { busy = true; thinkStartedAt = Date.now(); setSendsDisabled(true); }
    function heldDispatch(res) {
      var left = 0;
      if (deepModeOn() && !reduce && thinkStartedAt) {
        left = DEEP_THINK_HOLD_MS - (Date.now() - thinkStartedAt);
      }
      if (left > 0) { setTimeout(function () { dispatch(res); }, left); return; }
      dispatch(res);
    }
    function endRequest() { busy = false; setSendsDisabled(false); }

    /* --- Reply lifecycle (P7-01): replies PERSIST after completion. -------
       settle() (auto-hide after a delay) is gone. Instead:
         - onSpoken(): agent returns to idle, the surface stays visible, and
           a 20s auto-MINIMIZE timer starts (pointer/key activity restores).
         - dismiss: click on the surface (outside interactive children) or
           Esc hides it and stops any audio.
         - startTurn(): the one interrupt path (see below). */
    var surfaceEl = document.getElementById("surface");
    var replyToken = 0;      // per-reply; bumped on every new reply / interrupt
    var minTimer = null;
    var hoveringSurface = false;

    /* --- startTurn(): beginning a turn IS the interrupt (P12-04) ----------
       One primitive, because "the user is starting something new" and "the
       previous reply ends here" are the same event, and splitting them is
       how the two halves of a reply drifted apart. It cuts the voice, stops
       the words that voice was writing, and claims the surface. Every
       painter in the turn carries the claim it returns, so the instant the
       NEXT turn begins, everything still in flight from this one — a late
       fallback, a late audio, a queued cross-fade, a half-typed line, a
       continuation ask — refuses to paint instead of landing on top.

       Before this, the ask-submit paths (clarify answer, check-in answer,
       insight verdict, onboarding step) started a turn with pending() alone
       and never touched the voice, so a still-playing reply kept talking
       under the new one. Now there is nowhere left to forget it.

       WHAT COUNTS AS AN INTERRUPT: something the user does to START
       SPEAKING OR SEND. Not something they do to LOOK at part of the app.
         interrupts  — typing/sending a turn, sending a photo, holding the
                       mic or Spacebar, opening the compose field, answering
                       a question, dismissing the reply outright.
         do NOT      — opening Settings, pulling the plan into view, hovering
                       or scrolling the reply, switching face or palette.
       (Settings and the plan used to cut the voice mid-sentence. The user
       reported it on 2026-08-27 and they are both gone.) */
    var turn = null;         // the surface claim the current turn holds
    function startTurn() {
      replyToken++;
      voice.stop();          // this turn's voice ends here…
      surface.abort();       // …and so do the words it was still writing
      turn = surface.claim();
      return turn;
    }

    function armMinimize() {
      clearTimeout(minTimer);
      if (!surfaceEl.classList.contains("show")) return;
      minTimer = setTimeout(function () {
        if (hoveringSurface) return;   // paused while hovering the surface
        if (surfaceEl.classList.contains("show") && agent.get() === "idle") {
          surfaceEl.classList.add("surface-min");
        }
      }, 20000);
    }
    function restoreSurface() {
      // while viewing, the surface stays receded — the horizon owns the room
      if (appEl.classList.contains("viewing")) return;
      surfaceEl.classList.remove("surface-min");
      if (surfaceEl.classList.contains("show") && agent.get() === "idle") armMinimize();
    }
    document.addEventListener("pointermove", restoreSurface, { passive: true });
    document.addEventListener("keydown", restoreSurface, { passive: true });
    surfaceEl.addEventListener("pointerenter", function () {
      hoveringSurface = true; clearTimeout(minTimer);
    });
    surfaceEl.addEventListener("pointerleave", function () {
      hoveringSurface = false;
      if (surfaceEl.classList.contains("show") && agent.get() === "idle") armMinimize();
    });

    function dismissSurface() {
      // Dismissing is the user throwing the whole reply away, so both halves
      // go: startTurn cuts the voice and the type-on together and retires the
      // claim, which is why nothing from it can paint itself back.
      startTurn();
      eyes.clearEmote();                // a dismissed turn never holds a face
      clearTimeout(minTimer);
      surfaceEl.classList.remove("surface-min");
      surface.hide();
      clearEcho();                      // the turn is over; the echo goes too
      if (agent.get() === "speaking") agent.set("idle");
    }
    // Click anywhere on a finished (or still-speaking) reply dismisses it —
    // but never a compose field, ask control, or other interactive child.
    surfaceEl.addEventListener("click", function (e) {
      if (!surfaceEl.classList.contains("show")) return;
      var t = e.target;
      if (t && t.closest && t.closest("textarea, input, button, select, a, label, [contenteditable]")) return;
      var s = agent.get();
      if (s !== "idle" && s !== "speaking") return;   // compose/ask/thinking own the surface
      dismissSurface();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (appEl.classList.contains("viewing")) return;   // Esc closes the horizon first (P7-05)
      if (!surfaceEl.classList.contains("show")) return;
      var s = agent.get();
      if (s === "idle" || s === "speaking") dismissSurface();
    });

    // Speech/typing completed: hint returns to idle, the reply stays up.
    function onSpoken() {
      agent.set("idle");
      armMinimize();
    }

    // Deliver one reply: race prepare(text) against a time budget.
    //   audio wins  -> caption-synced reveal (surface.speakSynced)
    //   timeout/off -> fast type-on fallback; late audio is dropped, not played
    // The budget is generous when the user has voice ON (Cloud TTS needs 2-4s
    // for a full reply, and a silent reply breaks the promise of the toggle;
    // the eyes are already in their speaking state, so the wait reads as the
    // agent drawing breath) and instant-ish when voice is off.
    // `decor` (P11-08) is the ADDITIVE {refs, actions} the server sent beside
    // the reply. `text` is untouched and stays exactly what TTS speaks.
    function deliverReply(text, done, decor) {
      var tok = ++replyToken;
      // A reply does not get a claim of its own: it RIDES the claim of the
      // turn it answers (P12-04). P11-12 had deliverReply claim at hand-off,
      // which was better than claiming at paint time but still let a reply
      // take the room from whatever was already using it. Now the only thing
      // that ever claims is startTurn(), so a reply whose turn is over paints
      // nothing, and so does every continuation hanging off it.
      var rseq = turn;
      var settled = false;
      try { console.debug("[reply]", tok, "deliver:", (text || "").slice(0, 40)); } catch (_) {}
      agent.set("speaking");
      function fallback() {
        if (settled || tok !== replyToken) return;
        if (!surface.holds(rseq)) {          // a newer turn owns the surface
          settled = true;
          try { console.debug("[reply]", tok, "surface moved on, not painting"); } catch (_) {}
          return;
        }
        settled = true;
        try { console.debug("[reply]", tok, "fallback type-on"); } catch (_) {}
        surface.speak(text, done, decor, rseq);
      }
      var voiceOn = !!(window.FocusSettings && window.FocusSettings.get("voiceEnabled"));
      var timer = setTimeout(fallback, voiceOn ? 4500 : 1500);
      // Hidden tabs throttle timers (down to 1/min under intensive throttling),
      // which can strand the race un-settled. If we're hidden, or become
      // visible again while still racing, settle to the fallback right away.
      if (document.visibilityState === "hidden") fallback();
      document.addEventListener("visibilitychange", function onVis() {
        document.removeEventListener("visibilitychange", onVis);
        if (!settled && tok === replyToken) fallback();
      });
      // P12-03b: an unplayed stream keeps an audio context and a socket open,
      // so every path that declines the audio closes it rather than dropping
      // the reference. Harmless on the whole-file <audio> too.
      function discard(res) {
        if (res && res.audio && res.audio.pause) { try { res.audio.pause(); } catch (_) {} }
      }
      voice.prepare(text).then(function (res) {
        clearTimeout(timer);
        if (tok !== replyToken) { discard(res); try { console.debug("[reply]", tok, "superseded, dropped"); } catch (_) {} return; }
        if (settled) { discard(res); return; }  // fallback already typing — don't play
        if (!res || !res.audio) { fallback(); return; }
        if (!surface.holds(rseq)) {            // a newer turn owns the surface
          settled = true;
          discard(res);
          try { console.debug("[reply]", tok, "surface moved on, audio dropped"); } catch (_) {}
          return;
        }
        settled = true;
        try { console.debug("[reply]", tok, "audio won -> synced"); } catch (_) {}
        voice.adopt(res.audio);                // stop() can now cut it off
        surface.speakSynced(text, res.audio, done, decor, rseq);
      }).catch(function () { clearTimeout(timer); fallback(); });
    }

    // First planned response this page load — gates the heart moment (P7-03).
    var plannedOnce = false;

    // One reply, one calm recovery — same sorry tone as a person would use.
    function fail() {
      endRequest();
      dimEcho();
      eyes.emote("sorry", 1800);        // droop while the apology lands (P7-03)
      agent.set("speaking");
      // The apology belongs to the turn that failed, so it rides that turn's
      // claim: if the user has already moved on, they get their new turn
      // rather than an apology for the one they abandoned.
      surface.speak(
        "Sorry, I couldn't reach the planner just now. Give me a moment, then try again.",
        onSpoken, null, turn
      );
    }

    // Dispatch a /turn or /elicit/answer response by its `type`.
    //   message  -> speak the reply (audio-synced when possible), then idle
    //   planned  -> speak the summary, then idle (the week morph lands in P7-05)
    //   question -> enter (or continue) the elicitation loop
    // The additive decoration fields, or null. Absent = the reply renders
    // exactly as it did before P11-08 (degradation is free).
    function decorOf(res) {
      if (!res) return null;
      // P20-02: the additive artifact payloads ride the same decoration seam
      // as refs/actions. Each is null when absent, and the surface renders
      // NOTHING for a null (no payload, no artifact).
      var hasArt = res.trace ||
                   (res.artifacts && res.artifacts.sessions) ||
                   res.moves ||
                   (res.sources && res.sources.length);
      if (!res.refs && !res.actions && !hasArt) return null;
      return {
        refs: res.refs || null,
        actions: res.actions || null,
        trace: res.trace || null,
        artifacts: res.artifacts || null,
        moves: res.moves || null,
        calendar_note: res.calendar_note || null,
        sources: res.sources || null,
        query: res.query || null,
      };
    }

    function dispatch(res) {
      endRequest();
      dimEcho();                        // the reply is rendering: recede
      if (!res || !res.type) return fail();

      if (res.type === "message") {
        var reply = res.text || "…";
        history.push({ role: "assistant", content: reply });
        // Honest-miss beat (P9-00a): when the reply owns up to not finding
        // anything actionable, the eyes look sheepish while it says so.
        // Presentation-only string check — the truth itself is server-side.
        if (/didn't find|couldn't place|couldn't read/i.test(reply)) {
          eyes.emote("sheepish", 1600);
        }
        deliverReply(reply, function () {
          onSpoken();
          // Typed intent (P7-05): the user asked to SEE the plan — even a
          // chat reply opens the horizon once it finishes rendering.
          if (stage.consumeIntent()) stage.open();
        }, decorOf(res));
        return;
      }

      if (res.type === "planned") {
        var summary = res.text || "All set. Your week is updated.";
        history.push({ role: "assistant", content: summary });
        session = null;                 // the commitment is resolved
        // Emotion beat (P7-03): the first plan of the session that actually
        // placed blocks earns the heart moment; every other plan gets the
        // happy crescent. Either way the reply closes on one slow
        // "satisfied" blink once the words finish.
        var placed = res.blocks_scheduled || 0;
        var unplaced = (res.schedule && res.schedule.unplaced && res.schedule.unplaced.length) || 0;
        if (!plannedOnce && placed > 0) {
          plannedOnce = true;
          eyes.emote("heart", 900);
        } else if (placed === 0 || unplaced > 0) {
          // some (or all) of it couldn't be placed — the worry brows say so
          // before the words do (P9-00a)
          eyes.emote("worried", 1800);
        } else {
          // every later successful plan gets the quieter proud crescent;
          // heart stays reserved for the first (P9-00a)
          eyes.emote("proud", 1600);
        }
        deliverReply(summary, function () {
          onSpoken();
          // The hinge (P7-05): reply text finishes -> one satisfied slow
          // blink -> the eyes park and the fresh plan materializes.
          eyes.emote("satisfied");
          stage.openSoon();
          // P14b: a real plan just landed, so keeping it across devices now
          // means something. One quiet offer, after the words, never during
          // the first-run interview, never twice.
          if (placed > 0 && !onboardingActive) {
            setTimeout(function () { account.offerAfterPlan(); }, 900);
          }
        }, decorOf(res));
        return;
      }

      if (res.type === "replanned") {
        // P9-01 "life happens": the rebalancer already ran server-side; the
        // reply carries the REAL counts. Worried when something couldn't be
        // re-placed, proud when everything found room. (The animated week
        // diff lands with the rest of P9-01.)
        var rtext = res.text || "I rebalanced today.";
        history.push({ role: "assistant", content: rtext });
        var rCancelled = res.cancelled_blocks || 0;
        var rMoved = res.rescheduled_blocks || 0;
        if (rCancelled > 0 && rMoved < rCancelled) eyes.emote("worried", 1800);
        else if (rCancelled + rMoved > 0) eyes.emote("proud", 1600);
        stage.stageDiff(res);           // the week diff plays when it opens
        deliverReply(rtext, function () {
          onSpoken();
          if (rCancelled + rMoved > 0) {
            eyes.emote("satisfied");
            stage.openSoon();
          }
        }, decorOf(res));
        return;
      }

      if (res.type === "question") {
        session = res.session || session;   // {commitment_id, goal}
        askQuestion(res.question || {});
        return;
      }

      if (res.type === "courses") {
        // P9-04: search-grounded course cards. Same posture as any other
        // question: eyes stay asking, curious held until the answer commits.
        session = res.session || session;
        askCourses(res);
        return;
      }

      if (res.type === "checkin") {
        // P9-03 evening check-in: walk today's blocks one at a time.
        runCheckin(res);
        return;
      }

      if (res.type === "teach") {
        // P9-08 taught zones: the server parsed a standing life fact and
        // answers with a CONFIRM question. The zone rides the payload and is
        // stored ONLY on yes (POST /onboarding/answer step "taught_zone").
        var teachQ = res.question || {
          question: res.text || "Keep that clear every week?",
          field: "teach_confirm", input_type: "confirm",
        };
        history.push({ role: "assistant", content: teachQ.question || "" });
        agent.set("asking");
        eyes.emote("curious", 0);
        surface.ask(teachQ, function (v) {
          if (busy) { hint.pulse("One at a time. Still thinking…"); return; }
          if (v === true && res.zone) {
            showEcho("Yes, keep it clear");
            agent.set("thinking");
            surface.pending(startTurn());
            beginRequest();
            api("/onboarding/answer", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ step: "taught_zone", value: res.zone, mode: thinkingMode() }),
            }).then(function (r) {
              dispatch(r);
              if (window.FocusRefresh) window.FocusRefresh();   // capacity changed
            }).catch(fail);
          } else {
            showEcho("No, leave it");
            var leaveLine = "Okay, I'll leave it out.";
            history.push({ role: "assistant", content: leaveLine });
            deliverReply(leaveLine, onSpoken);
          }
        }, turn);
        return;
      }

      if (res.type === "focus") {
        // P9-07: the server picked the block to time (current, else next
        // today). Speak the line, then the stage becomes the Now.
        var ftext = res.text || "Starting.";
        history.push({ role: "assistant", content: ftext });
        deliverReply(ftext, function () {
          onSpoken();
          if (res.block) nowCtl.start(res.block);
        }, decorOf(res));
        return;
      }

      fail();   // unknown type — recover gently
    }

    /* --- Evening check-in loop (P9-03a) --------------------------------
       The server handed back today's unresolved blocks; ask about each in
       turn (done / partial / skipped — the existing clarify single_select),
       POST each answer to /checkin/resolve, then close with the grounded
       summary from /checkin/summary. One question per turn, voice rules
       intact. The celebrate beat fires ONLY when the summary says the
       streak really just reached 7 (truthful-emotion rule). */
    function localTodayKey() {
      var d = new Date();
      function p2(n) { return (n < 10 ? "0" : "") + n; }
      return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate());
    }
    function fmtClock(iso) {
      var d = new Date(iso);
      return isNaN(d) ? "" : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    }
    function runCheckin(res) {
      var blocks = res.blocks || [];
      var intro = res.text || "Let's close out today.";
      history.push({ role: "assistant", content: intro });
      var i = 0;

      function askNext() {
        if (i >= blocks.length) { finishCheckin(); return; }
        var b = blocks[i];
        agent.set("asking");
        eyes.emote("curious", 0);
        var when = fmtClock(b.starts_at);
        surface.ask({
          question: (b.title || "That session") + (when ? ", " + when : "") + ". How did it go?",
          field: "checkin",
          input_type: "single_select",
          options: [
            { label: "Done", value: "done" },
            { label: "Partial", value: "partial" },
            { label: "Skipped", value: "skipped" },
          ],
          why: "",
        }, function (value) {
          if (busy) { hint.pulse("One at a time, still thinking…"); return; }
          if (value !== "done" && value !== "partial" && value !== "skipped") return;
          showEcho((b.title || "Session") + ": " + value);
          agent.set("thinking");
          surface.pending(startTurn());
          beginRequest();
          api("/checkin/resolve", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ block_id: b.id, outcome: value }),
          }).then(function () {
            endRequest();
            i++;
            askNext();
          }).catch(fail);
        }, turn);
      }

      function finishCheckin() {
        agent.set("thinking");
        surface.pending(startTurn());
        beginRequest();
        api("/checkin/summary", { method: "POST" }).then(function (s) {
          endRequest();
          dimEcho();
          var text = (s && s.text) || "Today is closed out.";
          history.push({ role: "assistant", content: text });
          var KEY7 = "focus.streak7";
          var today = localTodayKey();
          var celebrated = false;
          try { celebrated = localStorage.getItem(KEY7) === today; } catch (_) {}
          if (s && s.streak === 7 && s.streak_incremented_today && !celebrated) {
            // the one earned celebration: the streak really just hit 7
            try { localStorage.setItem(KEY7, today); } catch (_) {}
            eyes.emote("celebrate", 1400);
          } else if (s && (s.done || 0) > 0 && (s.skipped || 0) === 0) {
            eyes.emote("proud", 1600);
          }
          deliverReply(text, function () {
            onSpoken();
            // P9-09: at most one insight rides the summary; after the
            // summary speaks, it becomes a consent ask. Absent = silence.
            if (s && s.insight) askInsight(s.insight);
          }, decorOf(s));
          if (window.FocusRefresh) window.FocusRefresh();   // actuals changed
        }).catch(fail);
      }

      // Speak the intro, then start on the first block.
      deliverReply(intro, function () { askNext(); }, decorOf(res));
    }

    /* --- Continued learning (P9-09): consent-gated insight ask ----------
       The server mined the pattern and phrased it with the real numbers;
       this only renders the ask (existing confirm kit) and posts the
       verdict to /onboarding/answer step "insight_response". Accept
       graduates the suggestion into memory server-side (the reply cites
       what changed); decline persists a dismissal so the same insight is
       never offered again. Eyes: curious while it's up, satisfied only on
       accept (truthful-emotion rule: something really was learned). */
    function askInsight(insight) {
      if (!insight || !insight.insight_id || !insight.text) return;
      history.push({ role: "assistant", content: insight.text });
      agent.set("asking");
      eyes.emote("curious", 0);
      surface.ask({
        question: insight.text,
        field: "insight",
        input_type: "confirm",
        options: [{ label: "Adapt" }, { label: "Leave it" }],
        why: insight.evidence_text || "",
      }, function (v) {
        if (busy) { hint.pulse("One at a time. Still thinking…"); return; }
        var accepted = v === true;
        showEcho(accepted ? "Adapt" : "Leave it");
        agent.set("thinking");
        surface.pending(startTurn());
        beginRequest();
        api("/onboarding/answer", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            step: "insight_response",
            mode: thinkingMode(),
            value: {
              insight_id: insight.insight_id,
              accept: accepted,
              insight: insight,
            },
          }),
        }).then(function (r) {
          if (accepted) {
            eyes.emote("satisfied");
            if (window.FocusRefresh) window.FocusRefresh();  // memory changed
          }
          dispatch(r);
        }).catch(fail);
      }, turn);
    }

    // The elicitation loop: render the question, and on submit POST the answer
    // to /elicit/answer and dispatch the next step (another question, or the
    // final plan). session carries commitment_id + goal between rounds.
    function askQuestion(question) {
      agent.set("asking");
      eyes.emote("curious", 0);         // held until the answer submits (P7-03)
      surface.ask(question, function (value) {
        if (busy) { hint.pulse("One at a time, still thinking…"); return; }
        // P17-01: a calendar confirm the agent surfaced through /turn commits
        // its YES against the EXISTING confirm-gated write endpoint, carrying
        // the pending action in question.config (action/event_id/summary/
        // start/end). This is the only place the write actually lands; the
        // generic /elicit/answer path below never touches the calendar. "Not
        // now" (false) dismisses without writing, and no reply ever claims a
        // calendar change that did not return success.
        if (question.input_type === "confirm" &&
            typeof question.field === "string" &&
            question.field.indexOf("calendar_") === 0) {
          if (value !== true) {                    // Not now
            showEcho("Not now");
            var skipLine = "Okay, I'll leave your calendar as it is.";
            history.push({ role: "assistant", content: skipLine });
            deliverReply(skipLine, onSpoken);
            return;
          }
          showEcho("Yes");
          agent.set("thinking");
          surface.pending(startTurn());
          beginRequest();
          var writeBody = { confirm: true };
          var cfg = question.config || {};
          Object.keys(cfg).forEach(function (k) { writeBody[k] = cfg[k]; });
          api("/calendar/events", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(writeBody),
          }).then(function () {
            var action = cfg.action || "";
            var line = action === "delete" ? "Done, that's off your calendar now."
                     : action === "edit"   ? "Done, I've updated that event."
                     :                        "Done, it's on your calendar now.";
            if (window.FocusRefresh) window.FocusRefresh();   // capacity changed
            heldDispatch({ type: "message", text: line });
          }).catch(function () {
            heldDispatch({ type: "message",
              text: "I couldn't reach your calendar to make that change, so nothing changed." });
          });
          return;
        }
        // P17-03: a web-search confirm the agent surfaced through /turn commits
        // its YES against the /web-search endpoint, which remembers consent and
        // runs Gemini's own Google Search grounding (never a third-party API).
        // The pending query rides in question.config.query. "Not now" (false)
        // searches nothing and plans with what's known; consent stays unset so a
        // later explicit ask may re-offer. Mirrors the calendar confirm above.
        if (question.input_type === "confirm" &&
            question.field === "web_search") {
          if (value !== true) {                    // Not now
            showEcho("Not now");
            var noSearch = "Okay, I'll plan with what I already know.";
            history.push({ role: "assistant", content: noSearch });
            deliverReply(noSearch, onSpoken);
            return;
          }
          showEcho("Yes");
          agent.set("thinking");
          surface.pending(startTurn());
          beginRequest();
          var wsCfg = question.config || {};
          api("/web-search", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: wsCfg.query, mode: thinkingMode() }),
          }).then(function (r) {
            // P20-02: the search artifact shows the query that actually ran.
            // The server reply carries the sources; the query it answers is
            // the one THIS request posted, so tag it on only when the reply
            // is a cited one and the server did not name it itself.
            if (r && r.sources && r.sources.length && !r.query && wsCfg.query) {
              r.query = wsCfg.query;
            }
            heldDispatch(r);
          }).catch(fail);
          return;
        }
        // P19-06: a reschedule confirm the agent surfaced through /turn commits
        // its YES against the /reschedule endpoint, which replays the single-use
        // batch server-side — cancel the old placements, commit the new ones —
        // and answers with the REAL moved count. The token rides in
        // question.config.token. "Not now" (false) writes nothing; the plan
        // stays as it is. The reply shown is always the server's own sentence,
        // via the shared dispatch — never a fabricated "moved N". Mirrors the
        // calendar and web_search confirms above.
        if (question.input_type === "confirm" &&
            question.field === "reschedule") {
          if (value !== true) {                    // Not now
            showEcho("Not now");
            var keepLine = "Okay, I'll leave your plan as it is.";
            history.push({ role: "assistant", content: keepLine });
            deliverReply(keepLine, onSpoken);
            return;
          }
          showEcho("Yes");
          agent.set("thinking");
          surface.pending(startTurn());
          beginRequest();
          var rsCfg = question.config || {};
          api("/reschedule", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ confirm: true, token: rsCfg.token }),
          }).then(function (r) {
            if (window.FocusRefresh) window.FocusRefresh();   // plan changed
            heldDispatch(r);
          }).catch(fail);
          return;
        }
        // P17-02: the personal-why beat is skippable, emitting the {__skip:true}
        // sentinel. A skip posts a null value, which the server treats as a
        // first-class skip (no why stored, reminders keep the plain line).
        var skipped = !!(value && value.__skip);
        showEcho(skipped ? "Skip" : echoValue(value));
        agent.set("thinking");
        surface.pending(startTurn());
        beginRequest();
        api("/elicit/answer", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            commitment_id: session && session.commitment_id,
            goal: session && session.goal,
            field: question.field,
            value: skipped ? null : value,
            mode: thinkingMode(),
          }),
        }).then(heldDispatch).catch(fail);
      }, turn);
    }

    // P9-04 "Blink found these": render the grounded course cards and POST
    // the picks (or an empty Skip) to /elicit/courses, whose reply is the
    // normal planned response and rides the SAME dispatch.
    function askCourses(res) {
      agent.set("asking");
      eyes.emote("curious", 0);         // held while the cards are up
      surface.ask({
        question: res.text || "I found real courses that fit. Want the plan built around any of them?",
        field: "courses",
        input_type: "courses",
        why: "Found by a live search. Links open in a new tab.",
        courses: res.courses || [],
      }, function (value) {
        if (busy) { hint.pulse("One at a time, still thinking…"); return; }
        var picked = (value && value.courses) || [];
        showEcho(picked.length
          ? "Build around " + picked.length + (picked.length === 1 ? " course" : " courses")
          : "Skip those, plan without them");
        agent.set("thinking");
        surface.pending(startTurn());
        beginRequest();
        api("/elicit/courses", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            commitment_id: session && session.commitment_id,
            goal: session && session.goal,
            courses: picked,
            mode: thinkingMode(),
          }),
        }).then(heldDispatch).catch(fail);
      }, turn);
    }

    /* --- First-run onboarding (P9-08): the agent learns the user's life. --
       A brand-new workspace (server onboarded=false, no localStorage
       dismissal) gets the interview INSTEAD of the idle eyes: wake beat,
       a short spoken intro, then four tap questions through the existing
       ask pipeline, each posting to /onboarding/answer. Every answer is
       skippable; skipping everything still finishes (onboarded, empty
       memory, never nagged again). Talking over the interview abandons it,
       and an abandoned interview earns ONE gentle hint on a later load,
       nothing more. */
    var onboardingActive = false;
    var ONB_DISMISS = "focus.onboard.dismissed";
    function abandonOnboarding() {
      if (!onboardingActive) return;
      onboardingActive = false;
      try { localStorage.setItem(ONB_DISMISS, "1"); } catch (_) {}
    }
    function postOnboarding(body, cb) {
      body = Object.assign({ mode: thinkingMode() }, body);
      agent.set("thinking");
      surface.pending(startTurn());
      beginRequest();
      api("/onboarding/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (res) {
        endRequest();
        cb(res);
      }).catch(function () {
        abandonOnboarding();
        fail();
      });
    }
    function stepOnboarding(res) {
      if (!onboardingActive) return;    // the user moved on mid-flight
      if (res && res.type === "onboarding_question") {
        var proceed = function () { askOnboarding(res); };
        if (res.intro) {
          history.push({ role: "assistant", content: res.intro });
          deliverReply(res.intro, proceed);
        } else {
          proceed();
        }
        return;
      }
      // The interview is over: the server spoke a grounded summary of what
      // it stored (or an honest "nothing stored"). Mark done locally too so
      // a recycled in-memory store never re-runs it on this browser.
      onboardingActive = false;
      try { localStorage.setItem(ONB_DISMISS, "1"); } catch (_) {}
      if (res && res.type === "message") {
        var otext = res.text || "Done.";
        history.push({ role: "assistant", content: otext });
        deliverReply(otext, onSpoken);
        if (window.FocusRefresh) window.FocusRefresh();   // capacity changed
      }
    }
    function askOnboarding(res) {
      agent.set("asking");
      eyes.emote("curious", 0);
      var q = {};
      Object.keys(res.question || {}).forEach(function (k) { q[k] = res.question[k]; });
      q.skippable = true;
      surface.ask(q, function (value) {
        if (busy) { hint.pulse("One at a time. Still thinking…"); return; }
        if (!onboardingActive) return;
        var skipped = !!(value && value.__skip);
        showEcho(skipped ? "Skip" : echoValue(value));
        postOnboarding({
          step: res.step,
          value: skipped ? null : value,
          skipped: skipped,
          pending: res.pending || null,
        }, stepOnboarding);
      }, turn);
    }
    function runOnboarding() {
      var dismissed = null;
      try { dismissed = localStorage.getItem(ONB_DISMISS); } catch (_) {}
      api("/details").then(function (d) {
        if (!d || d.onboarded !== false) return;
        if (dismissed) {
          // One gentle re-offer, ever, via the hint. Then silence.
          var HINTED = "focus.onboard.hinted";
          try {
            if (localStorage.getItem(HINTED)) return;
            localStorage.setItem(HINTED, "1");
          } catch (_) { return; }
          if (agent.get() === "idle" && !busy) {
            hint.set('You can teach me your week anytime. "I work 9 to 5" is enough.');
          }
          return;
        }
        if (busy || agent.get() !== "idle") return;
        onboardingActive = true;
        sleepCtl.wake();
        eyes.emote("surprised", 650);   // the wake beat: "oh, you're new here"
        postOnboarding({ step: "start" }, stepOnboarding);
      }).catch(function () { /* quiet; the interview can wait for a reload */ });
    }

    function sendMessage(text) {
      if (busy) { hint.pulse("One at a time, still thinking…"); return; }
      abandonOnboarding();              // talking over the interview ends it
      startTurn();                      // a new turn always cuts off the old reply
      stage.noteIntent(text);           // "show my week"? arm the reveal (P7-05)
      showEcho(text);
      history.push({ role: "user", content: text });
      agent.set("thinking");
      surface.pending(turn);
      beginRequest();
      api("/turn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: history, mode: thinkingMode() }),
      }).then(heldDispatch).catch(fail);
    }

    // Opening Settings deliberately used to cut the voice mid-sentence. It
    // does not any more (user report, 2026-08-27): reaching for a panel is
    // looking, not talking, and the reply keeps both its voice and its words
    // while the panel is open. Starting a hold (mic or Spacebar) or opening
    // the compose field IS a new turn, and startTurn rides in as onBegin.
    var voiceInput = createVoiceInput(agent, surface, sendMessage, setHint, startTurn, isInteractionLocked);

    /* --- Photo-to-plan (P9-02): drop / paste / compose "+" all land in
       sendImage, which validates, shows the reading chip, squints the eyes,
       POSTs to /ingest-image, and hands the reply to the SAME dispatch as a
       typed turn — so planned/message replies speak, emote, and open the
       horizon identically. --- */
    function sendImage(file) {
      if (busy) { hint.pulse("One at a time, still thinking…"); return; }
      if (!file || (file.type || "").indexOf("image/") !== 0) {
        hint.pulse("I can only read images. A syllabus screenshot works best.");
        return;
      }
      if (file.size > 8 * 1024 * 1024) {
        hint.pulse("That image is over 8MB. A smaller screenshot reads fine.");
        return;
      }
      var reader;
      try { reader = new FileReader(); } catch (_) { reader = null; }
      if (!reader) {
        hint.pulse("I couldn't open that image here.");
        return;
      }
      reader.onload = function () {
        var dataUrl = reader.result;
        if (typeof dataUrl !== "string" || dataUrl.indexOf(",") < 0) {
          hint.pulse("I couldn't open that image. Try another one?");
          return;
        }
        if (busy) { hint.pulse("One at a time, still thinking…"); return; }
        startTurn();                    // a new turn always cuts off the old reply
        imageIngest.chipShow(dataUrl, "Reading…");
        history.push({ role: "user", content: "(shared a photo of a syllabus or timetable)" });
        agent.set("thinking");          // eyes squint while the image is read
        surface.pending(turn);
        beginRequest();
        api("/ingest-image", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            image_base64: dataUrl.slice(dataUrl.indexOf(",") + 1),
            mime: file.type,
            mode: thinkingMode(),
          }),
        }).then(function (res) {
          imageIngest.chipHide();
          heldDispatch(res);
        }).catch(function () {
          imageIngest.chipHide();
          fail();
        });
      };
      reader.onerror = function () {
        hint.pulse("I couldn't open that image. Try another one?");
      };
      try { reader.readAsDataURL(file); } catch (_) {
        hint.pulse("I couldn't open that image. Try another one?");
      }
    }
    // Re-setting the current state re-applies its hint after a drag ends.
    var imageIngest = createImageIngest(appEl, sendImage,
      function (t) { hint.set(t); },
      function () { agent.set(agent.get()); });
    // The dock trio (P11-02a): the keyboard button opens the field through
    // the SAME path as Enter-on-the-mic, and "+" feeds the same sendImage
    // that drag-drop and clipboard paste use.
    createDock({
      onKeyboard: function () { voiceInput.openCompose(); },
      onFile: sendImage,
    });

    // Autoplay priming (P7-01): one real user gesture unlocks audio playback
    // for the session, so the first synced reply isn't blocked by the browser.
    document.addEventListener("pointerdown", function prime() {
      try {
        var a = new Audio();
        a.muted = true;
        var p = a.play();
        if (p && p.catch) p.catch(function () { /* fine — gesture still registered */ });
      } catch (_) { /* no Audio support — nothing to prime */ }
    }, { once: true });

    // Local block reminders (P9-03d): armed here, re-armed on every refresh
    // so a fresh plan reschedules its nudges.
    var reminders = createReminders(settings);

    /* --- Focus sessions (P9-07): the Now timer. Entered by the `focus`
       intent, the day-view "Start now", or the quiet start-time hint; the
       eyes settle into the focused ambient while it runs. On record, the
       emotions follow the REAL numbers (truthfulness rule): satisfied slow
       blink always; proud only when measured <= estimate; worried only on
       a real overrun of the planned span. --- */
    var nowCtl = createNow(appEl, agent, hint, {
      onEnter: function () {
        dismissSurface();
        stage.close();
      },
      onRecorded: function (res, block) {
        var total = res.total_minutes || 0;
        var planned = res.planned_minutes || 0;
        var estimate = (block && block.estimate_minutes != null)
          ? block.estimate_minutes : planned;
        eyes.emote("satisfied");             // one deliberate slow blink
        setTimeout(function () {
          if (planned > 0 && total > planned) eyes.emote("worried", 1800);
          else if (estimate > 0 && total <= estimate) eyes.emote("proud", 1600);
        }, 1100);
        var line = "Recorded " + total + (total === 1 ? " minute" : " minutes")
          + " on " + ((block && block.title) || "that session") + ".";
        if (res.block_status === "partial") line += " Marked partial for now.";
        history.push({ role: "assistant", content: line });
        deliverReply(line, onSpoken);
      },
    });
    window.FocusNow = nowCtl;   // sanctioned bridge: the day-view popover's
                                // "Start now" reaches the timer through it
    nowCtl.restore();           // a reload mid-session resumes (paused + asked)
    nowCtl.armOffers();

    // Let the settings panel (Google Calendar sync) re-pull the week after a
    // sync — re-renders the horizon content whenever it's open (P7-05).
    window.FocusRefresh = function () {
      try { stage.refresh(); } catch (_) {}
      try { reminders.arm(); } catch (_) {}
      try { nowCtl.armOffers(); } catch (_) {}
    };

    /* --- Spoken morning brief (P9-03c): app open before 10am, once a day.
       Real counts from the EXISTING morning-brief trigger; zero sessions
       means zero words (silence is a first-class output). Runs only for a
       signed-in (or guest-fallback) session — openApp() calls it. --- */
    function scheduleMorningBrief() {
      var KEY = "focus.morningBrief";
      if (new Date().getHours() >= 10) return;
      try { if (localStorage.getItem(KEY) === localTodayKey()) return; } catch (_) {}
      try { localStorage.setItem(KEY, localTodayKey()); } catch (_) {}
      setTimeout(function () {
        if (busy || agent.get() !== "idle") return;   // never talk over the user
        api("/trigger", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ trigger: "morning_brief" }),
        }).then(function (r) {
          var brief = r && r.brief;
          if (!brief || !brief.blocks_today) return;  // nothing planned: say nothing
          if (busy || agent.get() !== "idle") return;
          var n = brief.blocks_today;
          var line = "Morning. " + n + (n === 1 ? " session" : " sessions") + " today";
          if (brief.first_start) line += ", first at " + fmtClock(brief.first_start);
          line += ".";
          history.push({ role: "assistant", content: line });
          deliverReply(line, function () {
            onSpoken();
            // P9-09: the brief may carry one mined insight; it surfaces
            // only when the brief itself spoke (silence stays silent).
            if (brief.insight) askInsight(brief.insight);
          });
        }).catch(function () { /* quiet — the brief is a nicety */ });
      }, 1400);
    }

    /* --- Evening check-in affordance (P9-03a): after 5pm, once a day, a
       quiet hint — only when today actually has ended, unresolved blocks.
       Never a nag: no popups, no repeats, silence when there is nothing
       to reconcile. --- */
    function scheduleEveningNudge() {
      var KEY = "focus.checkinHint";
      if (new Date().getHours() < 17) return;
      try { if (localStorage.getItem(KEY) === localTodayKey()) return; } catch (_) {}
      setTimeout(function () {
        api("/details").then(function (d) {
          var tk = localTodayKey();
          var now = new Date();
          var pending = (d.blocks || []).some(function (b) {
            if (b.status !== "planned") return false;
            if ((b.starts_at || "").slice(0, 10) !== tk) return false;
            var end = new Date(b.ends_at);
            return !isNaN(end) && end <= now;
          });
          if (!pending) return;                       // nothing to check off
          try { localStorage.setItem(KEY, localTodayKey()); } catch (_) {}
          if (agent.get() === "idle") {
            hint.set('Evening. Ask me "how did today go" when you\'re ready.');
          }
        }).catch(function () { /* quiet */ });
      }, 2200);
    }

    /* --- The auth gate (2026-08-30). ------------------------------------
       The whole interactive surface waits behind Google sign-in. `openApp`
       unlocks it (a real session, a ?ws= demo override, or a server with
       sign-in disabled, where guest access is the only sane fallback);
       `showWall` reveals the sign-in wall and keeps the input locked. The
       eyes are alive in every state. `authGate` reads /v1/session ONCE and
       decides, folding in the old sessionBoot corrections (a dead binding
       resets to guest; a signed-in cookie adopts its own workspace) and the
       greeting. */
    function openApp(s, cameBack) {
      interactionLocked = false;
      appEl.setAttribute("data-auth", "in");
      // The greeting: one warm line built server-side from the STORED name,
      // on the sign-in return and at most once a day otherwise. Never invented.
      if (s && s.greeting) {
        var GREET_KEY = "focus.greetedOn";
        var greetedToday = false;
        try { greetedToday = localStorage.getItem(GREET_KEY) === localTodayKey(); } catch (_) {}
        if (cameBack || !greetedToday) {
          try { localStorage.setItem(GREET_KEY, localTodayKey()); } catch (_) {}
          setTimeout(function () {
            if (busy || agent.get() !== "idle") return;   // never talk over the user
            startTurn();
            history.push({ role: "assistant", content: s.greeting });
            deliverReply(s.greeting, onSpoken);
          }, cameBack ? 700 : 900);
        }
      }
      scheduleMorningBrief();
      scheduleEveningNudge();
      // First-run gate (P9-08): a beat after load, a brand-new workspace gets
      // the interview instead of the idle eyes.
      setTimeout(runOnboarding, 1200);
    }

    function showWall() {
      interactionLocked = true;
      appEl.setAttribute("data-auth", "out");
      var btn = document.getElementById("aw-signin");
      var note = document.getElementById("aw-note");
      if (!btn) return;
      var going = false;
      btn.addEventListener("click", function () {
        if (going) return;
        going = true;
        if (note) { note.textContent = ""; note.classList.remove("show"); }
        api("/auth/signin").then(function (r) {
          if (r && r.auth_url) { window.location.href = r.auth_url; return; }
          throw new Error("no auth url");
        }).catch(function () {
          going = false;
          if (note) {
            note.textContent = "Sign-in isn’t available right now. Try again in a moment.";
            note.classList.add("show");
          }
        });
      });
    }

    function authGate() {
      // A ?ws= demo override is a look at a specific workspace, not a home —
      // it bypasses the wall so tests and demo links keep working.
      if (WS_FROM_QUERY) { openApp(null, false); return; }
      var cameBack = window.__focusSigninFlash === "connected";
      window.__focusSigninFlash = null;
      fetch("/v1/session").then(function (r) { return r.json(); }).then(function (s) {
        var signedIn = !!(s && s.signed_in);
        if (signedIn) {
          if (s.workspace_id && s.workspace_id !== WS) {
            // The cookie knows this browser's account; adopt its workspace.
            try { localStorage.setItem("focus.workspace", s.workspace_id); } catch (_) {}
            window.location.reload();
            return;
          }
          openApp(s, cameBack);
          return;
        }
        // Not signed in. A stale signed-in binding (u_ workspace, cookie gone)
        // resets to a fresh guest first, then that guest meets the wall.
        if (WS.indexOf("u_") === 0) {
          try { localStorage.removeItem("focus.workspace"); } catch (_) {}
          window.location.reload();
          return;
        }
        // Wall the surface — UNLESS the server has sign-in disabled, where a
        // wall would be a door nobody can open. There, guest access stands.
        if (s && s.signin_enabled === false) { openApp(null, false); return; }
        showWall();
      }).catch(function () {
        // The session couldn't be read. Don't brick the app on a blip: fall
        // back to guest access, which is exactly what the app did before the
        // gate existed.
        openApp(null, false);
      });
    }

    // Demo-rehearsal hook (P7-03, deliberate): trigger any emotion from the
    // console — window.__emote("happy" | "wide" | "sorry" | "curious" |
    // "satisfied" | "heart" | "surprised" | "sleepy" | "proud" | "sheepish" |
    // "worried" | "celebrate", holdMs).
    window.__emote = eyes.emote;

    // Prime state once on load (empty workspace is fine). P13: when the
    // server still holds the thread from before a reload and this page
    // hasn't spoken yet, adopt it as the local history so the model and the
    // UI agree on what was already said. Memory only, never a transcript UI.
    api("/details").then(function (d) {
      var thread = (d && d.conversation) || [];
      if (history.length !== 0 || !thread.length) return;
      thread.forEach(function (m) {
        if (m && m.content && (m.role === "user" || m.role === "assistant")) {
          history.push({ role: m.role, content: m.content });
        }
      });
    }).catch(function () { /* stay calm; schedule view handles retry */ });

    agent.set("idle");

    // The auth gate decides everything interactive: it either unlocks the app
    // (openApp — greeting, brief, onboarding) or raises the sign-in wall. Runs
    // last so every component above already exists for openApp to drive.
    authGate();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main);
  } else {
    main();
  }
})();
