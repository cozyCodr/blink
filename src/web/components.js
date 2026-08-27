/* =====================================================================
   Focus Agent — Response-component library (P3-02)

   The kit the agent draws from to ask anything without ever feeling like
   a form. Every ClarifyQuestion the backend emits carries an `input_type`;
   this file renders the matching control, shows a live readout, and emits
   a typed value through onAnswer(typedValue).

   Design: ported 1:1 from the focus-design study into the Nocturne token
   system (see css/tokens.css — the only palette, P10-00). Components inherit
   the app tokens, so no per-component color code lives here.

   Structure: one small factory per input_type, each vanilla and framework-
   free, plus a dispatcher `renderClarifyQuestion(container, question, onAnswer)`.
   Every factory carries a `// -> future React <Component/>` mapping comment so
   the port later is mechanical.

   The ClarifyQuestion shape this file reads:
     { question, field, why, input_type, allow_free_text,
       options: [{label, value, opens_free_text}],   // *_select, duration, time_bucket
       config:  { min, max, step, unit } }            // duration(_range), number, scale

   Emission model: each component emits its CURRENT typed value through
   onAnswer whenever the user changes it (a chip click, a stepper tick, a
   keystroke). For decisive single-shot controls (single_select, scale,
   confirm) that first click IS the commit; for building controls
   (ranges, recurrence, free_text) the latest emit is the running value.
   P3-04 owns the surrounding answer loop; here we just render + emit.

   Value shapes, by input_type:
     free_text      -> string
     single_select  -> the chosen option's value
     multi_select   -> array of chosen option values
     scale_1_5      -> integer 1..5
     duration       -> minutes (int)
     duration_range -> { min, max }   (minutes, min<=max enforced)
     time_bucket    -> "morning"|"afternoon"|"evening"  OR  "HH:MM"
     time_range     -> { from:"HH:MM", to:"HH:MM" }      (to>from enforced)
     date           -> "YYYY-MM-DD"
     date_range     -> { from:"YYYY-MM-DD", to:"YYYY-MM-DD" }
     recurrence     -> { days:["Mon","Wed"], time:"HH:MM" }
     number         -> integer
     confirm        -> boolean
     courses        -> { use: boolean, courses: [picked candidates] }  (P9-04)
   ===================================================================== */
