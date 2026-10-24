/* EchoGuard sitting / standing tracker UI.
 * Vanilla JS, no external resources (CSP: script-src 'self'). Polls the local
 * JSON API of dashboard/server.py. Pages are switched with the URL hash.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  const PAGES = ["today", "history", "health", "share", "calibrate", "sensor"];
  const STATE_LABEL = { sitting: "Sitting", standing: "Standing", moving: "Moving", away: "Away", unknown: "Unknown" };
  const COLORS = { sitting: "#b45309", upright: "#047857", accent: "#1f5fbf", grid: "#d9d9d4", text: "#5a5a5a" };

  function text(id, value) {
    const el = $(id);
    if (el && el.textContent !== String(value)) el.textContent = String(value);
  }
  function show(id, visible) {
    const el = $(id);
    if (el) el.hidden = !visible;
  }
  function el(tag, props, children) {
    const e = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (k === "text") e.textContent = v;
        else if (k === "className") e.className = v;
        else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v);
      }
    }
    (children || []).forEach((c) => e.appendChild(typeof c === "string" ? document.createTextNode(c) : c));
    return e;
  }
  function fmtDur(seconds) {
    if (seconds == null || !isFinite(seconds)) return "-";
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return s + " s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " min";
    const h = Math.floor(m / 60);
    return h + " h " + String(m % 60).padStart(2, "0") + " min";
  }
  const fmtMin = (min) => (min == null ? "-" : fmtDur(min * 60));
  function clock(seconds) {
    const s = Math.max(0, Math.round(seconds));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }
  function lengthText(seconds) {
    const s = Math.round(seconds);
    if (s < 60) return s + " s";
    return Math.floor(s / 60) + " min" + (s % 60 ? " " + (s % 60) + " s" : "");
  }
  function isoDate(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      show("server-error", true);
      throw new Error("cannot reach the backend");
    }
    show("server-error", false);
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) throw new Error((data && data.error) || ("HTTP " + resp.status));
    return data;
  }

  // --------------------------------------------------------------- audio
  let audioCtx = null;
  function unlockAudio() {
    try {
      if (!audioCtx) {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx) audioCtx = new Ctx();
      }
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    } catch (e) { audioCtx = null; }
  }
  function beep(freq, ms, times) {
    if (!audioCtx) return;
    const n = times || 1;
    for (let i = 0; i < n; i++) {
      const t0 = audioCtx.currentTime + i * (ms / 1000 + 0.12);
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq || 880;
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.3, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + ms / 1000);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t0);
      osc.stop(t0 + ms / 1000 + 0.05);
    }
  }
  function speak(phrase) {
    if (!phrase || !$("cal-speak").checked || !("speechSynthesis" in window)) return;
    try {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(phrase);
      u.rate = 1.0;
      window.speechSynthesis.speak(u);
    } catch (e) { /* speech unavailable */ }
  }

  // ------------------------------------------------------------- routing
  let page = "today";
  function route() {
    const want = (location.hash || "#today").slice(1);
    page = PAGES.includes(want) ? want : "today";
    PAGES.forEach((p) => show("page-" + p, p === page));
    document.querySelectorAll("[data-nav]").forEach((a) => {
      if (a.dataset.nav === page) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    refreshPage();
  }
  function refreshPage() {
    if (page === "today") loadToday();
    else if (page === "history") loadHistory();
    else if (page === "health") loadHealth();
    else if (page === "share") loadSummary();
    else if (page === "calibrate") { loadCalibrations(); pollCalibration(); }
    else if (page === "sensor") { loadSources(); pollSeries(); }
  }

  // ------------------------------------------------------- status/banners
  let status = null;
  async function pollStatus() {
    try { status = await api("/api/status"); } catch (e) { return; }
    const src = status.source || {};
    const kind = src.kind;
    let pill = "No data source";
    if (kind === "simulation") pill = "Simulation (synthetic)";
    else if (kind === "replay") pill = "Replay";
    else if (kind === "live") pill = src.connected ? "Sensor connected" : "Sensor disconnected";
    if (kind && src.finished) pill += " - ended";
    text("source-pill", pill);
    show("synthetic-banner", kind === "simulation" || !!src.synthetic);
    show("replay-banner", kind === "replay");
    show("cal-sim", kind === "simulation");
    if (page === "sensor") renderSensor();
  }

  function renderSensor() {
    if (!status) return;
    const src = status.source || {};
    text("sn-source", src.kind ? (src.kind + (src.port ? " - " + src.port : "") + (src.running ? "" : " (stopped)")
      + (src.synthetic ? " [SYNTHETIC]" : "")) : "none");
    text("sn-rate", status.packet_rate_hz != null ? status.packet_rate_hz.toFixed(0) + " /s" : "-");
    const r = status.rssi || {};
    text("sn-rssi", r.last != null ? r.last + " dBm (mean " + (r.mean != null ? r.mean.toFixed(1) : "-") + ")" : "-");
    const q = status.quality || {};
    text("sn-quality", q.ok ? "OK" : (q.reasons && q.reasons.length ? "Insufficient: " + q.reasons.join("; ") : "-"));
    const c = status.config;
    text("sn-config", c ? (c.chip_family + ", TX " + c.tx_mac + ", channel " + c.channel
      + (c.bandwidth_mhz ? ", " + c.bandwidth_mhz + " MHz" : "") + ", " + c.n_subcarriers + " subcarriers") : "-");
    const cal = status.calibration || {};
    text("sn-calibration", cal.active ? (cal.name || cal.key) + (cal.posture_model ? "" : " (no posture model)") : "not calibrated");
    text("sn-error", src.last_error || status.engine_error || "none");
  }

  // ------------------------------------------------------------- posture
  let lastReminderActive = false;
  async function pollPosture() {
    let p;
    try { p = await api("/api/posture"); } catch (e) { return; }
    const state = p.state || "unknown";
    const label = STATE_LABEL[state] || state;
    const nowEl = $("now-state");
    nowEl.className = "now-state state-" + state;
    text("now-state", label);
    let reason = p.reason || "";
    if (!p.running) reason = "No data source - connect the sensor on the Sensor page.";
    else if (!p.calibrated) reason = "Not calibrated - calibrate the room.";
    text("now-reason", reason);
    show("now-calibrate-hint", !p.calibrated);
    text("now-confidence", p.confidence != null && state !== "unknown" ? Math.round(p.confidence * 100) + "%" : "-");
    if (p.streak && p.streak.active) text("now-streak", "Sitting for " + fmtDur(p.streak.seconds));
    else if (p.running && state !== "unknown") text("now-streak", label + " for " + fmtDur(p.state_duration_s));
    else text("now-streak", "-");
    if (p.upright_note) text("upright-note", p.upright_note);
    if (p.today) renderTodayTiles(p.today);

    const rem = p.reminder || {};
    show("reminder-banner", !!rem.active);
    if (rem.active) {
      text("reminder-text", rem.message || ("You have been sitting for " + fmtDur(p.sitting_streak_s)
        + ". Time to stand up for a minute."));
      if (!lastReminderActive) notifyReminder($("reminder-text").textContent);
    }
    lastReminderActive = !!rem.active;
  }

  function notifyReminder(msg) {
    unlockAudio();
    beep(660, 180, 2);
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification("Time to stand up", { body: msg }); } catch (e) { /* ignore */ }
    }
  }

  function renderTodayTiles(t) {
    const tot = t.totals_s || {};
    text("t-sitting", fmtDur(tot.sitting));
    text("t-standing", fmtDur(tot.standing));
    text("t-moving", fmtDur(tot.moving));
    text("t-upright", fmtDur(t.upright_s));
    text("t-standhours", (t.stand_hours != null ? t.stand_hours : "-") + " / " + (t.stand_goal_hours != null ? t.stand_goal_hours : "-"));
    text("t-transitions", t.transitions != null ? t.transitions : "-");
    text("t-longest", fmtDur(t.longest_sitting_s));
  }

  async function loadToday() {
    let t;
    try { t = await api("/api/posture/today"); } catch (e) { return; }
    renderTodayTiles(t);
    const h = t.hourly_min || {};
    const sit = h.sitting || [];
    const up = h.upright || [];
    drawStacked($("hourly-chart"), sit, up, (i) => String(i), 60, "min");
    const nowH = new Date().getHours();
    text("hourly-summary", "This hour (" + nowH + ":00): " + Math.round(sit[nowH] || 0) + " min sitting, "
      + Math.round(up[nowH] || 0) + " min upright. Stand hours so far: " + t.stand_hours + " of " + t.stand_goal_hours + ".");
  }

  // ----------------------------------------------------------- canvases
  function setupCanvas(canvas) {
    const ratio = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || canvas.width;
    const h = Math.round(w * (canvas.height / canvas.width)) || canvas.height;
    if (canvas._w !== w || canvas._r !== ratio) {
      canvas._w = w; canvas._r = ratio;
      canvas._h = Math.max(160, Math.min(h, 260));
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(canvas._h * ratio);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, canvas._w, canvas._h);
    ctx.font = "12px -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
    return { ctx, w: canvas._w, h: canvas._h };
  }

  /** Stacked bars: a (bottom, sitting colour) and b (top, upright colour). */
  function drawStacked(canvas, a, b, labelFn, minMax, unit) {
    if (!canvas || canvas.offsetParent === null) return;
    const { ctx, w, h } = setupCanvas(canvas);
    const n = Math.max(a.length, b.length);
    const left = 40, right = 8, top = 10, bottom = 24;
    const pw = w - left - right, ph = h - top - bottom;
    let max = minMax;
    for (let i = 0; i < n; i++) max = Math.max(max, (a[i] || 0) + (b[i] || 0));
    max = niceMax(max);
    ctx.strokeStyle = COLORS.grid; ctx.fillStyle = COLORS.text; ctx.lineWidth = 1;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let k = 0; k <= 4; k++) {
      const v = (max * k) / 4, y = top + ph - (v / max) * ph;
      ctx.beginPath(); ctx.moveTo(left, y + 0.5); ctx.lineTo(w - right, y + 0.5); ctx.stroke();
      ctx.fillText(fmtNum(v), left - 6, y);
    }
    ctx.save(); ctx.translate(10, top + ph / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = "center";
    ctx.fillText(unit, 0, 0); ctx.restore();
    const bw = pw / Math.max(n, 1);
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const every = n > 16 ? Math.ceil(n / 12) : 1;
    for (let i = 0; i < n; i++) {
      const x = left + i * bw + bw * 0.15, bwi = bw * 0.7;
      const ha = ((a[i] || 0) / max) * ph, hb = ((b[i] || 0) / max) * ph;
      ctx.fillStyle = COLORS.sitting; ctx.fillRect(x, top + ph - ha, bwi, ha);
      ctx.fillStyle = COLORS.upright; ctx.fillRect(x, top + ph - ha - hb, bwi, hb);
      if (i % every === 0) { ctx.fillStyle = COLORS.text; ctx.fillText(labelFn(i), left + i * bw + bw / 2, top + ph + 6); }
    }
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  function fmtNum(v) { return Math.abs(v) >= 10 || v === 0 ? String(Math.round(v)) : v.toFixed(1); }

  function drawSeries(canvas, ts, ys, lines) {
    if (!canvas || canvas.offsetParent === null) return;
    const { ctx, w, h } = setupCanvas(canvas);
    const left = 40, right = 8, top = 10, bottom = 22;
    const pw = w - left - right, ph = h - top - bottom;
    let max = 1;
    ys.forEach((y) => { if (y != null && isFinite(y)) max = Math.max(max, y); });
    lines.forEach((l) => { if (l != null) max = Math.max(max, l * 1.2); });
    max = niceMax(max);
    ctx.strokeStyle = COLORS.grid; ctx.fillStyle = COLORS.text; ctx.lineWidth = 1;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let k = 0; k <= 4; k++) {
      const v = (max * k) / 4, y = top + ph - (v / max) * ph;
      ctx.beginPath(); ctx.moveTo(left, y + 0.5); ctx.lineTo(w - right, y + 0.5); ctx.stroke();
      ctx.fillText(fmtNum(v), left - 6, y);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText("-60 s", left + 12, top + ph + 6); ctx.fillText("now", w - right - 12, top + ph + 6);
    const t1 = ts.length ? ts[ts.length - 1] : 0, t0 = t1 - 60;
    const xOf = (t) => left + ((t - t0) / 60) * pw;
    const yOf = (v) => top + ph - (Math.min(v, max) / max) * ph;
    ctx.setLineDash([5, 4]); ctx.strokeStyle = COLORS.text;
    lines.forEach((l) => { if (l != null) { ctx.beginPath(); ctx.moveTo(left, yOf(l)); ctx.lineTo(w - right, yOf(l)); ctx.stroke(); } });
    ctx.setLineDash([]); ctx.strokeStyle = COLORS.accent; ctx.lineWidth = 2; ctx.beginPath();
    let started = false;
    for (let i = 0; i < ts.length; i++) {
      const v = ys[i];
      if (v == null || !isFinite(v)) { started = false; continue; }
      if (!started) { ctx.moveTo(xOf(ts[i]), yOf(v)); started = true; } else ctx.lineTo(xOf(ts[i]), yOf(v));
    }
    ctx.stroke();
  }

  // ------------------------------------------------------------- history
  async function loadHistory() {
    const days = Number($("history-days").value) || 7;
    let hdata;
    try { hdata = await api("/api/posture/history?days=" + days); } catch (e) { text("history-summary", e.message); return; }
    const daily = hdata.daily || [];
    const rows = $("history-rows");
    rows.replaceChildren(...daily.slice().reverse().map((d) => el("tr", null, [
      el("th", { scope: "row", text: d.date }),
      el("td", { text: fmtMin(d.sitting_min) }), el("td", { text: fmtMin(d.standing_min) }),
      el("td", { text: fmtMin(d.moving_min) }), el("td", { text: fmtMin(d.upright_min) }),
      el("td", { text: d.sitting_bouts ? fmtMin(d.average_sitting_min) : "-" }),
      el("td", { text: d.sitting_bouts ? fmtMin(d.longest_sitting_min) : "-" }),
      el("td", { text: String(d.sit_to_stand_transitions) }),
      el("td", { text: d.stand_hours + " / " + d.stand_goal_hours }),
    ])));
    drawStacked($("history-chart"), daily.map((d) => d.sitting_min / 60), daily.map((d) => d.upright_min / 60),
      (i) => (daily[i] ? daily[i].date.slice(5) : ""), 1, "hours");
    const s = summarize(daily);
    text("history-summary", s.days ? (s.days + " of " + daily.length + " days have data. Average sitting "
      + fmtMin(s.avgSitting) + " per day, average sitting bout " + fmtMin(s.avgBout) + ", "
      + s.avgTransitions.toFixed(1) + " transitions and " + s.avgStandHours.toFixed(1) + " stand hours per day."
      + (hdata.synthetic ? " SYNTHETIC data." : ""))
      : "No tracked data in this period yet." + (hdata.synthetic ? " (Simulated data is kept in memory only.)" : ""));
  }

  function summarize(daily) {
    const withData = daily.filter((d) => (d.sitting_min + d.upright_min) > 0);
    const n = withData.length;
    const sum = (f) => withData.reduce((acc, d) => acc + (Number(f(d)) || 0), 0);
    const bouts = sum((d) => d.sitting_bouts);
    return {
      days: n,
      avgSitting: n ? sum((d) => d.sitting_min) / n : null,
      avgUpright: n ? sum((d) => d.upright_min) / n : null,
      avgBout: bouts ? sum((d) => d.average_sitting_min * d.sitting_bouts) / bouts : null,
      longest: n ? Math.max(...withData.map((d) => d.longest_sitting_min || 0)) : null,
      avgTransitions: n ? sum((d) => d.sit_to_stand_transitions) / n : 0,
      avgStandHours: n ? sum((d) => d.stand_hours) / n : 0,
    };
  }

  // -------------------------------------------------------------- health
  async function loadHealth() {
    let hdata;
    try { hdata = await api("/api/posture/history?days=7"); } catch (e) { return; }
    const daily = hdata.daily || [];
    const s = summarize(daily);
    text("health-avg-sitting", s.days ? fmtMin(s.avgSitting) : "No data yet");
    text("health-avg-bout", s.avgBout != null ? fmtMin(s.avgBout) : "-");
    // Share of hours with someone present (sitting or upright) that had a stand break.
    let present = 0, flagged = 0;
    const dates = daily.filter((d) => (d.sitting_min + d.upright_min) > 0).map((d) => d.date);
    const days = await Promise.all(dates.map((d) => api("/api/posture/today?date=" + d).catch(() => null)));
    days.forEach((t) => {
      if (!t) return;
      const h = t.hourly_min || {};
      for (let i = 0; i < 24; i++) {
        if (((h.sitting || [])[i] || 0) + ((h.upright || [])[i] || 0) > 0) {
          present++;
          if ((t.stand_hour_flags || [])[i]) flagged++;
        }
      }
    });
    text("health-break-share", present ? Math.round((100 * flagged) / present) + "% (" + flagged + " of " + present + ")" : "-");
    text("health-note", "Averages use the " + s.days + " day(s) with tracked data in the last 7 days. "
      + "Estimated from Wi-Fi signals; may be wrong." + (hdata.synthetic ? " SYNTHETIC data." : ""));
  }

  // --------------------------------------------------------------- share
  function shareRange() {
    const from = $("share-from").value, to = $("share-to").value;
    if (!from || !to) return { error: "Choose both dates." };
    const f = new Date(from + "T00:00:00"), t = new Date(to + "T00:00:00");
    const days = Math.round((t - f) / 86400000) + 1;
    if (!(days >= 1)) return { error: "'From' must be on or before 'To'." };
    if (days > 365) return { error: "Choose at most 365 days." };
    return { from, to, days };
  }
  function updateShareLinks() {
    const r = shareRange();
    text("share-error", r.error || "");
    ["share-csv", "share-json"].forEach((id) => {
      const a = $(id);
      if (r.error) { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); return; }
      a.removeAttribute("aria-disabled");
      a.href = "/api/posture/export?format=" + (id === "share-csv" ? "csv" : "json") + "&days=" + r.days + "&end=" + r.to;
    });
    return r;
  }
  async function loadSummary() {
    const r = updateShareLinks();
    if (r.error) return;
    const days = Math.min(r.days, 90);
    let hdata;
    try { hdata = await api("/api/posture/history?days=" + days + "&end=" + r.to); } catch (e) { text("share-error", e.message); return; }
    const daily = hdata.daily || [];
    const s = summarize(daily);
    text("summary-period", hdata.start_date + " to " + hdata.end_date + (r.days > 90 ? " (printed summary limited to the last 90 days; the downloads cover the full period)" : "")
      + ". Generated " + new Date().toLocaleString() + ".");
    show("summary-synthetic", !!hdata.synthetic);
    text("sum-days", s.days + " of " + daily.length);
    text("sum-sitting", fmtMin(s.avgSitting));
    text("sum-upright", fmtMin(s.avgUpright));
    text("sum-bout", fmtMin(s.avgBout));
    text("sum-longest", fmtMin(s.longest));
    text("sum-transitions", s.days ? s.avgTransitions.toFixed(1) : "-");
    text("sum-standhours", s.days ? s.avgStandHours.toFixed(1) + " (goal " + hdata.stand_goal_hours + ")" : "-");
    $("summary-rows").replaceChildren(...daily.filter((d) => (d.sitting_min + d.upright_min) > 0).map((d) => el("tr", null, [
      el("th", { scope: "row", text: d.date }), el("td", { text: fmtMin(d.sitting_min) }),
      el("td", { text: fmtMin(d.upright_min) }), el("td", { text: String(d.sitting_bouts) }),
      el("td", { text: d.sitting_bouts ? fmtMin(d.longest_sitting_min) : "-" }),
      el("td", { text: String(d.sit_to_stand_transitions) }), el("td", { text: String(d.stand_hours) }),
    ])));
  }

  // ------------------------------------------------------- calibration
  let calState = null;       // last /api/calibration/state
  let lastPhaseKey = null;   // kind:key of the last announced phase
  let wasRunning = false;
  let phaseEndsAt = null;    // performance.now() when the current phase ends
  let runEndsAt = null;
  let runTotal = null;
  let lastTick = null;
  const DUR_INPUT = (key) => "cal-dur-" + key;

  function phaseLabel(p, specByKey) {
    const spec = specByKey[p.key] || {};
    if (p.kind === "lead_in") return spec.person_present === false ? "Press Start, leave the room" : "Press Start, get ready";
    if (p.kind === "grace") return "Come back in";
    if (p.kind === "transition") return spec.prepare || "Get into position";
    const step = { empty_baseline: "Empty room recorded (stay out)", walking: "Walk back and forth (recorded)",
      standing_still: "Standing still recorded", sitting: "Sitting recorded" };
    return step[p.key] || p.title;
  }

  /** Client-side mirror of backend.calibration.build_timeline (for the plan before Start). */
  function buildTimeline(specs, durations, leadIn, grace, settle, transition) {
    const phases = [];
    let t = 0, prev = null;
    const add = (kind, key, title, length) => { phases.push({ kind, key, title, start_s: t, length_s: length }); t += length; };
    specs.forEach((spec, i) => {
      const dur = durations[spec.key];
      if (i === 0 && leadIn > 0) add("lead_in", spec.key, spec.person_present ? "Get ready" : "Leave the room now", leadIn);
      else if (prev && spec.person_present && !prev.person_present) { if (grace > 0) add("grace", spec.key, "Come back in", grace); }
      else if (prev && spec.person_present && prev.person_present && transition > 0) add("transition", spec.key, "Get into position", transition);
      add("step", spec.key, spec.title, settle + dur);
      prev = spec;
    });
    return { phases, total_s: t };
  }

  function readNum(id, lo, hi, name, errors) {
    const input = $(id);
    const v = Number(input.value);
    const ok = input.value.trim() !== "" && isFinite(v) && v >= lo && v <= hi;
    input.setAttribute("aria-invalid", ok ? "false" : "true");
    if (!ok) errors.push(name + " must be " + lo + "-" + hi + " s.");
    return v;
  }

  function readForm() {
    const errors = [];
    const st = calState || {};
    const rd = st.run_defaults || { max_lead_in_s: 60, max_grace_s: 60, max_transition_s: 30, settle_s: 1 };
    const placement = $("cal-placement").value.trim();
    const pOk = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$/.test(placement);
    $("cal-placement").setAttribute("aria-invalid", pOk ? "false" : "true");
    if (!pOk) errors.push("Room name must be 1-40 characters: letters, digits, spaces, '_' or '-'.");
    const out = {
      placement,
      lead_in_s: readNum("cal-lead", 0, rd.max_lead_in_s, "Time to leave", errors),
      grace_s: readNum("cal-grace", 0, rd.max_grace_s, "Time to come back", errors),
      transition_s: readNum("cal-transition", 0, rd.max_transition_s, "Time to change position", errors),
      durations: {}, auto: true,
    };
    (st.step_specs || []).forEach((s) => { out.durations[s.key] = readNum(DUR_INPUT(s.key), s.min_s, s.max_s, s.title, errors); });
    return { body: out, errors, settle: rd.settle_s || 0 };
  }

  function renderPlanTable(timeline, currentIdx) {
    const specs = (calState && calState.step_specs) || [];
    const byKey = Object.fromEntries(specs.map((s) => [s.key, s]));
    const rows = timeline.phases.map((p, i) => el("tr", { className: i === currentIdx ? "current" : (currentIdx != null && i < currentIdx ? "past" : "") }, [
      el("td", { text: clock(p.start_s) }),
      el("td", { text: phaseLabel(p, byKey) }),
      el("td", { text: lengthText(p.length_s) + (p.kind === "step" && byKey[p.key] && byKey[p.key].person_present === false ? ", stay out" : "") }),
    ]));
    rows.push(el("tr", { className: currentIdx != null && currentIdx >= timeline.phases.length ? "current" : "" }, [
      el("td", { text: clock(timeline.total_s) }), el("td", { text: "Done" }), el("td", { text: "" }),
    ]));
    $("cal-plan-rows").replaceChildren(...rows);
    text("cal-plan-total", clock(timeline.total_s) + " (" + lengthText(timeline.total_s) + ")");
  }

  function updatePlan() {
    if (!calState) return;
    const s = calState.session;
    if (s && s.running) return; // the server timeline is shown during a run
    const f = readForm();
    text("cal-form-error", f.errors.join(" "));
    $("cal-start").disabled = f.errors.length > 0;
    if (f.errors.length) return;
    const tl = buildTimeline(calState.step_specs, f.body.durations, f.body.lead_in_s, f.body.grace_s, f.settle, f.body.transition_s);
    renderPlanTable(tl, null);
  }

  function buildDurationInputs(specs) {
    const box = $("cal-durations");
    if (box.childElementCount) return;
    specs.forEach((s) => {
      box.appendChild(el("label", { for: DUR_INPUT(s.key), text: s.title + " (s, " + s.min_s + "-" + s.max_s + ")" }));
      box.appendChild(el("input", { id: DUR_INPUT(s.key), type: "number", min: String(s.min_s), max: String(s.max_s), step: "1", value: String(s.default_s) }));
    });
    box.querySelectorAll("input").forEach((i) => i.addEventListener("input", updatePlan));
  }

  async function pollCalibration() {
    let st;
    try { st = await api("/api/calibration/state"); } catch (e) { return; }
    const first = !calState;
    calState = st;
    if (first) {
      buildDurationInputs(st.step_specs || []);
      const rd = st.run_defaults || {};
      if (rd.lead_in_s != null) $("cal-lead").value = rd.lead_in_s;
      if (rd.grace_s != null) $("cal-grace").value = rd.grace_s;
      if (rd.transition_s != null) $("cal-transition").value = rd.transition_s;
      if (st.placement) $("cal-placement").value = st.placement;
    }
    renderCalibration();
  }

  function renderCalibration() {
    const st = calState;
    if (!st) return;
    const a = st.active;
    text("cal-active", a ? (a.name || a.key) + (a.posture_model ? "" : " - no sitting/standing model, recalibrate") : "None. The app cannot tell sitting from standing until you calibrate.");
    $("cal-clear").disabled = !a;

    const s = st.session;
    const running = !!(s && s.running);
    show("cal-setup", !running);
    show("cal-run", running);
    show("cal-review", !!s && !running && (s.state !== "ready" || s.steps.some((x) => x.status !== "pending")));

    if (running) renderRun(s);
    else {
      phaseEndsAt = null; runEndsAt = null;
      updatePlan();
    }
    if (wasRunning && !running) announceRunEnd(s);
    wasRunning = running;
    if (s) renderReview(s);
  }

  function renderRun(s) {
    const ph = s.phase;
    const tl = s.timeline;
    runTotal = tl.total_s;
    if (!ph) return;
    const now = performance.now();
    phaseEndsAt = now + ph.remaining_s * 1000;
    if (ph.run_remaining_s != null) runEndsAt = now + ph.run_remaining_s * 1000;
    const key = ph.kind + ":" + ph.key;
    if (key !== lastPhaseKey) {
      lastPhaseKey = key;
      lastTick = null;
      unlockAudio();
      beep(ph.kind === "step" ? 990 : 660, 220, ph.kind === "grace" ? 2 : 1);
      speak(ph.cue);
    }
    text("cal-phase-title", ph.title || "-");
    text("cal-instruction", ph.instruction || "");
    if (ph.next_kind === "done") text("cal-next", "Next: done. " + (ph.next_cue || ""));
    else text("cal-next", ph.next_title ? "Next: " + ph.next_title + (ph.next_cue ? " - " + ph.next_cue : "") : "");
    const idx = tl.phases.findIndex((p) => p.kind === ph.kind && p.key === ph.key);
    renderPlanTable(tl, idx < 0 ? null : idx);
    tickCountdown();
  }

  function tickCountdown() {
    if (phaseEndsAt == null) return;
    const now = performance.now();
    const rem = Math.max(0, (phaseEndsAt - now) / 1000);
    const secs = Math.ceil(rem);
    text("cal-countdown", clock(secs));
    // short ticks in the last 3 s before the next phase so the user hears it coming
    if (secs >= 1 && secs <= 3 && secs !== lastTick) { lastTick = secs; beep(440, 80, 1); }
    if (runEndsAt != null && runTotal) {
      const runRem = Math.max(0, (runEndsAt - now) / 1000);
      const pct = Math.min(100, Math.max(0, 100 * (1 - runRem / runTotal)));
      $("cal-progress-fill").style.width = pct.toFixed(1) + "%";
      $("cal-progress").setAttribute("aria-valuenow", String(Math.round(pct)));
      text("cal-progress-text", Math.round(pct) + "% - about " + lengthText(runRem) + " left of " + lengthText(runTotal));
    }
  }

  function announceRunEnd(s) {
    lastPhaseKey = null;
    unlockAudio();
    if (!s) return;
    if (s.state === "computed" || s.state === "saved") {
      beep(880, 200, 3);
      speak((s.timeline && s.timeline.done_cue) || "Calibration recording finished.");
    } else if (s.steps.some((x) => x.status === "failed") || s.state === "failed") {
      beep(300, 400, 2);
      speak("A calibration step failed. Come back to the computer to see why.");
    } else if (s.state === "ready" && s.steps.every((x) => x.status === "done" || x.status === "pending")) {
      speak("Calibration stopped.");
    }
  }

  function renderReview(s) {
    const running = s.running;
    $("cal-steps-rows").replaceChildren(...s.steps.map((x) => {
      const redo = el("button", { type: "button", className: "secondary", text: "Redo", "aria-label": "Redo " + x.title });
      redo.disabled = running || s.state === "saved";
      redo.addEventListener("click", () => calAction("/api/calibration/step/redo", { step: x.key }));
      const statusText = { pending: "Not recorded", recording: "Recording", done: "OK", failed: "Failed" }[x.status] || x.status;
      return el("tr", null, [
        el("th", { scope: "row", text: x.title }),
        el("td", { className: "status-" + x.status, text: statusText }),
        el("td", { text: x.ok_fraction != null ? Math.round(x.ok_fraction * 100) + "% of " + x.frames + " frames" : "-" }),
        el("td", { text: x.failure || "" }),
        el("td", null, x.status === "pending" ? [] : [redo]),
      ]);
    }));
    text("cal-error", s.error || "");
    const res = s.result;
    show("cal-result", !!res);
    if (res) {
      const sc = res.posture_model && res.posture_model.self_check && res.posture_model.self_check.sit_stand;
      text("cal-selfcheck", sc && sc.accuracy != null
        ? "Sitting vs standing within-calibration check, optimistic: " + Math.round(sc.accuracy * 100) + "% of "
          + sc.blocks + " blocks (chance " + Math.round((sc.chance_accuracy || 0.5) * 100) + "%). This is not validated accuracy."
          + (res.synthetic ? " SYNTHETIC data." : "")
        : "No sitting/standing self-check available.");
      $("cal-warnings").replaceChildren(...(res.warnings || []).map((w) => el("li", { text: w })));
    }
    const pendingOrFailed = s.steps.some((x) => x.status !== "done");
    show("cal-continue", !running && s.state === "ready" && pendingOrFailed);
    show("cal-save", s.state === "computed");
    show("cal-new", s.state === "saved" || s.state === "failed");
    show("cal-cancel", s.state !== "saved");
  }

  async function calAction(path, body) {
    text("cal-msg", "");
    try {
      const out = await api(path, body || {});
      if (out && out.state) { calState = out.state; renderCalibration(); }
      else await pollCalibration();
      return out;
    } catch (e) {
      text("cal-msg", "Error: " + e.message);
      text("cal-form-error", e.message);
      await pollCalibration();
      return null;
    }
  }

  async function startCalibration() {
    unlockAudio();
    const f = readForm();
    if (f.errors.length) { text("cal-form-error", f.errors.join(" ")); return; }
    text("cal-form-error", "");
    try {
      await api("/api/calibration/start", f.body);
      const out = await api("/api/calibration/step/start", {});
      calState = out.state;
      renderCalibration();
    } catch (e) {
      text("cal-form-error", "Could not start: " + e.message);
      await pollCalibration();
    }
  }

  async function loadCalibrations() {
    let out;
    try { out = await api("/api/calibrations"); } catch (e) { return; }
    const sel = $("cal-stored");
    const cur = sel.value;
    const opts = (out.calibrations || []).map((c) => el("option", { value: c.key, text: c.name || c.key }));
    if (!opts.length) opts.push(el("option", { value: "", text: "(none)" }));
    sel.replaceChildren(...opts);
    if (cur) sel.value = cur;
  }

  // -------------------------------------------------------------- sensor
  async function loadSources() {
    try {
      const p = await api("/api/ports");
      const sel = $("src-port"), cur = sel.value;
      sel.replaceChildren(el("option", { value: "auto", text: "auto (detect the receiver)" }),
        ...(p.ports || []).filter((x) => !x.excluded).map((x) => el("option", { value: x.device,
          text: x.device + (x.description ? " - " + x.description : "") + (x.likely_esp32 ? " (likely ESP32)" : "") })));
      if (cur) sel.value = cur;
      if (p.error) text("src-msg", p.error);
    } catch (e) { /* shown by the server banner */ }
    try {
      const r = await api("/api/replay_files");
      const sel = $("src-replay-file"), cur = sel.value;
      const files = (r.files || []).filter((f) => f.kind !== "take");
      sel.replaceChildren(...(files.length ? files.map((f) => el("option", { value: f.path, text: f.path + (f.synthetic ? " [SYNTHETIC]" : "") }))
        : [el("option", { value: "", text: "(none found)" })]));
      if (cur) sel.value = cur;
    } catch (e) { /* ignore */ }
    try {
      const s = await api("/api/simulation");
      const sel = $("src-sim-scenario"), cur = sel.value || "desk_day";
      sel.replaceChildren(...(s.scenarios || []).map((x) => el("option", { value: x.key, text: x.key + " - " + x.name })));
      sel.value = cur;
    } catch (e) { /* ignore */ }
  }

  async function setSource(body) {
    text("src-msg", "Starting...");
    try {
      await api("/api/source", body);
      text("src-msg", "Started.");
      pollStatus();
    } catch (e) { text("src-msg", "Error: " + e.message); }
  }

  async function pollSeries() {
    if (page !== "sensor") return;
    try {
      const s = await api("/api/series?seconds=60");
      const th = (status && status.thresholds) || {};
      drawSeries($("motion-chart"), s.ts || [], s.motion || [], [th.low, th.movement]);
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------- settings
  async function loadSettings() {
    try {
      const s = await api("/api/posture/settings");
      $("set-reminder").value = s.settings.reminder_after_min;
      $("set-goal").value = s.settings.stand_goal_hours;
      $("set-break").value = s.settings.stand_break_s;
    } catch (e) { /* ignore */ }
  }

  // --------------------------------------------------------------- events
  function bind() {
    window.addEventListener("hashchange", route);
    $("history-days").addEventListener("change", loadHistory);

    $("reminder-snooze").addEventListener("click", async () => {
      try { await api("/api/posture/reminder/snooze", { minutes: 10 }); show("reminder-banner", false); lastReminderActive = false; }
      catch (e) { text("reminder-text", "Could not snooze: " + e.message); }
    });
    if (!("Notification" in window) || Notification.permission !== "default") show("notify-enable", false);
    $("notify-enable").addEventListener("click", async () => {
      unlockAudio();
      try { await Notification.requestPermission(); } catch (e) { /* ignore */ }
      show("notify-enable", false);
    });

    $("settings-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      text("settings-msg", "");
      try {
        await api("/api/posture/settings", { reminder_after_min: Number($("set-reminder").value),
          stand_goal_hours: Number($("set-goal").value), stand_break_s: Number($("set-break").value) });
        text("settings-msg", "Saved.");
      } catch (e) { text("settings-msg", "Error: " + e.message); }
    });

    const today = new Date(), from = new Date();
    from.setDate(today.getDate() - 29);
    $("share-from").value = isoDate(from);
    $("share-to").value = isoDate(today);
    ["share-from", "share-to"].forEach((id) => $(id).addEventListener("change", loadSummary));
    $("share-print").addEventListener("click", async () => { await loadSummary(); window.print(); });
    ["share-csv", "share-json"].forEach((id) => $(id).addEventListener("click", (ev) => { if (shareRange().error) ev.preventDefault(); }));

    ["cal-placement", "cal-lead", "cal-grace", "cal-transition"].forEach((id) => $(id).addEventListener("input", updatePlan));
    $("cal-test-sound").addEventListener("click", () => {
      unlockAudio();
      beep(880, 200, 1);
      const was = $("cal-speak").checked;
      if (was) speak("This is how the calibration instructions will sound.");
      else text("cal-msg", "Speech is off; you heard the beep only.");
    });
    $("cal-start").addEventListener("click", startCalibration);
    $("cal-cancel-step").addEventListener("click", () => calAction("/api/calibration/step/cancel"));
    $("cal-continue").addEventListener("click", () => { unlockAudio(); calAction("/api/calibration/step/start"); });
    $("cal-cancel").addEventListener("click", () => calAction("/api/calibration/cancel"));
    $("cal-new").addEventListener("click", () => calAction("/api/calibration/cancel"));
    $("cal-save").addEventListener("click", async () => {
      const out = await calAction("/api/calibration/save");
      if (out) { text("cal-msg", "Saved as " + out.key + " and applied."); loadCalibrations(); }
    });
    $("cal-clear").addEventListener("click", async () => {
      await calAction("/api/calibration/clear");
      text("cal-active-msg", "Calibration cleared. Sitting and standing will show as unknown until you calibrate.");
    });
    $("cal-load").addEventListener("click", async () => {
      const key = $("cal-stored").value;
      if (!key) return;
      try {
        const out = await api("/api/calibration/load", { key });
        text("cal-active-msg", "Loaded." + ((out.warnings || []).length ? " Warning: " + out.warnings.join("; ") : ""));
      } catch (e) { text("cal-active-msg", "Error: " + e.message); }
      pollCalibration();
    });
    $("cal-sim").addEventListener("click", async () => {
      try {
        const out = await api("/api/simulation/calibrate", { placement: "simulated-room" });
        text("cal-active-msg", out.notice || "SYNTHETIC calibration applied.");
      } catch (e) { text("cal-active-msg", "Error: " + e.message); }
      loadCalibrations(); pollCalibration();
    });

    $("src-refresh").addEventListener("click", loadSources);
    $("src-serial-form").addEventListener("submit", (ev) => { ev.preventDefault(); setSource({ kind: "serial", port: $("src-port").value || "auto" }); });
    $("src-replay-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const path = $("src-replay-file").value;
      if (!path) { text("src-msg", "No replay file selected."); return; }
      setSource({ kind: "replay", path });
    });
    $("src-sim-form").addEventListener("submit", (ev) => { ev.preventDefault(); setSource({ kind: "simulation", scenario: $("src-sim-scenario").value }); });
    $("src-stop").addEventListener("click", async () => {
      try { await api("/api/stop", {}); text("src-msg", "Stopped."); } catch (e) { text("src-msg", "Error: " + e.message); }
      pollStatus();
    });
    window.addEventListener("resize", () => { if (page === "today") loadToday(); else if (page === "history") loadHistory(); });
  }

  // ---------------------------------------------------------------- loops
  function every(ms, fn) { fn(); return setInterval(fn, ms); }

  bind();
  route();
  loadSettings();
  every(2000, pollStatus);
  every(2000, pollPosture);
  every(10000, () => { if (page === "today") loadToday(); });
  every(30000, () => { if (page === "history") loadHistory(); });
  every(1000, pollSeries);
  // Calibration: fast while on the page or while a run is going (so cues play on any page).
  setInterval(() => { if (page === "calibrate" || wasRunning) pollCalibration(); }, 500);
  setInterval(tickCountdown, 200);
})();
