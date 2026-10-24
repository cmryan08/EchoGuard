/* EchoGuard dashboard client. Plain ES2017, no dependencies, no network
 * access other than the local backend (same origin). Polls the JSON API and
 * draws charts on <canvas>. Never shows invented values: missing data is
 * rendered as "No data yet" / "-". */
"use strict";

(function () {
  const POLL_MS = 400;          // ~2.5 Hz
  const SERIES_SECONDS = 30;
  const HEATMAP_ROWS = 300;
  const REQUEST_TIMEOUT_MS = 4000;
  const NO_DATA = "No data yet";

  const ACTIVITY_KEYS = ["empty_low", "movement", "rapid_movement", "potential_fall", "unknown"];
  const COLORS = {
    text: "#e6eaf2", muted: "#9aa5bd", grid: "#232c42", accent: "#5ab0ff",
    ok: "#3ddc97", warn: "#ffb547", bad: "#ff5d6c", violet: "#d27bff",
  };

  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------ helpers
  async function api(path, options) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
    const opts = Object.assign({ signal: ctrl.signal, cache: "no-store" }, options || {});
    try {
      const resp = await fetch(path, opts);
      let body = null;
      try { body = await resp.json(); } catch (e) { body = null; }
      if (!resp.ok) {
        const msg = body && body.error ? body.error : "HTTP " + resp.status;
        throw new Error(msg);
      }
      return body;
    } finally {
      clearTimeout(timer);
    }
  }

  function postJSON(path, data) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data || {}),
    });
  }

  const isNum = (v) => typeof v === "number" && isFinite(v);

  function fmt(v, digits, unit) {
    if (!isNum(v)) return null;
    const s = v.toFixed(digits === undefined ? 1 : digits);
    return unit ? s + " " + unit : s;
  }

  function setText(id, value, fallback) {
    const el = $(id);
    if (!el) return;
    const text = value === null || value === undefined || value === "" ? (fallback === undefined ? "-" : fallback) : String(value);
    if (el.textContent !== text) el.textContent = text;
  }

  function setClass(el, base, cls) {
    const next = base + " " + cls;
    if (el.className !== next) el.className = next;
  }

  function showMessage(text, isError) {
    const el = $("control-msg");
    el.textContent = text || "";
    el.style.color = isError ? COLORS.bad : "";
  }

  // ------------------------------------------------------------ canvas
  function prepCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(10, Math.floor(canvas.clientWidth));
    const h = Math.max(10, Math.floor(canvas.clientHeight));
    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx: ctx, w: w, h: h };
  }

  function drawEmpty(ctx, w, h, text) {
    ctx.fillStyle = COLORS.muted;
    ctx.font = "13px -apple-system, Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, w / 2, h / 2);
  }

  // Viridis-like colour map (5 stops), precomputed into a 256 entry LUT.
  const CMAP_STOPS = [
    [0.0, [68, 1, 84]], [0.25, [59, 82, 139]], [0.5, [33, 145, 140]],
    [0.75, [94, 201, 98]], [1.0, [253, 231, 37]],
  ];
  const LUT = (function () {
    const lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let k = 0;
      while (k < CMAP_STOPS.length - 2 && t > CMAP_STOPS[k + 1][0]) k++;
      const a = CMAP_STOPS[k], b = CMAP_STOPS[k + 1];
      const f = (t - a[0]) / (b[0] - a[0]);
      for (let c = 0; c < 3; c++) lut[i * 3 + c] = a[1][c] + (b[1][c] - a[1][c]) * f;
    }
    return lut;
  })();

  function drawLegend() {
    const canvas = $("heat-legend");
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(canvas.width, canvas.height);
    for (let x = 0; x < canvas.width; x++) {
      const i = Math.round((x / (canvas.width - 1)) * 255);
      for (let y = 0; y < canvas.height; y++) {
        const p = (y * canvas.width + x) * 4;
        img.data[p] = LUT[i * 3]; img.data[p + 1] = LUT[i * 3 + 1]; img.data[p + 2] = LUT[i * 3 + 2]; img.data[p + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

  const heatOffscreen = document.createElement("canvas");

  function drawHeatmap(data) {
    const canvas = $("heatmap");
    const c = prepCanvas(canvas);
    const ctx = c.ctx;
    const rows = data && data.rows ? data.rows : [];
    if (!rows.length) {
      setText("heat-min", null); setText("heat-max", null);
      drawEmpty(ctx, c.w, c.h, NO_DATA + " - waiting for CSI packets");
      return;
    }
    const nT = rows.length;
    const nS = rows[0].length;
    let vmin = isNum(data.vmin) ? data.vmin : 0;
    let vmax = isNum(data.vmax) ? data.vmax : 1;
    if (vmax - vmin < 1e-6) { vmin -= 0.5; vmax += 0.5; }
    setText("heat-min", vmin.toFixed(2)); setText("heat-max", vmax.toFixed(2));

    // time on the x axis (newest at right), subcarrier on the y axis
    heatOffscreen.width = nT; heatOffscreen.height = nS;
    const octx = heatOffscreen.getContext("2d");
    const img = octx.createImageData(nT, nS);
    const span = vmax - vmin;
    for (let t = 0; t < nT; t++) {
      const row = rows[t];
      for (let s = 0; s < nS; s++) {
        const v = row[s];
        const p = ((nS - 1 - s) * nT + t) * 4;
        if (!isNum(v)) {
          img.data[p] = 30; img.data[p + 1] = 34; img.data[p + 2] = 44; img.data[p + 3] = 255;
          continue;
        }
        let i = Math.round(((v - vmin) / span) * 255);
        i = i < 0 ? 0 : (i > 255 ? 255 : i);
        img.data[p] = LUT[i * 3]; img.data[p + 1] = LUT[i * 3 + 1]; img.data[p + 2] = LUT[i * 3 + 2]; img.data[p + 3] = 255;
      }
    }
    octx.putImageData(img, 0, 0);

    const padL = 52, padB = 22, padT = 6, padR = 8;
    const pw = c.w - padL - padR, ph = c.h - padT - padB;
    // keep a fixed time scale: fewer rows than HEATMAP_ROWS fill from the right
    const drawW = Math.max(1, Math.round(pw * Math.min(1, nT / HEATMAP_ROWS)));
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = "#0e1420";
    ctx.fillRect(padL, padT, pw, ph);
    ctx.drawImage(heatOffscreen, padL + pw - drawW, padT, drawW, ph);

    ctx.fillStyle = COLORS.muted;
    ctx.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const labels = data.labels || [];
    if (labels.length) {
      ctx.fillText(labels[labels.length - 1], padL - 4, padT + 6);
      ctx.fillText(labels[0], padL - 4, padT + ph - 6);
    }
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(nT + " packets, " + nS + " subcarriers", padL, padT + ph + 5);
    ctx.textAlign = "right";
    ctx.fillText("newest", padL + pw, padT + ph + 5);
  }

  /* Generic time-series line chart.
   * opts: {ts, tEnd, seconds, series: [{values, color, label}], thresholds: [{value, color, label}],
   *        unit, digits, minRange, zeroBased} */
  function drawLineChart(canvas, opts) {
    const c = prepCanvas(canvas);
    const ctx = c.ctx;
    const ts = opts.ts || [];
    const padL = 52, padR = 10, padT = 10, padB = 22;
    const pw = c.w - padL - padR, ph = c.h - padT - padB;

    let lo = Infinity, hi = -Infinity, count = 0;
    opts.series.forEach((s) => s.values.forEach((v) => {
      if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); count++; }
    }));
    if (!ts.length || !count) {
      drawEmpty(ctx, c.w, c.h, NO_DATA);
      return;
    }
    (opts.thresholds || []).forEach((t) => {
      if (isNum(t.value)) { lo = Math.min(lo, t.value); hi = Math.max(hi, t.value); }
    });
    if (opts.zeroBased) lo = Math.min(0, lo);
    const minRange = opts.minRange || 1e-3;
    if (hi - lo < minRange) { const mid = (hi + lo) / 2; lo = mid - minRange / 2; hi = mid + minRange / 2; }
    const padY = (hi - lo) * 0.08;
    hi += padY; if (!opts.zeroBased || lo < 0) lo -= padY;

    const tEnd = isNum(opts.tEnd) ? opts.tEnd : ts[ts.length - 1];
    const seconds = opts.seconds || SERIES_SECONDS;
    const x = (t) => padL + pw * (1 - (tEnd - t) / seconds);
    const y = (v) => padT + ph * (1 - (v - lo) / (hi - lo));

    // grid + y labels
    ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
    ctx.fillStyle = COLORS.muted;
    ctx.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    const digits = opts.digits === undefined ? 2 : opts.digits;
    for (let i = 0; i <= 4; i++) {
      const v = lo + (hi - lo) * (i / 4);
      const yy = Math.round(y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke();
      ctx.fillText(v.toFixed(digits), padL - 5, yy);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let s = 0; s <= seconds; s += Math.max(5, Math.round(seconds / 6))) {
      const xx = x(tEnd - s);
      ctx.fillText(s === 0 ? "now" : "-" + s + "s", xx, padT + ph + 5);
    }

    // thresholds
    (opts.thresholds || []).forEach((t) => {
      if (!isNum(t.value)) return;
      const yy = Math.round(y(t.value)) + 0.5;
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = t.color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = t.color; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
      ctx.fillText(t.label, padL + 4, yy - 2);
    });

    // series (gaps where values are null)
    ctx.save();
    ctx.beginPath(); ctx.rect(padL, padT, pw, ph); ctx.clip();
    opts.series.forEach((s) => {
      ctx.strokeStyle = s.color; ctx.lineWidth = 1.8; ctx.lineJoin = "round";
      ctx.beginPath();
      let pen = false;
      for (let i = 0; i < ts.length; i++) {
        const v = s.values[i];
        if (!isNum(v) || !isNum(ts[i])) { pen = false; continue; }
        const xx = x(ts[i]), yy = y(v);
        if (pen) ctx.lineTo(xx, yy); else ctx.moveTo(xx, yy);
        pen = true;
      }
      ctx.stroke();
    });
    ctx.restore();

    // legend
    ctx.textAlign = "right"; ctx.textBaseline = "top";
    let lx = padL + pw - 4;
    opts.series.slice().reverse().forEach((s) => {
      if (!s.label) return;
      ctx.fillStyle = s.color;
      ctx.fillText(s.label, lx, padT + 2);
      lx -= ctx.measureText(s.label).width + 14;
    });
  }

  // ------------------------------------------------------------ rendering
  let lastStatus = null;

  function renderStatus(st) {
    lastStatus = st;
    const src = st.source || {};
    const counters = st.counters || {};
    const cfg = st.config || {};
    const hasPackets = isNum(counters.packets) && counters.packets > 0;

    // top bar + banner
    const pill = $("source-pill");
    const banner = $("source-banner");
    if (!src.kind) {
      pill.textContent = "No source"; setClass(pill, "pill", "pill-muted");
    } else {
      const kindName = { live: "Live serial", replay: "Replay", simulation: "Simulation" }[src.kind] || src.kind;
      const state = src.finished ? "finished" : (src.connected ? (src.running ? "running" : "stopped") : (src.running ? "connecting" : "stopped"));
      pill.textContent = kindName + ": " + state;
      setClass(pill, "pill", src.connected && src.running ? "pill-ok" : (src.running ? "pill-warn" : "pill-muted"));
    }
    const gt = st.ground_truth || null;
    if (src.kind === "simulation" || src.synthetic) {
      banner.textContent = "SIMULATED DATA - synthetic, not evidence of real-world accuracy" + (gt ? " | label: " + gt.label_name : "");
      setClass(banner, "banner", "banner-sim");
    } else if (src.kind === "replay") {
      banner.textContent = "REPLAY of recorded data - not a live measurement" + (gt ? " | ground truth: " + gt.label_name + (gt.take_id ? " (take " + gt.take_id + ")" : "") : "");
      setClass(banner, "banner", "banner-replay");
    } else {
      banner.textContent = "";
      setClass(banner, "banner", "hidden");
    }

    // connection card
    let conn = "No source";
    if (src.kind === "live") conn = src.connected ? "Connected" : (src.running ? "Disconnected (retrying)" : "Stopped");
    else if (src.kind === "replay") conn = src.finished ? "Replay finished" : (src.running ? "Replaying" : "Stopped");
    else if (src.kind) conn = src.running ? "Running" : "Stopped";
    setText("conn-state", conn);
    setText("conn-port", src.port);
    setText("conn-reconnects", src.kind ? src.reconnects : null);
    setText("conn-error", src.engine_error || src.last_error);

    // packet rate card
    setText("rate", hasPackets ? fmt(st.packet_rate_hz, 1, "Hz") : null, NO_DATA);
    setText("cnt-packets", src.kind ? counters.packets : null);
    setText("cnt-lines", src.kind ? counters.lines : null);
    setText("pkt-age", isNum(st.last_packet_age_s) ? st.last_packet_age_s.toFixed(1) + " s ago" : null);

    // signal card
    const rssi = st.rssi || {};
    setText("rssi", isNum(rssi.last) ? rssi.last + " dBm" : null, NO_DATA);
    setText("rssi-stats", isNum(rssi.mean) ? fmt(rssi.mean, 1) + " / " + (fmt(rssi.std, 1) || "-") + " dB" : null);
    setText("noise-floor", isNum(st.noise_floor) ? st.noise_floor + " dBm" : null);
    const motion = st.motion || null;
    setText("mean-amp", motion ? fmt(motion.mean_amplitude, 2) : null);

    // errors card
    setText("malformed", src.kind ? counters.malformed + " malformed" : null, NO_DATA);
    setText("seq-gaps", src.kind ? counters.seq_gaps : null);
    setText("seq-dups", src.kind ? counters.duplicates + " / " + counters.seq_resets : null);
    const reasons = counters.malformed_reasons || {};
    const reasonText = Object.keys(reasons).map((k) => k + ": " + reasons[k]).join(", ");
    setText("malformed-reasons", reasonText || null);

    // quality
    const q = st.quality || {};
    const qb = $("quality-badge");
    if (q.ok === true) { qb.textContent = "Sufficient"; setClass(qb, "badge", "badge-ok"); }
    else if (q.ok === false) { qb.textContent = "Insufficient"; setClass(qb, "badge", "badge-bad"); }
    else { qb.textContent = "No source"; setClass(qb, "badge", "badge-muted"); }
    const ul = $("quality-reasons");
    const items = (q.reasons || []).map((r) => "<li>" + escapeHtml(r) + "</li>").join("");
    if (ul.innerHTML !== items) ul.innerHTML = items;

    // configuration
    setText("cfg-chip", cfg.chip_family ? cfg.chip_family + (cfg.header_family ? " (header: " + cfg.header_family + ")" : "") : null);
    setText("cfg-channel", cfg.channel);
    setText("cfg-secondary", isNum(cfg.secondary_channel) ? ["none", "above", "below"][cfg.secondary_channel] || cfg.secondary_channel : null);
    setText("cfg-bw", isNum(cfg.bandwidth_mhz) ? cfg.bandwidth_mhz + " MHz" : (isNum(cfg.bandwidth) ? "code " + cfg.bandwidth : null));
    setText("cfg-len", cfg.csi_len);
    setText("cfg-nsc", cfg.n_subcarriers);
    setText("cfg-mac", cfg.tx_mac);
    const cal = st.calibration || {};
    let calText = "none (adaptive reference)";
    if (cal.name) {
      calText = cal.name + (cal.auto_loaded ? " [auto-loaded]" : "") + (cal.active ? "" : " [loaded, layout mismatch: not applied]");
    }
    setText("cfg-calib", calText);
    const calPill = $("calib-pill");
    if (cal.name) {
      calPill.textContent = "Calibration: " + cal.name;
      setClass(calPill, "pill", cal.active ? "pill-ok" : "pill-warn");
    } else {
      calPill.textContent = "Uncalibrated";
      setClass(calPill, "pill", "pill-muted");
    }
    const recPill = $("rec-pill");
    const rec = st.recording || {};
    recPill.classList.toggle("hidden", !rec.active);
    if (rec.active) recPill.textContent = "REC";
    const rx = cfg.rx_stats;
    setText("cfg-rxstats", rx ? rx.csi_rx_rate_hz.toFixed(1) + " Hz, total " + rx.csi_rx_total + ", drops " + rx.queue_drops + ", gaps " + rx.seq_gaps + ", up " + Math.round(rx.uptime_ms / 1000) + " s" : null);
    const tx = cfg.tx_stats;
    setText("cfg-txstats", tx ? tx.rate_hz.toFixed(1) + " Hz, ok " + tx.sent_ok + ", fail " + tx.send_fail + ", up " + Math.round(tx.uptime_ms / 1000) + " s" : null);

    // activity
    const act = st.activity || { key: "unknown", label: "Unknown / insufficient data" };
    const badge = $("activity-badge");
    const key = ACTIVITY_KEYS.indexOf(act.key) >= 0 ? act.key : "unknown";
    setClass(badge, "activity", "act-" + key);
    if (badge.textContent !== act.label) badge.textContent = act.label;
    setText("motion-score", motion ? fmt(motion.motion_score, 2) : null, NO_DATA);
    setText("motion-dev", motion ? fmt(motion.baseline_deviation, 2) : null);
    setText("motion-var", motion ? fmt(motion.variance, 4) : null);
    setText("motion-diff", motion ? fmt(motion.diff_energy, 4) : null);
    const th = st.thresholds || {};
    setText("motion-th", isNum(th.movement) ? "movement " + th.movement + ", rapid " + th.rapid + ", still " + th.low + (th.calibrated ? " (calibrated)" : " (uncalibrated)") : null);
    setText("ground-truth", gt ? gt.label_name + (gt.take_id ? " (take " + gt.take_id + ")" : "") : null);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  }

  function renderSeries(se) {
    const th = (se && se.thresholds) || (lastStatus && lastStatus.thresholds) || {};
    const ts = (se && se.ts) || [];
    const base = { ts: ts, tEnd: se ? se.t_end : null, seconds: SERIES_SECONDS };
    drawLineChart($("chart-amp"), Object.assign({}, base, {
      series: [{ values: (se && se.mean_amplitude) || [], color: COLORS.accent, label: "mean |CSI|" }],
      digits: 1, minRange: 1,
    }));
    drawLineChart($("chart-motion"), Object.assign({}, base, {
      series: [{ values: (se && se.motion) || [], color: COLORS.ok, label: "motion score" }],
      thresholds: [
        { value: th.low, color: COLORS.violet, label: "stillness" },
        { value: th.movement, color: COLORS.accent, label: "movement" },
        { value: th.rapid, color: COLORS.warn, label: "rapid / spike" },
      ],
      digits: 1, minRange: 1, zeroBased: true,
    }));
    drawLineChart($("chart-dev"), Object.assign({}, base, {
      series: [{ values: (se && se.baseline_deviation) || [], color: COLORS.violet, label: "deviation (z)" }],
      digits: 1, minRange: 0.5, zeroBased: true,
    }));
    drawLineChart($("chart-rssi"), Object.assign({}, base, {
      series: [{ values: (se && se.rssi) || [], color: COLORS.warn, label: "RSSI dBm" }],
      digits: 0, minRange: 4,
    }));
  }

  // ------------------------------------------------------------ calibration
  const STEP_STATUS_TEXT = { pending: "pending", recording: "recording", done: "done", failed: "failed" };
  const STEP_COLORS = { empty_baseline: COLORS.ok, walking: COLORS.accent, standing_still: COLORS.violet, sitting: COLORS.warn };
  let lastCalib = null;
  let countdown = null;   // {kind: "calib"|"take", endsAt, label, timer}

  function setMsg(id, text, cls) {
    const el = $(id);
    if (!el) return;
    if (el.textContent !== (text || "")) el.textContent = text || "";
    const next = "msg" + (cls ? " " + cls : "");
    if (el.className !== next) el.className = next;
  }

  function setHTML(el, html) {
    if (el.innerHTML !== html) el.innerHTML = html;
  }

  function fmtNum(v, d) { return isNum(v) ? v.toFixed(d === undefined ? 2 : d) : "-"; }

  function renderCalibration(cs) {
    lastCalib = cs;
    const active = cs.active;
    setText("calib-active", active ? active.name + (active.auto_loaded ? " [auto-loaded]" : "") : "none (adaptive reference, uncalibrated thresholds)");
    const la = cs.last_auto;
    let autoText = null;
    if (la && la.action === "loaded") autoText = "loaded " + la.key + " for the detected sensor";
    else if (la && la.action === "cleared") autoText = "cleared " + la.key + ": " + (la.reason || "sensor mismatch");
    else if (la && la.action === "none") autoText = "no saved calibration matches the detected sensor";
    setText("calib-auto", autoText, "waiting for packets");

    const s = cs.session;
    const wizard = $("calib-wizard");
    wizard.classList.toggle("hidden", !s);
    $("calib-start").textContent = s ? "Restart calibration" : "Start calibration";
    if (!s) {
      $("calib-results").classList.add("hidden");
      return;
    }
    const recording = s.state === "recording";
    const items = s.steps.map((st, i) => {
      const cur = i === s.step_index && (s.state === "ready" || recording);
      const frac = isNum(st.ok_fraction) ? Math.round(st.ok_fraction * 100) + "% good" : "";
      const detail = st.status === "pending" ? st.duration_s + " s" :
        fmtNum(st.elapsed_s, 1) + " / " + st.duration_s + " s, " + st.ok_frames + " good frames" + (frac ? " (" + frac + ")" : "");
      const redo = (st.status === "done" || st.status === "failed") && !recording && !countdown
        ? '<button type="button" class="btn btn-ghost btn-small" data-redo="' + escapeHtml(st.key) + '">Redo</button>' : "";
      return '<li class="st-' + escapeHtml(st.status) + (cur ? " current" : "") + '">' +
        '<div class="step-head"><span>' + (i + 1) + ". " + escapeHtml(st.title) + '</span><span class="step-status">' +
        escapeHtml(STEP_STATUS_TEXT[st.status] || st.status) + "</span></div>" +
        '<div class="step-detail">' + escapeHtml(detail) + " " + redo + "</div>" +
        (st.failure ? '<div class="step-fail">' + escapeHtml(st.failure) + "</div>" : "") + "</li>";
    }).join("");
    setHTML($("calib-steps"), items);

    const cur = s.steps[s.step_index] || null;
    if (cur && (s.state === "ready" || recording)) {
      setText("calib-step-title", "Step " + (s.step_index + 1) + " of " + s.steps.length + ": " + cur.title);
      setText("calib-step-instr", cur.instructions);
      const p = cur.duration_s > 0 ? Math.min(1, (cur.elapsed_s || 0) / cur.duration_s) : 0;
      $("calib-progress").value = recording ? p : 0;
      setText("calib-progress-text", recording ? fmtNum(cur.elapsed_s, 1) + " / " + cur.duration_s + " s" : cur.duration_s + " s");
    } else {
      setText("calib-step-title", s.state === "failed" ? "Calibration could not be computed" : "All steps recorded");
      setText("calib-step-instr", s.state === "failed" ? "Redo the steps listed with problems." : "Review the results below, then save.");
      $("calib-progress").value = s.state === "failed" ? 0 : 1;
      setText("calib-progress-text", "-");
    }
    const lq = cur && cur.last_quality;
    const qb = $("calib-quality");
    const q = lastStatus && lastStatus.quality ? lastStatus.quality : {};
    const qok = lq ? lq.ok : q.ok;
    if (qok === true) { qb.textContent = "Sufficient"; setClass(qb, "badge", "badge-ok"); }
    else if (qok === false) { qb.textContent = "Insufficient"; setClass(qb, "badge", "badge-bad"); }
    else { qb.textContent = "No data"; setClass(qb, "badge", "badge-muted"); }
    const reasons = lq ? lq.reasons : (q.reasons || []);
    setText("calib-quality-reasons", reasons && reasons.length ? reasons.join("; ") : null, "");

    $("calib-step-start").disabled = !(s.state === "ready") || !!countdown;
    $("calib-step-cancel").disabled = !(recording || (countdown && countdown.kind === "calib"));
    if (!(countdown && countdown.kind === "calib")) {
      if (s.state === "failed") setMsg("calib-msg", s.error, "error");
      else if (recording) setMsg("calib-msg", "Recording: " + (cur ? cur.title : ""), "");
      else if (cur && cur.status === "failed") setMsg("calib-msg", "Step failed - fix the problem and start it again.", "error");
      else if (s.state === "computed") setMsg("calib-msg", "Calibration computed. Save it to use it.", "");
      else if (s.state === "saved") setMsg("calib-msg", "Calibration saved and active.", "");
      else setMsg("calib-msg", "", "");
    }

    const res = s.result;
    $("calib-results").classList.toggle("hidden", !res);
    if (res) renderCalibResult(res, s);
  }

  function renderCalibResult(res, s) {
    const th = res.thresholds || {};
    setText("calib-th-low", fmtNum(th.low));
    setText("calib-th-movement", fmtNum(th.movement));
    setText("calib-th-rapid", fmtNum(th.rapid));
    setText("calib-separable", res.separable ? "yes" : "NO (fallback thresholds)");
    setText("calib-noise-var", isNum(res.noise_variance) ? res.noise_variance.toExponential(3) : null);
    setText("calib-noise-diff", isNum(res.noise_diff) ? res.noise_diff.toExponential(3) : null);
    const sen = res.sensor || {};
    setText("calib-sensor", sen.chip_family ? sen.chip_family + ", TX " + sen.tx_mac + ", ch " + sen.channel + "/" + sen.secondary_channel + ", len " + sen.csi_len : null);
    setText("calib-synthetic", res.synthetic ? "SYNTHETIC (simulation)" : (res.source_kind || "-"));
    setHTML($("calib-warnings"), (res.warnings || []).map((w) => "<li>" + escapeHtml(w) + "</li>").join(""));
    $("calib-save").disabled = s.state !== "computed";
    setText("calib-saved", s.state === "saved" && res.key ? "Saved as data/calibration/" + res.key + ".json" : null, "");
    drawBoxPlot($("calib-box"), res);
    drawProfiles($("calib-profile"), res);
  }

  function drawBoxPlot(canvas, res) {
    const c = prepCanvas(canvas);
    const ctx = c.ctx;
    const steps = res.steps || {};
    const keys = Object.keys(steps);
    const th = res.thresholds || {};
    let lo = Infinity, hi = -Infinity;
    keys.forEach((k) => {
      const m = steps[k].stats && steps[k].stats.motion_score;
      if (m) { lo = Math.min(lo, m.p05, m.min); hi = Math.max(hi, m.p95); }
    });
    ["low", "movement", "rapid"].forEach((n) => { if (isNum(th[n])) { lo = Math.min(lo, th[n]); hi = Math.max(hi, th[n]); } });
    if (!isFinite(lo) || !isFinite(hi)) { drawEmpty(ctx, c.w, c.h, NO_DATA); return; }
    lo = Math.max(1e-2, lo * 0.8); hi = Math.max(hi * 1.25, lo * 2);
    const padL = 52, padR = 70, padT = 10, padB = 24;
    const pw = c.w - padL - padR, ph = c.h - padT - padB;
    const L = (v) => Math.log10(Math.max(v, 1e-6));
    const y = (v) => padT + ph * (1 - (L(v) - L(lo)) / (L(hi) - L(lo)));
    ctx.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
    ctx.strokeStyle = COLORS.grid; ctx.fillStyle = COLORS.muted; ctx.lineWidth = 1;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let e = Math.floor(L(lo)); e <= Math.ceil(L(hi)); e++) {
      [1, 2, 5].forEach((m) => {
        const v = m * Math.pow(10, e);
        if (v < lo || v > hi) return;
        const yy = Math.round(y(v)) + 0.5;
        ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke();
        ctx.fillText(String(+v.toPrecision(2)), padL - 5, yy);
      });
    }
    [["low", COLORS.violet], ["movement", COLORS.accent], ["rapid", COLORS.warn]].forEach((t) => {
      const v = th[t[0]];
      if (!isNum(v)) return;
      const yy = Math.round(y(v)) + 0.5;
      ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = t[1];
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke(); ctx.restore();
      ctx.fillStyle = t[1]; ctx.textAlign = "left"; ctx.fillText(t[0] + " " + v.toFixed(2), padL + pw + 4, yy);
    });
    const slot = pw / Math.max(1, keys.length);
    keys.forEach((k, i) => {
      const m = steps[k].stats && steps[k].stats.motion_score;
      const cx = padL + slot * (i + 0.5);
      const bw = Math.min(60, slot * 0.45);
      ctx.fillStyle = COLORS.muted; ctx.textAlign = "center"; ctx.textBaseline = "top";
      ctx.fillText(steps[k].title, cx, padT + ph + 6);
      if (!m) return;
      const col = STEP_COLORS[k] || COLORS.text;
      ctx.strokeStyle = col; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(cx, y(m.p05)); ctx.lineTo(cx, y(m.p25)); ctx.moveTo(cx, y(m.p75)); ctx.lineTo(cx, y(m.p95));
      ctx.moveTo(cx - bw / 4, y(m.p05)); ctx.lineTo(cx + bw / 4, y(m.p05));
      ctx.moveTo(cx - bw / 4, y(m.p95)); ctx.lineTo(cx + bw / 4, y(m.p95)); ctx.stroke();
      const top = y(m.p75), bot = y(m.p25);
      ctx.globalAlpha = 0.25; ctx.fillStyle = col; ctx.fillRect(cx - bw / 2, top, bw, Math.max(1, bot - top)); ctx.globalAlpha = 1;
      ctx.strokeRect(cx - bw / 2, top, bw, Math.max(1, bot - top));
      ctx.lineWidth = 2.5; ctx.beginPath(); ctx.moveTo(cx - bw / 2, y(m.median)); ctx.lineTo(cx + bw / 2, y(m.median)); ctx.stroke();
    });
  }

  function drawProfiles(canvas, res) {
    const c = prepCanvas(canvas);
    const ctx = c.ctx;
    const base = res.baseline || {};
    const mean = base.mean_profile || [], std = base.std_profile || [];
    const walk = (res.steps && res.steps.walking && res.steps.walking.profile_mean) || [];
    const walkStd = (res.steps && res.steps.walking && res.steps.walking.profile_std) || [];
    const n = mean.length;
    if (!n) { drawEmpty(ctx, c.w, c.h, NO_DATA); return; }
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < n; i++) {
      [isNum(mean[i]) && isNum(std[i]) ? mean[i] - std[i] : null, isNum(mean[i]) && isNum(std[i]) ? mean[i] + std[i] : null,
        isNum(walk[i]) && isNum(walkStd[i]) ? walk[i] - walkStd[i] : walk[i], isNum(walk[i]) && isNum(walkStd[i]) ? walk[i] + walkStd[i] : walk[i]]
        .forEach((v) => { if (isNum(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    }
    if (!isFinite(lo)) { drawEmpty(ctx, c.w, c.h, NO_DATA); return; }
    const pad = (hi - lo) * 0.08 || 0.1; lo -= pad; hi += pad;
    const padL = 52, padR = 10, padT = 10, padB = 22;
    const pw = c.w - padL - padR, ph = c.h - padT - padB;
    const x = (i) => padL + pw * (n > 1 ? i / (n - 1) : 0.5);
    const y = (v) => padT + ph * (1 - (v - lo) / (hi - lo));
    ctx.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
    ctx.strokeStyle = COLORS.grid; ctx.fillStyle = COLORS.muted; ctx.lineWidth = 1;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const v = lo + (hi - lo) * i / 4, yy = Math.round(y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke();
      ctx.fillText(v.toFixed(2), padL - 5, yy);
    }
    ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText("subcarrier (valid index order, " + n + ")", padL, padT + ph + 5);
    // std band (gaps at masked subcarriers)
    ctx.fillStyle = "rgba(61,220,151,0.18)";
    let i = 0;
    while (i < n) {
      while (i < n && !(isNum(mean[i]) && isNum(std[i]))) i++;
      const start = i;
      while (i < n && isNum(mean[i]) && isNum(std[i])) i++;
      if (i - start >= 1) {
        ctx.beginPath();
        for (let k = start; k < i; k++) ctx.lineTo(x(k), y(mean[k] + std[k]));
        for (let k = i - 1; k >= start; k--) ctx.lineTo(x(k), y(mean[k] - std[k]));
        ctx.closePath(); ctx.fill();
      }
    }
    const line = (vals, color, width) => {
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
      let pen = false;
      for (let k = 0; k < n; k++) {
        if (!isNum(vals[k])) { pen = false; continue; }
        if (pen) ctx.lineTo(x(k), y(vals[k])); else ctx.moveTo(x(k), y(vals[k]));
        pen = true;
      }
      ctx.stroke();
    };
    line(mean, COLORS.ok, 2);
    line(walk, COLORS.accent, 1.6);
    ctx.textAlign = "right"; ctx.textBaseline = "top";
    ctx.fillStyle = COLORS.accent; ctx.fillText("walking mean", padL + pw - 4, padT + 2);
    ctx.fillStyle = COLORS.ok; ctx.fillText("empty room mean +/- std", padL + pw - 4, padT + 16);
  }

  function readDurations() {
    const out = {};
    ["empty_baseline", "walking", "standing_still", "sitting"].forEach((k) => {
      const v = parseFloat($("dur-" + k).value);
      if (isNum(v)) out[k] = v;
    });
    return out;
  }

  function clampInt(id, lo, hi, dflt) {
    const v = parseInt($(id).value, 10);
    if (!isNum(v)) return dflt;
    return Math.max(lo, Math.min(hi, v));
  }

  function cancelCountdown() {
    if (countdown) { clearInterval(countdown.timer); countdown = null; }
  }

  // Run fn after a visible countdown of `seconds` (0 = immediately).
  function startCountdown(kind, seconds, msgId, label, fn) {
    cancelCountdown();
    if (seconds <= 0) { fn(); return; }
    const endsAt = Date.now() + seconds * 1000;
    const update = () => {
      const left = Math.ceil((endsAt - Date.now()) / 1000);
      if (left <= 0) {
        cancelCountdown();
        setMsg(msgId, "", "");
        fn();
        return;
      }
      setMsg(msgId, label + " starts in " + left + " s", "countdown");
    };
    countdown = { kind: kind, endsAt: endsAt, timer: setInterval(update, 200) };
    update();
  }

  async function refreshCalibration() {
    try { renderCalibration(await api("/api/calibration/state")); } catch (e) { setMsg("calib-msg", "Calibration state unavailable: " + e.message, "error"); }
  }

  async function loadCalibrations() {
    const sel = $("calib-select");
    try {
      const res = await api("/api/calibrations");
      const prev = sel.value;
      sel.innerHTML = "";
      const list = res.calibrations || [];
      if (!list.length) sel.appendChild(new Option("(none saved)", ""));
      list.forEach((cal) => sel.appendChild(new Option(cal.name + "  [" + cal.key + "]", cal.key)));
      if ([].some.call(sel.options, (o) => o.value === prev)) sel.value = prev;
      else if (res.active && [].some.call(sel.options, (o) => o.value === res.active.key)) sel.value = res.active.key;
    } catch (e) {
      setMsg("calib-msg", "Could not list calibrations: " + e.message, "error");
    }
  }

  function bindCalibration() {
    $("calib-start").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      cancelCountdown();
      try {
        renderCalibration((await postJSON("/api/calibration/start", { placement: $("calib-placement").value, durations: readDurations() })).state);
        setMsg("calib-msg", "Calibration started. Follow the instructions for step 1.", "");
      } catch (e) { setMsg("calib-msg", e.message, "error"); }
    }));
    $("calib-step-start").addEventListener("click", () => {
      const s = lastCalib && lastCalib.session;
      const cur = s && s.steps[s.step_index];
      startCountdown("calib", clampInt("calib-leadin", 0, 60, 5), "calib-msg", cur ? cur.title : "Step", async () => {
        try { renderCalibration((await postJSON("/api/calibration/step/start", {})).state); }
        catch (e) { setMsg("calib-msg", e.message, "error"); }
      });
      if (lastCalib) renderCalibration(lastCalib);
    });
    $("calib-step-cancel").addEventListener("click", async () => {
      if (countdown && countdown.kind === "calib") { cancelCountdown(); setMsg("calib-msg", "Step start cancelled.", ""); if (lastCalib) renderCalibration(lastCalib); return; }
      try { renderCalibration((await postJSON("/api/calibration/step/cancel", {})).state); }
      catch (e) { setMsg("calib-msg", e.message, "error"); }
    });
    $("calib-cancel").addEventListener("click", async () => {
      if (!window.confirm("Cancel this calibration? Recorded steps are discarded.")) return;
      cancelCountdown();
      try { renderCalibration((await postJSON("/api/calibration/cancel", {})).state); }
      catch (e) { setMsg("calib-msg", e.message, "error"); }
    });
    $("calib-steps").addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-redo]");
      if (!btn) return;
      try { renderCalibration((await postJSON("/api/calibration/step/redo", { step: btn.getAttribute("data-redo") })).state); }
      catch (e) { setMsg("calib-msg", e.message, "error"); }
    });
    $("calib-save").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      try {
        const res = await postJSON("/api/calibration/save", {});
        setMsg("calib-msg", "Saved " + res.path + " and applied it.", "");
        await loadCalibrations();
        await refreshCalibration();
      } catch (e) { setMsg("calib-msg", e.message, "error"); }
    }));
    $("calib-recalibrate").addEventListener("click", () => $("calib-start").click());
    $("calib-refresh").addEventListener("click", loadCalibrations);
    $("calib-load").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      const key = $("calib-select").value;
      if (!key) { setMsg("calib-msg", "Choose a saved calibration first.", "error"); return; }
      try {
        const res = await postJSON("/api/calibration/load", { key: key });
        setMsg("calib-msg", "Loaded " + key + ((res.warnings || []).length ? " - warning: " + res.warnings.join("; ") : ""), (res.warnings || []).length ? "error" : "");
        await refreshCalibration();
      } catch (e) { setMsg("calib-msg", e.message, "error"); }
    }));
    $("calib-clear").addEventListener("click", async () => {
      try { renderCalibration((await postJSON("/api/calibration/clear", {})).state); setMsg("calib-msg", "Using the adaptive reference and uncalibrated thresholds.", ""); }
      catch (e) { setMsg("calib-msg", e.message, "error"); }
    });
    $("calib-delete").addEventListener("click", async () => {
      const key = $("calib-select").value;
      if (!key) { setMsg("calib-msg", "Choose a saved calibration first.", "error"); return; }
      if (!window.confirm("Delete calibration " + key + "? This removes the file and cannot be undone.")) return;
      try {
        await postJSON("/api/calibration/delete", { key: key, confirm: true });
        setMsg("calib-msg", "Deleted " + key + ".", "");
        await loadCalibrations();
        await refreshCalibration();
      } catch (e) { setMsg("calib-msg", e.message, "error"); }
    });
  }

  // ------------------------------------------------------------ recording
  let lastRec = null;

  function fmtTime(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
  }

  function renderRecording(rs) {
    lastRec = rs;
    const s = rs.session;
    setText("rec-session", s ? s.session_id + (s.notes ? " - " + s.notes : "") : null, "none (create or select one)");
    setText("rec-session-kind", s ? (s.source_kind || "no source yet") + (s.synthetic ? ", SYNTHETIC" : ", real data") : null);
    const take = rs.active_take;
    const busy = !!take || (countdown && countdown.kind === "take");
    [].forEach.call(document.querySelectorAll(".label-btn"), (b) => {
      const isActive = take && take.label === b.getAttribute("data-label");
      b.classList.toggle("active", !!isActive);
      b.disabled = !rs.can_record || (busy && !isActive);
      b.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
    $("rec-stop").disabled = !busy;
    $("rec-create").disabled = !!take;
    $("rec-select").disabled = !!take;
    if (take) {
      setText("rec-timer", fmtNum(take.elapsed_s, 1) + (isNum(take.duration_s) ? " / " + take.duration_s + " s" : " s"));
      setText("rec-packets", take.packets + " packets, " + take.lines + " lines - " + take.label_name);
    } else {
      setText("rec-timer", null);
      setText("rec-packets", null);
    }
    if (!(countdown && countdown.kind === "take")) {
      if (take) setMsg("rec-msg", "Recording " + take.label_name + "...", "");
      else if (!rs.can_record) setMsg("rec-msg", "Recording disabled: " + (rs.reason || "not ready"), "error");
      else if (rs.last_take) setMsg("rec-msg", "Last take: " + rs.last_take.label_name + ", " + rs.last_take.packets + " packets (" + rs.last_take.stop_reason + ")", "");
      else setMsg("rec-msg", "Ready. Pick a label to record a take.", "");
    }
    const rows = (rs.takes || []).slice().reverse().map((t) =>
      "<tr><td class=\"mono\">" + escapeHtml(t.take_id) + "</td><td>" + escapeHtml(t.label_name || t.label) + "</td><td class=\"mono\">" +
      escapeHtml(fmtTime(t.start)) + "</td><td class=\"mono\">" + escapeHtml(fmtNum(t.duration_s, 1)) + " s</td><td class=\"mono\">" +
      escapeHtml(String(t.packets)) + "</td><td>" + escapeHtml(t.stop_reason || "-") + "</td><td class=\"mono\">" + escapeHtml(t.file) +
      "</td><td><button type=\"button\" class=\"btn btn-danger btn-small\" data-delete-take=\"" + escapeHtml(t.take_id) + "\" aria-label=\"Delete take " +
      escapeHtml(t.take_id) + "\">Delete</button></td></tr>").join("");
    setHTML($("rec-takes"), rows || "<tr><td colspan=\"8\" class=\"muted\">No takes yet</td></tr>");
  }

  async function refreshRecording() {
    try { renderRecording(await api("/api/recording")); } catch (e) { setMsg("rec-msg", "Recording state unavailable: " + e.message, "error"); }
  }

  async function loadSessions() {
    const sel = $("rec-session-select");
    try {
      const res = await api("/api/sessions");
      const prev = sel.value;
      sel.innerHTML = "";
      const list = res.sessions || [];
      if (!list.length) sel.appendChild(new Option("(no sessions)", ""));
      list.forEach((s) => sel.appendChild(new Option(s.session_id + "  [" + s.n_takes + " takes" + (s.synthetic ? ", SYNTHETIC" : "") + "]", s.session_id)));
      if ([].some.call(sel.options, (o) => o.value === prev)) sel.value = prev;
      else if (res.current) sel.value = res.current.session_id;
    } catch (e) {
      setMsg("rec-msg", "Could not list sessions: " + e.message, "error");
    }
  }

  function bindRecording() {
    $("rec-create").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      try {
        const res = await postJSON("/api/session/create", { placement: $("rec-placement").value, notes: $("rec-notes").value });
        setMsg("rec-msg", "Created session " + res.session.session_id, "");
        await loadSessions(); await refreshRecording(); loadReplayFiles();
      } catch (e) { setMsg("rec-msg", e.message, "error"); }
    }));
    $("rec-select").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      const id = $("rec-session-select").value;
      if (!id) { setMsg("rec-msg", "Choose a session first.", "error"); return; }
      try { await postJSON("/api/session/select", { session_id: id }); await refreshRecording(); }
      catch (e) { setMsg("rec-msg", e.message, "error"); }
    }));
    $("rec-sessions-refresh").addEventListener("click", loadSessions);
    $("rec-labels").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-label]");
      if (!btn || btn.disabled) return;
      if (btn.classList.contains("active")) { $("rec-stop").click(); return; }
      const label = btn.getAttribute("data-label");
      const dur = clampInt("rec-duration", 0, 3600, 0);
      startCountdown("take", clampInt("rec-countdown", 0, 30, 3), "rec-msg", btn.textContent, async () => {
        try { await postJSON("/api/take/start", { label: label, duration: dur > 0 ? dur : null }); }
        catch (e) { setMsg("rec-msg", e.message, "error"); }
        await refreshRecording();
      });
      if (lastRec) renderRecording(lastRec);
    });
    $("rec-stop").addEventListener("click", async () => {
      if (countdown && countdown.kind === "take") { cancelCountdown(); setMsg("rec-msg", "Take cancelled before it started.", ""); if (lastRec) renderRecording(lastRec); return; }
      try { await postJSON("/api/take/stop", {}); await refreshRecording(); loadReplayFiles(); }
      catch (e) { setMsg("rec-msg", e.message, "error"); }
    });
    $("rec-takes").addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-delete-take]");
      if (!btn) return;
      const id = btn.getAttribute("data-delete-take");
      if (!window.confirm("Delete take " + id + " and its CSV file? This cannot be undone.")) return;
      try { await postJSON("/api/take/delete", { take_id: id, confirm: true }); await refreshRecording(); loadReplayFiles(); }
      catch (e) { setMsg("rec-msg", e.message, "error"); }
    });
  }

  // ------------------------------------------------------------ alerts
  let lastAlertId = null;

  function fmtClock(ts) {
    if (!isNum(ts)) return "-";
    return new Date(ts * 1000).toLocaleTimeString();
  }

  function renderAlerts(al, det) {
    al = al || {}; det = det || {};
    const box = $("alert-box");
    const a = al.alert || null;
    const state = a ? a.state : "idle";
    setClass(box, "alert-box", "alert-" + state);
    const ev = a && a.event ? a.event : null;
    $("alert-synthetic").classList.toggle("hidden", !(a && a.synthetic));
    let title = "No active alert";
    let summary = "The heuristic needs a sudden motion spike followed by sustained stillness with good data quality. Stillness alone never raises an alert.";
    if (state === "active") {
      title = "POTENTIAL FALL DETECTED";
      summary = (ev && ev.summary ? ev.summary + ". " : "") + "Triggered at " + fmtClock(a.triggered_at) + ". Cancel if everything is fine.";
    } else if (state === "notified") {
      title = "Potential fall - simulated caregiver notification sent";
      summary = (ev && ev.summary ? ev.summary + ". " : "") + "Not cancelled within " + a.countdown_s + " s.";
    } else if (state === "cancelled") {
      title = a.cancelled_after_notification ? "Alert dismissed" : "Alert cancelled";
      summary = "Alert " + a.alert_id + " from " + fmtClock(a.triggered_at) + " was " + (a.cancelled_after_notification ? "dismissed" : "cancelled") + " at " + fmtClock(a.cancelled_at) + ". Monitoring continues.";
    }
    setText("alert-title", title);
    setText("alert-summary", summary);
    const counting = state === "active";
    $("alert-countdown-row").classList.toggle("hidden", !counting);
    if (counting) {
      $("alert-progress").value = isNum(a.progress) ? a.progress : 0;
      setText("alert-countdown", isNum(a.remaining_s) ? "simulated caregiver notification in " + Math.ceil(a.remaining_s) + " s" : "-");
    }
    const note = $("alert-notification");
    note.classList.toggle("hidden", !(a && a.notification));
    if (a && a.notification) setText("alert-notification", a.notification);
    const cancel = $("alert-cancel");
    cancel.classList.toggle("hidden", !(state === "active" || state === "notified"));
    cancel.textContent = state === "notified" ? "Dismiss alert" : "Cancel alert";
    const crit = ev && ev.criteria ? ev.criteria : [];
    $("alert-criteria-wrap").classList.toggle("hidden", !crit.length);
    if (a && a.alert_id !== lastAlertId) {
      lastAlertId = a.alert_id;
      const rows = crit.map((c) => {
        const unit = c.unit ? " " + c.unit : "";
        return "<tr><td class=\"mono\">" + escapeHtml(c.name) + "</td><td class=\"mono\">" + escapeHtml(fmtNum(c.value, 2) + unit) +
          "</td><td class=\"mono\">" + escapeHtml(c.comparison + " " + fmtNum(c.threshold, 2) + unit) + "</td><td class=\"" +
          (c.passed ? "pass\">PASS" : "fail\">FAIL") + "</td><td>" + escapeHtml(c.description) + "</td></tr>";
      }).join("");
      setHTML($("alert-criteria"), rows);
    }

    // detector
    const db = $("detector-state");
    const dstate = det.state || "-";
    db.textContent = "Detector: " + (det.state_text || dstate);
    setClass(db, "badge", dstate === "spike" || dstate === "stillness" ? "badge-warn" : (dstate === "monitoring" ? "badge-ok" : "badge-muted"));
    setText("det-state", det.state_text);
    const th = det.thresholds || null;
    setText("det-spike", th ? fmtNum(th.spike) + " (" + th.spike_source + ", rise >= " + fmtNum(th.min_rise) + ")" : null);
    setText("det-still", th ? fmtNum(th.stillness) + " (" + th.stillness_source + ")" + (th.calibrated ? " calibrated" : " uncalibrated") : null);
    const c = det.candidate;
    setText("det-candidate", c ? "peak " + fmtNum(c.peak_score) + ", " + (isNum(c.stillness_elapsed_s) ? "still " + fmtNum(c.stillness_elapsed_s, 1) + " / " + c.stillness_needed_s + " s" : fmtNum(c.since_spike_s, 1) + " s since spike (stillness must start within " + c.max_gap_s + " s)") :
      (isNum(det.refractory_remaining_s) ? "refractory, " + det.refractory_remaining_s + " s left" : null), "none");
    setText("det-rejection", det.last_rejection ? det.last_rejection.reason : null, "none");
    setText("det-count", isNum(det.events_detected) ? det.events_detected : null);
  }

  function renderEvents(res) {
    const list = (res && res.events) || [];
    const rows = list.map((e) => "<tr class=\"ev-" + escapeHtml(e.level === "info" ? e.kind : e.level) + "\"><td>" +
      escapeHtml(fmtClock(e.ts)) + "</td><td>" + escapeHtml(e.kind) + "</td><td>" + escapeHtml(e.message) + "</td></tr>").join("");
    setHTML($("event-rows"), rows || "<tr><td colspan=\"3\" class=\"muted\">No events yet</td></tr>");
  }

  const DET_FIELDS = ["spike_threshold", "stillness_threshold", "stillness_duration_s", "max_gap_s", "rise_window_s"];

  async function loadDetectorConfig() {
    try {
      const res = await api("/api/detector/config");
      DET_FIELDS.forEach((k) => { $("det-" + k).value = res.fall[k]; });
      $("det-countdown_s").value = res.alerts.countdown_s;
      $("det-enabled").checked = !!res.fall.enabled;
    } catch (e) { setMsg("det-msg", "Settings unavailable: " + e.message, "error"); }
  }

  function bindAlerts() {
    $("alert-cancel").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      try {
        await postJSON("/api/alerts/cancel", {});
        setText("alert-msg", "Alert cancelled.");
      } catch (e) { setText("alert-msg", "Cancel failed: " + e.message); }
    }));
    $("det-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = { enabled: $("det-enabled").checked };
      let bad = null;
      DET_FIELDS.concat(["countdown_s"]).forEach((k) => {
        const v = parseFloat($("det-" + k).value);
        if (!isNum(v)) bad = k; else body[k] = v;
      });
      if (bad) { setMsg("det-msg", "Enter a number for " + bad, "error"); return; }
      try {
        const res = await postJSON("/api/detector/config", body);
        setMsg("det-msg", "Applied" + (res.saved_to ? " and saved to data/config.json" : "") + ".", "");
      } catch (e) { setMsg("det-msg", e.message, "error"); }
    });
  }

  // ------------------------------------------------------------ simulation
  const SIM_SLIDERS = [["sim-noise", 2], ["sim-strength", 2], ["sim-duration", 1], ["sim-rate", 0]];
  const SIM_DEFAULTS = { "sim-noise": 0.03, "sim-strength": 1, "sim-duration": 1, "sim-rate": 100 };

  function updateSliderOutputs() {
    SIM_SLIDERS.forEach((s) => { setText(s[0] + "-out", parseFloat($(s[0]).value).toFixed(s[1])); });
  }

  function bindSimulation() {
    SIM_SLIDERS.forEach((s) => $(s[0]).addEventListener("input", updateSliderOutputs));
    $("sim-reset").addEventListener("click", () => {
      Object.keys(SIM_DEFAULTS).forEach((k) => { $(k).value = SIM_DEFAULTS[k]; });
      updateSliderOutputs();
    });
    $("sim-start").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      const body = {
        kind: "simulation", scenario: $("sim-scenario").value,
        noise: parseFloat($("sim-noise").value), motion_strength: parseFloat($("sim-strength").value),
        event_duration_s: parseFloat($("sim-duration").value), rate_hz: parseFloat($("sim-rate").value),
      };
      try {
        await postJSON("/api/source", body);
        showMessage("Simulation '" + body.scenario + "' started (SYNTHETIC data).", false);
      } catch (e) { showMessage("Simulation failed: " + e.message, true); }
    }));
    updateSliderOutputs();
  }

  // ------------------------------------------------------------ export
  async function loadExports() {
    try {
      const res = await api("/api/exports");
      const list = res.exports || [];
      const items = list.slice(0, 20).map((x) => "<li><a href=\"/api/exports/" + encodeURIComponent(x.name) + "\" download>" +
        escapeHtml(x.name) + "</a> <span class=\"muted\">" + Math.round(x.size / 1024) + " kB" + (x.synthetic ? ", SYNTHETIC" : "") + "</span></li>").join("");
      setHTML($("export-list"), items || "<li class=\"muted\">No exports yet</li>");
    } catch (e) { setText("export-msg", "Could not list exports: " + e.message); }
    try {
      const res = await api("/api/replay_files");
      const sel = $("export-session");
      const prev = sel.value;
      sel.innerHTML = "";
      const files = (res.files || []).filter((f) => f.kind === "session" || f.kind === "take");
      if (!files.length) sel.appendChild(new Option("(no recordings)", ""));
      files.forEach((f) => {
        const text = f.kind === "session" ? f.session_id + " (all takes)" : f.session_id + " / " + f.name;
        sel.appendChild(new Option(text + (f.synthetic ? " [SYNTHETIC]" : ""), f.path));
      });
      if ([].some.call(sel.options, (o) => o.value === prev)) sel.value = prev;
    } catch (e) { /* replay list errors are shown in the replay panel */ }
  }

  async function doExport(body) {
    setText("export-msg", "Exporting...");
    try {
      const res = await postJSON("/api/export", body);
      setText("export-msg", "Exported " + res.export.rows + " packets to data/exports/" + res.export.name + (res.export.synthetic ? " (SYNTHETIC)" : ""));
      await loadExports();
    } catch (e) { setText("export-msg", "Export failed: " + e.message); }
  }

  function bindExport() {
    $("export-buffer").addEventListener("click", (ev) => withButton(ev.currentTarget, () => doExport({ what: "buffer" })));
    $("export-session-btn").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      const path = $("export-session").value;
      if (!path) { setText("export-msg", "Choose a recording first."); return; }
      await doExport({ what: "session", path: path });
    }));
    $("export-refresh").addEventListener("click", loadExports);
  }

  // ------------------------------------------------------------ polling
  let lastSeries = null, lastHeat = null;

  async function tick() {
    const state = $("server-state");
    try {
      const results = await Promise.all([
        api("/api/status"),
        api("/api/series?seconds=" + SERIES_SECONDS),
        api("/api/heatmap?n=" + HEATMAP_ROWS),
      ]);
      renderStatus(results[0]);
      renderAlerts(results[0].alert, results[0].detector);
      lastSeries = results[1]; lastHeat = results[2];
      renderSeries(lastSeries);
      drawHeatmap(lastHeat);
      state.textContent = "Backend OK"; setClass(state, "pill", "pill-ok");
      // calibration / recording / events (secondary; failures are shown in their panels)
      const extra = await Promise.all([
        api("/api/calibration/state").catch(() => null),
        api("/api/recording").catch(() => null),
        api("/api/events?n=60").catch(() => null),
      ]);
      if (extra[0]) renderCalibration(extra[0]);
      if (extra[1]) renderRecording(extra[1]);
      if (extra[2]) renderEvents(extra[2]);
    } catch (e) {
      state.textContent = "Backend unreachable: " + (e && e.message ? e.message : e);
      setClass(state, "pill", "pill-bad");
    } finally {
      setTimeout(tick, POLL_MS);
    }
  }

  // ------------------------------------------------------------ controls
  async function loadPorts() {
    const sel = $("port-select");
    try {
      const res = await api("/api/ports");
      const prev = sel.value;
      sel.innerHTML = "";
      sel.appendChild(new Option("auto (detect receiver)", "auto"));
      (res.ports || []).forEach((p) => {
        const tag = p.likely_esp32 ? "likely ESP32" : (p.excluded ? "system port" : "other");
        const opt = new Option(p.device + "  [" + tag + (p.description ? ", " + p.description : "") + "]", p.device);
        opt.disabled = !!p.excluded;
        sel.appendChild(opt);
      });
      if ([].some.call(sel.options, (o) => o.value === prev)) sel.value = prev;
      const n = (res.ports || []).filter((p) => p.likely_esp32 && !p.excluded).length;
      showMessage(res.error ? res.error : (n ? n + " likely ESP32 port(s) found." : "No likely ESP32 port found. Check the USB cable."), !!res.error);
    } catch (e) {
      showMessage("Could not list ports: " + e.message, true);
    }
  }

  async function loadReplayFiles() {
    const sel = $("replay-select");
    try {
      const res = await api("/api/replay_files");
      const prev = sel.value;
      sel.innerHTML = "";
      const files = res.files || [];
      if (!files.length) sel.appendChild(new Option("(no files found)", ""));
      const groups = [
        ["sessions", "Sessions (all takes)"], ["takes", "Labeled takes"],
        ["recordings", "Other recordings"], ["fixtures", "Format fixtures (not real captures)"],
      ];
      groups.forEach((g) => {
        const items = files.filter((f) => f.group === g[0]);
        if (!items.length) return;
        const og = document.createElement("optgroup");
        og.label = g[1];
        items.forEach((f) => {
          let text = f.path;
          if (f.kind === "session") text = f.session_id + "  [" + f.takes + " takes" + (f.synthetic ? ", SYNTHETIC" : "") + "]";
          else if (f.kind === "take") text = f.session_id + " / " + f.name + "  [" + (f.label_name || f.label) + (f.synthetic ? ", SYNTHETIC" : "") + "]";
          og.appendChild(new Option(text, f.path));
        });
        sel.appendChild(og);
      });
      if ([].some.call(sel.options, (o) => o.value === prev)) sel.value = prev;
    } catch (e) {
      showMessage("Could not list replay files: " + e.message, true);
    }
  }

  async function withButton(btn, fn) {
    btn.disabled = true;
    try { await fn(); } finally { btn.disabled = false; }
  }

  const TABS = ["serial", "replay", "simulation", "calibration", "record"];

  function selectTab(which) {
    TABS.forEach((name) => {
      const tab = $("tab-" + name), panel = $("panel-" + name);
      const on = name === which;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      panel.classList.toggle("hidden", !on);
    });
    if (which === "calibration") { loadCalibrations(); if (lastCalib) renderCalibration(lastCalib); }
    if (which === "record") loadSessions();
    if (which === "replay") loadReplayFiles();
  }

  function bindControls() {
    TABS.forEach((name) => $("tab-" + name).addEventListener("click", () => selectTab(name)));
    $("ports-refresh").addEventListener("click", loadPorts);
    $("replay-refresh").addEventListener("click", loadReplayFiles);

    $("serial-connect").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      const baud = parseInt($("baud-input").value, 10);
      if (!(baud >= 9600 && baud <= 3000000)) { showMessage("Baud must be between 9600 and 3000000.", true); return; }
      try {
        await postJSON("/api/source", { kind: "serial", port: $("port-select").value, baud: baud });
        showMessage("Opening " + $("port-select").value + " (read-only)...", false);
      } catch (e) { showMessage("Connect failed: " + e.message, true); }
    }));

    $("replay-start").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      const path = $("replay-select").value;
      if (!path) { showMessage("Choose a recording first.", true); return; }
      try {
        await postJSON("/api/source", {
          kind: "replay", path: path, speed: parseFloat($("replay-speed").value), loop: $("replay-loop").checked,
        });
        showMessage("Replaying " + path, false);
      } catch (e) { showMessage("Replay failed: " + e.message, true); }
    }));

    $("source-stop").addEventListener("click", (ev) => withButton(ev.currentTarget, async () => {
      try { await postJSON("/api/stop", {}); showMessage("Source stopped.", false); }
      catch (e) { showMessage("Stop failed: " + e.message, true); }
    }));

    window.addEventListener("resize", () => {
      if (lastSeries) renderSeries(lastSeries);
      if (lastHeat) drawHeatmap(lastHeat);
      if (lastCalib) renderCalibration(lastCalib);
    });
    bindCalibration();
    bindRecording();
    bindAlerts();
    bindSimulation();
    bindExport();
  }

  document.addEventListener("DOMContentLoaded", () => {
    drawLegend();
    bindControls();
    loadPorts();
    loadReplayFiles();
    loadCalibrations();
    loadSessions();
    loadDetectorConfig();
    loadExports();
    tick();
  });
})();