(function () {
  "use strict";

  /* ---------- tiny DOM helpers ---------- */
  function el(tag, cls, attrs) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "text") n.textContent = attrs[k];
        else if (k === "html") n.innerHTML = attrs[k];
        else n.setAttribute(k, attrs[k]);
      });
    }
    return n;
  }
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // Duration formatter shared across duration components: 75 -> "1h 15m".
  function fmtDur(m) {
    m = Math.max(0, Math.round(m));
    var h = Math.floor(m / 60), mm = m % 60;
    if (h && mm) return h + "h " + mm + "m";
    if (h) return h + "h 0m";
    return mm + "m";
  }
  // Minutes between two "HH:MM" strings (b - a), or NaN.
  function minutesBetween(a, b) {
    if (!a || !b) return NaN;
    return (parseInt(b, 10) * 60 + parseInt(b.slice(3), 10)) -
           (parseInt(a, 10) * 60 + parseInt(a.slice(3), 10));
  }

  // A dashed live readout row, matching the design study. Returns the node
  // plus a set(html) updater. Each component owns one.
  function readout(initialHtml) {
    var r = el("div", "clarify-readout");
    r.innerHTML = initialHtml || "";
    return { node: r, set: function (h) { r.innerHTML = h; } };
  }

  /* =====================================================================
     free_text -> string
     -> future React <FreeText/>
     ===================================================================== */
  function freeText(q, onAnswer) {
    var wrap = el("div", "clarify-control");
    var input = el("textarea", "field", {
      rows: "2",
      placeholder: (q.options && q.options[0] && q.options[0].label) || "Type your answer…",
      autocomplete: "off",
    });
    var out = readout("Open answers: titles, notes, anything unenumerable.");
    input.addEventListener("input", function () {
      var v = input.value.trim();
      out.set(v ? 'Saved as <b>"' + esc(v) + '"</b>' : "Open answers: titles, notes, anything unenumerable.");
      onAnswer(input.value);
    });
    wrap.appendChild(input);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     single_select -> the chosen option's value
     An option with opens_free_text reveals a text field and emits its string.
     -> future React <SingleSelect/>
     ===================================================================== */
  function singleSelect(q, onAnswer) {
    var wrap = el("div", "clarify-control");
    var chips = el("div", "chips");
    var out = readout("Pick one. Mutually exclusive buckets.");
    var reveal = el("div", "reveal");
    var free = el("input", "field", { type: "text", placeholder: "Tell me more…", autocomplete: "off" });
    reveal.appendChild(free);

    (q.options || []).forEach(function (opt) {
      var chip = el("span", "chip", { text: opt.label, tabindex: "0", role: "button" });
      function choose() {
        chips.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("on"); });
        chip.classList.add("on");
        if (opt.opens_free_text) {
          reveal.classList.add("show");
          out.set("Chosen: <b>" + esc(opt.label) + "</b>. Add a little detail below.");
          onAnswer(free.value.trim());
          setTimeout(function () { free.focus(); }, 40);
        } else {
          reveal.classList.remove("show");
          out.set("Chosen: <b>" + esc(opt.label) + "</b>");
          onAnswer(opt.value);
        }
      }
      chip.addEventListener("click", choose);
      chip.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); }
      });
      chips.appendChild(chip);
    });
    free.addEventListener("input", function () {
      out.set('Chosen: <b>"' + esc(free.value.trim()) + '"</b>');
      onAnswer(free.value);
    });

    wrap.appendChild(chips);
    wrap.appendChild(reveal);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     multi_select -> array of chosen option values
     An option with opens_free_text toggles a text field alongside the chips.
     -> future React <MultiSelect/>
     ===================================================================== */
  function multiSelect(q, onAnswer) {
    var wrap = el("div", "clarify-control");
    var chips = el("div", "chips");
    var out = readout("Choose any that apply.");
    var reveal = el("div", "reveal");
    var free = el("input", "field", { type: "text", placeholder: "Something else…", autocomplete: "off" });
    reveal.appendChild(free);
    var freeChip = null;

    function emit() {
      var vals = [].slice.call(chips.querySelectorAll(".chip.on")).map(function (c) {
        return c._optHasFree ? free.value.trim() : c._value;
      }).filter(function (v) { return v !== "" && v !== undefined; });
      var labels = [].slice.call(chips.querySelectorAll(".chip.on")).map(function (c) { return c._label; });
      out.set(labels.length ? "Selected: <b>" + esc(labels.join(", ")) + "</b>" : "Choose any that apply.");
      onAnswer(vals);
    }

    (q.options || []).forEach(function (opt) {
      var chip = el("span", "chip", { tabindex: "0", role: "button" });
      chip.innerHTML = '<span class="chk">✓</span>' + esc(opt.label);
      chip._value = opt.value;
      chip._label = opt.label;
      chip._optHasFree = !!opt.opens_free_text;
      if (opt.opens_free_text) freeChip = chip;
      function toggle() {
        chip.classList.toggle("on");
        if (opt.opens_free_text) {
          reveal.classList.toggle("show", chip.classList.contains("on"));
          if (chip.classList.contains("on")) setTimeout(function () { free.focus(); }, 40);
        }
        emit();
      }
      chip.addEventListener("click", toggle);
      chip.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
      chips.appendChild(chip);
    });
    free.addEventListener("input", function () { if (freeChip && freeChip.classList.contains("on")) emit(); });

    wrap.appendChild(chips);
    wrap.appendChild(reveal);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     scale_1_5 -> integer 1..5
     -> future React <Scale/>
     ===================================================================== */
  function scale15(q, onAnswer) {
    var labels = ["", "idle", "minor", "steady", "high", "critical"];
    var wrap = el("div", "clarify-control");
    var row = el("div", "scale");
    var out = readout("Pick a level from 1 to 5.");
    for (var i = 1; i <= 5; i++) {
      (function (n) {
        var dot = el("span", "dot", { text: String(n), tabindex: "0", role: "button", "aria-label": "Level " + n });
        function pick() {
          row.querySelectorAll(".dot").forEach(function (d, j) { d.classList.toggle("on", j < n); });
          out.set("Level <b>" + n + "</b> · " + labels[n] + ".");
          onAnswer(n);
        }
        dot.addEventListener("click", pick);
        dot.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
        });
        row.appendChild(dot);
      })(i);
    }
    wrap.appendChild(row);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* ---------- shared stepper widget (used by duration + duration_range) ---------- */
  // Returns { node, get, set, onChange }. Value is an integer (minutes here).
  function stepper(opts) {
    var step = opts.step || 15, min = (opts.min != null ? opts.min : 0);
    var max = (opts.max != null ? opts.max : Infinity);
    var val = (opts.value != null ? opts.value : min);
    var fmt = opts.fmt || function (v) { return String(v); };
    var listeners = [];
    var node = el("div", "stepper");
    var dec = el("button", null, { type: "button", "aria-label": "less", "data-dec": "" });
    dec.textContent = "−";
    var valEl = el("span", "val");
    var inc = el("button", null, { type: "button", "aria-label": "more", "data-inc": "" });
    inc.textContent = "+";
    function render() { valEl.textContent = fmt(val); }
    function fire() { listeners.forEach(function (fn) { fn(val); }); }
    function setVal(v) { val = Math.min(max, Math.max(min, v)); render(); }
    dec.addEventListener("click", function () { setVal(val - step); fire(); });
    inc.addEventListener("click", function () { setVal(val + step); fire(); });
    node.appendChild(dec); node.appendChild(valEl); node.appendChild(inc);
    render();
    return {
      node: node,
      get: function () { return val; },
      set: function (v) { setVal(v); },       // silent set (no fire) — used by chips that also fire once
      onChange: function (fn) { listeners.push(fn); },
    };
  }

  /* =====================================================================
     duration -> minutes (int).  Quick chips + a stepper (config.step) + Other.
     -> future React <Duration/>
     ===================================================================== */
  function duration(q, onAnswer) {
    var cfg = q.config || {};
    var wrap = el("div", "clarify-control");
    var chips = el("div", "chips");
    var out = readout("Anchors a fuzzy estimate. Quick chips, or the stepper for precision.");
    var step = stepper({
      step: cfg.step || 15, min: cfg.min != null ? cfg.min : 15,
      max: cfg.max != null ? cfg.max : 480,
      value: cfg.min != null ? Math.max(cfg.min, 60) : 60, fmt: fmtDur,
    });

    (q.options || []).forEach(function (opt) {
      var chip = el("span", "chip", { text: opt.label, tabindex: "0", role: "button" });
      function choose() {
        chips.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("on"); });
        chip.classList.add("on");
        var mins = Number(opt.value);
        step.set(mins);
        out.set("Set to <b>" + fmtDur(mins) + "</b>.");
        onAnswer(mins);
      }
      chip.addEventListener("click", choose);
      chip.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); }
      });
      chips.appendChild(chip);
    });

    var exactRow = el("div", "rowflex");
    exactRow.appendChild(el("span", "conj", { text: "or set it exactly" }));
    exactRow.appendChild(step.node);
    step.onChange(function (v) {
      chips.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("on"); });
      out.set("Set to <b>" + fmtDur(v) + "</b>.");
      onAnswer(v);
    });

    wrap.appendChild(chips);
    wrap.appendChild(exactRow);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     duration_range -> { min, max } minutes (two steppers; min<=max enforced)
     -> future React <DurationRange/>
     ===================================================================== */
  function durationRange(q, onAnswer) {
    var cfg = q.config || {};
    var wrap = el("div", "clarify-control");
    var lo = stepper({ step: cfg.step || 15, min: cfg.min != null ? cfg.min : 0, max: cfg.max != null ? cfg.max : 480, value: 120, fmt: fmtDur });
    var hi = stepper({ step: cfg.step || 15, min: cfg.min != null ? cfg.min : 0, max: cfg.max != null ? cfg.max : 480, value: 240, fmt: fmtDur });
    var out = readout("<b>Between 2h and 4h.</b> The scheduler treats it as a window, not a promise.");

    var row = el("div", "rowflex");
    row.appendChild(lo.node);
    row.appendChild(el("span", "conj", { text: "to" }));
    row.appendChild(hi.node);

    function emit() {
      var a = lo.get(), b = hi.get();
      if (a > b) {
        out.set("<b>Give me a real window.</b> The low end is above the high end right now.");
      } else {
        out.set("<b>Between " + fmtDur(a) + " and " + fmtDur(b) + ".</b> The scheduler treats it as a window, not a promise.");
      }
      // Enforce min<=max in the emitted value regardless of transient UI state.
      onAnswer({ min: Math.min(a, b), max: Math.max(a, b) });
    }
    lo.onChange(emit); hi.onChange(emit);

    wrap.appendChild(row);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     time_bucket -> "morning"|"afternoon"|"evening"  OR  exact "HH:MM"
     -> future React <TimeBucket/>
     ===================================================================== */
  function timeBucket(q, onAnswer) {
    var wrap = el("div", "clarify-control");
    var chips = el("div", "chips");
    var out = readout('Coarse buckets keep it low-effort. "Pick a time" opens the exact field.');
    var reveal = el("div", "reveal");
    reveal.appendChild(el("span", "conj", { text: "at" }));
    var time = el("input", "nat", { type: "time", value: "15:30" });
    reveal.appendChild(time);

    // Prefer options if provided; otherwise the three canonical buckets.
    var buckets = (q.options && q.options.length)
      ? q.options.filter(function (o) { return !o.opens_free_text; })
      : [{ label: "Morning", value: "morning" }, { label: "Afternoon", value: "afternoon" }, { label: "Evening", value: "evening" }];

    buckets.forEach(function (opt) {
      var chip = el("span", "chip", { text: opt.label, tabindex: "0", role: "button" });
      function choose() {
        chips.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("on"); });
        chip.classList.add("on");
        reveal.classList.remove("show");
        out.set("Chosen: <b>" + esc(opt.label) + "</b>");
        onAnswer(opt.value);
      }
      chip.addEventListener("click", choose);
      chip.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); } });
      chips.appendChild(chip);
    });

    var pick = el("span", "chip", { text: "Pick a time…", tabindex: "0", role: "button" });
    function pickExact() {
      chips.querySelectorAll(".chip").forEach(function (c) { c.classList.remove("on"); });
      pick.classList.add("on");
      reveal.classList.add("show");
      out.set("Exact time: <b>" + time.value + "</b>");
      onAnswer(time.value);
    }
    pick.addEventListener("click", pickExact);
    pick.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickExact(); } });
    chips.appendChild(pick);
    time.addEventListener("input", function () {
      if (pick.classList.contains("on")) { out.set("Exact time: <b>" + time.value + "</b>"); onAnswer(time.value); }
    });

    wrap.appendChild(chips);
    wrap.appendChild(reveal);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     time_range -> { from:"HH:MM", to:"HH:MM" } (to>from enforced in readout)
     -> future React <TimeRange/>
     ===================================================================== */
  function timeRange(q, onAnswer) {
    var cfg = q.config || {};
    var wrap = el("div", "clarify-control");
    // config {from, to} seeds the defaults (P9-08: the sleep question opens
    // on 23:00-07:00); config.allow_overnight makes an end before the start
    // read as a window crossing midnight instead of an error.
    var from = el("input", "nat", { type: "time", value: cfg.from || "09:00" });
    var to = el("input", "nat", { type: "time", value: cfg.to || "17:00" });
    var out = readout("");
    var row = el("div", "rowflex");
    row.appendChild(from);
    row.appendChild(el("span", "conj", { text: "to" }));
    row.appendChild(to);

    function emit() {
      var d = minutesBetween(from.value, to.value);
      if (!isNaN(d) && d < 0 && cfg.allow_overnight) d += 1440;
      if (isNaN(d)) { out.set("Set a start and an end."); }
      else if (d <= 0) { out.set("<b>" + from.value + " to " + to.value + "</b> · end has to come after start."); }
      else if (cfg.allow_overnight && to.value <= from.value) {
        var oh = d / 60;
        var ohTxt = d % 60 ? oh.toFixed(1) : oh.toFixed(0);
        var art = /^(8|11|18)(\.|$)/.test(ohTxt) ? "an" : "a";
        out.set("<b>" + from.value + " to " + to.value + "</b> · " + art + " " + ohTxt + " hour window across midnight.");
        onAnswer({ from: from.value, to: to.value });
        return;
      }
      else {
        var h = d / 60;
        out.set("<b>" + from.value + " to " + to.value + "</b> · a " + (d % 60 ? h.toFixed(1) : h.toFixed(0)) + " hour window the agent plans around.");
      }
      onAnswer({ from: from.value, to: to.value });
    }
    from.addEventListener("input", emit);
    to.addEventListener("input", emit);
    emit();

    wrap.appendChild(row);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     date -> "YYYY-MM-DD"
     -> future React <DateField/>
     ===================================================================== */
  function dateField(q, onAnswer) {
    var wrap = el("div", "clarify-control");
    var input = el("input", "nat", { type: "date" });
    var out = readout("Pick a date.");
    input.addEventListener("input", function () {
      if (!input.value) { out.set("Pick a date."); onAnswer(""); return; }
      var days = Math.ceil((new Date(input.value) - new Date(new Date().toDateString())) / 86400000);
      out.set(isNaN(days) ? "Pick a date." : "<b>" + input.value + "</b> · " + days + " day" + (Math.abs(days) === 1 ? "" : "s") + " from today.");
      onAnswer(input.value);
    });
    wrap.appendChild(input);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     date_range -> { from, to }
     -> future React <DateRange/>
     ===================================================================== */
  function dateRange(q, onAnswer) {
    var wrap = el("div", "clarify-control");
    var from = el("input", "nat", { type: "date" });
    var to = el("input", "nat", { type: "date" });
    var out = readout("Pick a start and an end.");
    var row = el("div", "rowflex");
    row.appendChild(from);
    row.appendChild(el("span", "conj", { text: "to" }));
    row.appendChild(to);
    function emit() {
      if (from.value && to.value) {
        var days = Math.round((new Date(to.value) - new Date(from.value)) / 86400000) + 1;
        out.set(days > 0 ? "<b>" + days + " day" + (days === 1 ? "" : "s") + "</b> · " + from.value + " → " + to.value + "." : "End has to come after start.");
      } else {
        out.set("Pick a start and an end.");
      }
      onAnswer({ from: from.value, to: to.value });
    }
    from.addEventListener("input", emit);
    to.addEventListener("input", emit);
    wrap.appendChild(row);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     recurrence -> { days:["Mon","Wed"], time:"HH:MM" }
     -> future React <Recurrence/>
     ===================================================================== */
  function recurrence(q, onAnswer) {
    var DAYS = [["Mon", "M"], ["Tue", "T"], ["Wed", "W"], ["Thu", "T"], ["Fri", "F"], ["Sat", "S"], ["Sun", "S"]];
    var wrap = el("div", "clarify-control");
    var row = el("div", "rowflex");
    var week = el("div", "week");
    var out = readout("Turns a fuzzy habit into a real constraint the schedule respects.");
    var time = el("input", "nat", { type: "time", value: "06:30" });

    function emit() {
      var days = [].slice.call(week.querySelectorAll(".day.on")).map(function (d) { return d.getAttribute("data-d"); });
      out.set(days.length
        ? "<b>" + days.join(", ") + " at " + time.value + "</b> · a standing constraint."
        : "Turns a fuzzy habit into a real constraint the schedule respects.");
      onAnswer({ days: days, time: time.value });
    }
    DAYS.forEach(function (d) {
      var b = el("button", "day", { type: "button", "data-d": d[0], "aria-label": d[0], text: d[1] });
      b.addEventListener("click", function () { b.classList.toggle("on"); emit(); });
      week.appendChild(b);
    });
    time.addEventListener("input", emit);
    row.appendChild(week);
    row.appendChild(el("span", "conj", { text: "at" }));
    row.appendChild(time);
    wrap.appendChild(row);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     number -> int (slider, config.min/max/step)
     -> future React <NumberSlider/>
     ===================================================================== */
  function number(q, onAnswer) {
    var cfg = q.config || {};
    var min = cfg.min != null ? cfg.min : 1, max = cfg.max != null ? cfg.max : 25, step = cfg.step || 1;
    var start = cfg.value != null ? cfg.value : Math.round((min + max) / 5);
    var unit = cfg.unit || "";
    var wrap = el("div", "clarify-control");
    var range = el("input", null, { type: "range", min: String(min), max: String(max), step: String(step), value: String(start) });
    var out = readout("");
    function emit() {
      var v = parseInt(range.value, 10);
      out.set("<b>" + v + (unit ? " " + unit : "") + ".</b>");
      onAnswer(v);
    }
    range.addEventListener("input", emit);
    emit();
    wrap.appendChild(range);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     confirm -> boolean (yes / not-now)
     -> future React <Confirm/>
     ===================================================================== */
  function confirm(q, onAnswer) {
    var wrap = el("div", "clarify-control");
    var row = el("div", "confirm");
    var yesLabel = (q.options && q.options[0] && q.options[0].label) || "Yes";
    var noLabel = (q.options && q.options[1] && q.options[1].label) || "Not now";
    var yes = el("button", "btn go", { type: "button", text: yesLabel });
    var no = el("button", "btn ghost", { type: "button", text: noLabel });
    var out = readout("Only asked when a wrong guess is expensive.");
    yes.addEventListener("click", function () { out.set("<b>" + esc(yesLabel) + ".</b>"); onAnswer(true); });
    no.addEventListener("click", function () { out.set("<b>" + esc(noLabel) + ".</b>"); onAnswer(false); });
    row.appendChild(yes);
    row.appendChild(no);
    wrap.appendChild(row);
    wrap.appendChild(out.node);
    return wrap;
  }

  /* =====================================================================
     courses -> { use: boolean, courses: [picked candidate objects] }
     P9-04 "Blink found these": search-grounded course cards with
     pick-to-include checkboxes. Decisive control like confirm: the
     "Use these" / "Skip" click IS the commit (no separate Send).
     Card data rides in q.courses: [{title, provider, url, description,
     citation}]. Links open in a new tab; card text is DATA from a live
     search, rendered via textContent only (never innerHTML).
     -> future React <CourseCards/>
     ===================================================================== */
  function courseCards(q, onAnswer) {
    var courses = q.courses || [];
    var wrap = el("div", "clarify-control");
    var label = el("div", "courses-label", { text: "Blink found these" });
    var list = el("div", "courses");
    var out = readout("Real courses from a live search. Untick any you don't want.");

    function pickedNow() {
      return [].slice.call(list.querySelectorAll("input[type=checkbox]"))
        .filter(function (c) { return c.checked; })
        .map(function (c) { return courses[Number(c.getAttribute("data-i"))]; })
        .filter(Boolean);
    }
    function refresh() {
      var n = pickedNow().length;
      out.set(n
        ? "<b>" + n + " of " + courses.length + "</b> picked to build around."
        : "None picked. Use these commits your picks; Skip plans without them.");
      use.disabled = n === 0;
    }

    courses.forEach(function (c, i) {
      c = c || {};
      var card = el("label", "course-card");
      var check = el("input", null, {
        type: "checkbox", "data-i": String(i),
        "aria-label": "Include " + (c.title || "this course"),
      });
      check.checked = true;                     // found for you; untick to drop
      check.addEventListener("change", refresh);

      var body = el("div", "course-body");
      var head = el("div", "course-head");
      head.appendChild(el("span", "course-title", { text: c.title || "Untitled course" }));
      if (c.provider) head.appendChild(el("span", "course-provider", { text: c.provider }));
      body.appendChild(head);
      if (c.description) body.appendChild(el("p", "course-desc", { text: c.description }));

      var cite = el("div", "course-cite");
      cite.appendChild(el("span", null, { text: "via " + (c.citation || "search") }));
      var url = String(c.url || "");
      if (url.indexOf("http://") === 0 || url.indexOf("https://") === 0) {
        var link = el("a", "course-link", {
          href: url, target: "_blank", rel: "noopener noreferrer", text: "Open ↗",
        });
        // A link click reads the page, it never toggles the pick.
        link.addEventListener("click", function (e) { e.stopPropagation(); });
        cite.appendChild(link);
      }
      body.appendChild(cite);

      card.appendChild(check);
      card.appendChild(body);
      list.appendChild(card);
    });

    var row = el("div", "confirm");
    var use = el("button", "btn go", { type: "button", text: "Use these" });
    var skip = el("button", "btn ghost", { type: "button", text: "Skip" });
    use.addEventListener("click", function () {
      var picked = pickedNow();
      if (!picked.length) return;
      onAnswer({ use: true, courses: picked });
    });
    skip.addEventListener("click", function () { onAnswer({ use: false, courses: [] }); });
    row.appendChild(use);
    row.appendChild(skip);

    wrap.appendChild(label);
    wrap.appendChild(list);
    wrap.appendChild(row);
    wrap.appendChild(out.node);
    refresh();
    return wrap;
  }

  /* ---------- registry: input_type -> factory ---------- */
  var FACTORIES = {
    free_text: freeText,
    single_select: singleSelect,
    multi_select: multiSelect,
    scale_1_5: scale15,
    duration: duration,
    duration_range: durationRange,
    time_bucket: timeBucket,
    time_range: timeRange,
    date: dateField,
    date_range: dateRange,
    recurrence: recurrence,
    number: number,
    confirm: confirm,
    courses: courseCards,
  };

  /* =====================================================================
     Dispatcher — renderClarifyQuestion(container, question, onAnswer)
     Renders the question text (serif) + optional why (faint) + the control
     for question.input_type into `container`, and calls onAnswer(typedValue)
     as the user answers. Returns the rendered wrapper element.
     -> future React <ClarifyQuestion question={...} onAnswer={...} />
     ===================================================================== */
  function renderClarifyQuestion(container, question, onAnswer) {
    question = question || {};
    onAnswer = onAnswer || function () {};
    var factory = FACTORIES[question.input_type];

    var wrap = el("div", "clarify");
    if (question.question) wrap.appendChild(el("p", "clarify-ask", { text: question.question }));
    if (question.why) wrap.appendChild(el("p", "clarify-why", { text: question.why }));

    if (!factory) {
      // Unknown input_type — degrade to free text so the agent is never stuck.
      wrap.appendChild(freeText(question, onAnswer));
    } else {
      wrap.appendChild(factory(question, onAnswer));
    }

    if (container) container.appendChild(wrap);
    return wrap;
  }

  /* ---------- public surface ---------- */
  window.FocusComponents = {
    renderClarifyQuestion: renderClarifyQuestion,
    factories: FACTORIES,          // input_type -> factory(question, onAnswer)
    fmtDur: fmtDur,                // exported for gallery/readout reuse
  };
})();
