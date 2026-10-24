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
    if (src.kind === "simulation" || src.synthetic) {
      banner.textContent = "SIMULATED DATA - synthetic, not evidence of real-world accuracy";
      setClass(banner, "banner", "banner-sim");
    } else if (src.kind === "replay") {
      banner.textContent = "REPLAY of recorded data - not a live measurement";
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
    setText("cfg-calib", cal.active ? (cal.name || "active") : "none (adaptive reference)");
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
    setText("motion-th", isNum(th.movement) ? "movement " + th.movement + ", rapid " + th.rapid + (th.calibrated ? " (calibrated)" : " (uncalibrated)") : null);
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
        { value: th.movement, color: COLORS.accent, label: "movement" },
        { value: th.rapid, color: COLORS.warn, label: "rapid" },
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
      lastSeries = results[1]; lastHeat = results[2];
      renderSeries(lastSeries);
      drawHeatmap(lastHeat);
      state.textContent = "Backend OK"; setClass(state, "pill", "pill-ok");
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
      files.forEach((f) => sel.appendChild(new Option(f.path + (f.group === "fixtures" ? "  [format fixture]" : ""), f.path)));
      if ([].some.call(sel.options, (o) => o.value === prev)) sel.value = prev;
    } catch (e) {
      showMessage("Could not list replay files: " + e.message, true);
    }
  }

  async function withButton(btn, fn) {
    btn.disabled = true;
    try { await fn(); } finally { btn.disabled = false; }
  }

  function selectTab(which) {
    ["serial", "replay"].forEach((name) => {
      const tab = $("tab-" + name), panel = $("panel-" + name);
      const on = name === which;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      panel.classList.toggle("hidden", !on);
    });
  }

  function bindControls() {
    $("tab-serial").addEventListener("click", () => selectTab("serial"));
    $("tab-replay").addEventListener("click", () => selectTab("replay"));
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
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    drawLegend();
    bindControls();
    loadPorts();
    loadReplayFiles();
    tick();
  });
})();
