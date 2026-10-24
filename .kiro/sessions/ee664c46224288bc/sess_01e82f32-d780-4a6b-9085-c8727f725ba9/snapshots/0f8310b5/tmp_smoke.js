// Temporary smoke test: run app.js against stubbed DOM and recorded API payloads.
const fs = require("fs");
const payloads = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const elements = {};
function makeEl(id) {
  const cl = new Set();
  const el = {
    id, value: "1", checked: true, disabled: false, textContent: "", innerHTML: "", className: "", style: {},
    width: 100, height: 100, clientWidth: 300, clientHeight: 150, options: [],
    classList: { add: (c) => cl.add(c), remove: (c) => cl.delete(c), toggle: (c, on) => (on === undefined ? (cl.has(c) ? cl.delete(c) : cl.add(c)) : on ? cl.add(c) : cl.delete(c)), contains: (c) => cl.has(c) },
    setAttribute() {}, getAttribute() { return null; }, addEventListener() {}, appendChild(o) { this.options.push(o); },
    getContext() { return new Proxy({}, { get: (t, k) => (k === "measureText" ? () => ({ width: 10 }) : k === "createImageData" ? (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }) : typeof t[k] !== "undefined" ? t[k] : () => {}), set: (t, k, v) => { t[k] = v; return true; } }); },
  };
  return el;
}
global.document = {
  getElementById: (id) => elements[id] || (elements[id] = makeEl(id)),
  createElement: () => makeEl("x"), querySelectorAll: () => [], addEventListener: (ev, fn) => { global.__ready = fn; },
  body: {},
};
global.window = { devicePixelRatio: 1, addEventListener() {}, confirm: () => true };
global.getComputedStyle = () => ({ getPropertyValue: () => "monospace" });
global.Option = function (t, v) { this.text = t; this.value = v; };
let ticks = 0;
global.setTimeout = (fn) => { if (ticks++ < 3) Promise.resolve().then(fn); return 0; };
global.clearTimeout = () => {};
global.setInterval = () => 0; global.clearInterval = () => {};
global.AbortController = function () { this.signal = {}; this.abort = () => {}; };
global.fetch = async (path) => {
  const key = Object.keys(payloads).find((k) => path.startsWith(k));
  return { ok: true, status: 200, json: async () => payloads[key] || {} };
};
require(require("path").resolve("dashboard/static/app.js"));
global.__ready();
setTimeout(() => {});
process.on("exit", () => {
  console.log("server-state:", elements["server-state"].textContent);
  console.log("alert-title:", elements["alert-title"].textContent);
  console.log("alert-countdown:", elements["alert-countdown"].textContent);
  console.log("criteria rows:", (elements["alert-criteria"].innerHTML.match(/<tr>/g) || []).length);
  console.log("events:", (elements["event-rows"].innerHTML.match(/<tr/g) || []).length);
  console.log("banner:", elements["source-banner"].textContent);
});
