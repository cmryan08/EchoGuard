// Temporary smoke test (deleted after use): run app.js against stubbed DOM/canvas
// with real API payloads to catch runtime errors in the render code.
"use strict";
const fs = require("fs");
const path = require("path");
const payloads = JSON.parse(fs.readFileSync(path.join(__dirname, "tmp_payloads.json"), "utf8"));

function ctxStub() {
  return new Proxy({}, {
    get(target, prop) {
      if (prop === "createImageData") return (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) });
      if (prop === "measureText") return () => ({ width: 10 });
      if (prop in target) return target[prop];
      return () => {};
    },
    set(target, prop, value) { target[prop] = value; return true; },
  });
}
const elements = {};
function el(id) {
  if (!elements[id]) {
    const ctx = ctxStub();
    elements[id] = {
      id, textContent: "", innerHTML: "", className: "", value: "", checked: false, disabled: false,
      style: {}, width: 300, height: 150, clientWidth: 600, clientHeight: 200, options: [],
      classList: { toggle() {} }, setAttribute() {}, addEventListener() {},
      appendChild(o) { this.options.push(o); }, getContext: () => ctx,
    };
  }
  return elements[id];
}
const listeners = {};
global.window = { devicePixelRatio: 2, addEventListener() {} };
global.document = {
  getElementById: el,
  createElement: () => el("__offscreen" + Math.random()),
  addEventListener: (ev, fn) => { listeners[ev] = fn; },
  body: {},
};
global.getComputedStyle = () => ({ getPropertyValue: () => "monospace" });
global.Option = function (text, value) { this.text = text; this.value = value; };
let scenario = "empty";
global.fetch = async (url) => {
  const key = url.split("?")[0];
  const data = payloads[scenario][key];
  return { ok: true, status: 200, json: async () => data };
};
const timers = [];
global.setTimeout = (fn) => { timers.push(fn); return timers.length; };
global.clearTimeout = () => {};
global.AbortController = class { constructor() { this.signal = {}; } abort() {} };

require(path.join(__dirname, "..", "dashboard", "static", "app.js"));

async function runTick() {
  // the poll loop schedules itself via setTimeout; run queued callbacks
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
}

(async () => {
  const errors = [];
  process.on("unhandledRejection", (e) => errors.push(e));
  listeners.DOMContentLoaded();
  await runTick();
  console.log("empty:", el("server-state").textContent, "|", el("rate").textContent, "|", el("activity-badge").textContent, "|", el("quality-badge").textContent);
  scenario = "replay";
  const pending = timers.splice(0);
  pending.forEach((fn) => fn());
  await runTick();
  console.log("replay:", el("server-state").textContent, "|", el("rate").textContent, "|", el("rssi").textContent, "|", el("source-banner").textContent, "|", el("cfg-bw").textContent, "|", el("cfg-secondary").textContent);
  console.log("quality reasons html:", el("quality-reasons").innerHTML.slice(0, 120));
  if (errors.length) { console.error("ERRORS", errors); process.exit(1); }
  if (!el("server-state").textContent.startsWith("Backend OK")) { console.error("tick failed"); process.exit(1); }
  console.log("JS smoke OK");
})();
