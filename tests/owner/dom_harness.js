/* Browserless renderer for the owner console tests (spec 11.1: "a browserless test renders an XSS fixture
 * event file and checks no script node is created").
 *
 * usage: node dom_harness.js <app.js> <index.html> <scenario.json>
 *
 * A minimal DOM: elements, text nodes, attributes, event listeners. The HTML-parsing APIs are traps that
 * record a violation and throw, and setAttribute records handler attributes, style attributes and
 * javascript: URLs. app.js is loaded with window.ACB_TEST = true (no auto start), then the scenario is
 * rendered: renderAll(snapshot, items), every chat response, every live item, every card.
 * Prints one JSON object with the panel texts, the tags created and the violations.
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const [appPath, indexPath, scenarioPath] = process.argv.slice(2);
const violations = [];
const created = [];

class Node {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get firstChild() { return this.childNodes.length ? this.childNodes[0] : null; }
  get lastChild() { return this.childNodes.length ? this.childNodes[this.childNodes.length - 1] : null; }
  appendChild(child) {
    if (!(child instanceof Node)) { throw new TypeError("appendChild needs a node"); }
    if (child.parentNode) { child.parentNode.removeChild(child); }
    this.childNodes.push(child);
    child.parentNode = this;
    return child;
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i < 0) { throw new Error("not a child"); }
    this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(value) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = [];
    const text = value === null || value === undefined ? "" : String(value);
    if (text !== "") { this.appendChild(new Text(text)); }
  }
}

class Text extends Node {
  constructor(data) { super(); this.data = String(data); this.nodeType = 3; }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
  appendChild() { throw new Error("text nodes have no children"); }
}

class Element extends Node {
  constructor(tag) {
    super();
    this.tagName = String(tag).toUpperCase();
    this.nodeType = 1;
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
  }
  setAttribute(name, value) {
    const n = String(name).toLowerCase();
    const v = String(value);
    if (/^on/.test(n)) { violations.push("handler attribute " + n); }
    if (n === "style") { violations.push("style attribute"); }
    if ((n === "href" || n === "src" || n === "action") && /^\s*javascript:/i.test(v)) { violations.push("javascript url in " + n); }
    if (n === "src" || n === "srcdoc") { violations.push("attribute " + n); }
    this.attributes[n] = v;
  }
  getAttribute(name) { const v = this.attributes[String(name).toLowerCase()]; return v === undefined ? null : v; }
  removeAttribute(name) { delete this.attributes[String(name).toLowerCase()]; }
  get className() { return this.attributes["class"] || ""; }
  set className(v) { this.attributes["class"] = String(v); }
  get id() { return this.attributes["id"] || ""; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type) { (this.listeners[type] || []).forEach((fn) => fn({preventDefault() {}, target: this})); }
  get innerHTML() { return this.textContent; }
  set innerHTML(v) { violations.push("innerHTML"); throw new Error("innerHTML is forbidden"); }
  get outerHTML() { return ""; }
  set outerHTML(v) { violations.push("outerHTML"); throw new Error("outerHTML is forbidden"); }
  insertAdjacentHTML() { violations.push("insertAdjacentHTML"); throw new Error("insertAdjacentHTML is forbidden"); }
  all() {
    const out = [];
    const walk = (n) => { n.childNodes.forEach((c) => { if (c instanceof Element) { out.push(c); walk(c); } }); };
    walk(this);
    return out;
  }
}

const ids = {};
const html = fs.readFileSync(indexPath, "utf8");
const idRe = /<([a-zA-Z0-9]+)\b[^>]*\sid="([^"]+)"/g;
let m;
while ((m = idRe.exec(html)) !== null) {
  const e = new Element(m[1]);
  e.attributes.id = m[2];
  ids[m[2]] = e;
}

const document = {
  readyState: "complete",
  createElement(tag) { const e = new Element(tag); created.push(e); return e; },
  createTextNode(data) { return new Text(data); },
  getElementById(id) { return Object.prototype.hasOwnProperty.call(ids, id) ? ids[id] : null; },
  querySelector() { return null; },
  addEventListener() {},
  write() { violations.push("document.write"); throw new Error("document.write is forbidden"); },
  writeln() { violations.push("document.writeln"); throw new Error("document.writeln is forbidden"); }
};

globalThis.window = globalThis;
globalThis.document = document;
globalThis.ACB_TEST = true;
globalThis.setTimeout = () => 0;
globalThis.fetch = () => Promise.reject(new Error("no network in the harness"));
globalThis.confirm = () => false;
globalThis.location = {reload() {}};
globalThis.__pwned = 0;

vm.runInThisContext(fs.readFileSync(appPath, "utf8"), {filename: "app.js"});
const ACB = globalThis.ACB_OWNER;
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
const errors = [];

function attempt(label, fn) {
  try { fn(); } catch (err) { errors.push(label + ": " + (err && err.message ? err.message : String(err))); }
}

attempt("renderAll", () => ACB.renderAll(scenario.snapshot || null, scenario.items || []));
(scenario.chat || []).forEach((r, i) => attempt("chat " + i, () => ACB.handleChatResponse(r)));
(scenario.live || []).forEach((item, i) => attempt("live " + i, () => ACB.handleItem(item)));
(scenario.cards || []).forEach((card, i) => attempt("card " + i, () => {
  const node = ACB.renderCard(card);
  if (node) { ids["chat-log"].appendChild(node); }
}));

const panels = {};
["wallet-body", "wallet-actions", "grades-body", "decisions-body", "autonomy-body", "messages-body", "chat-log",
  "banner-network", "banner-llm", "banner-owner", "banner-feeds"].forEach((id) => {
  panels[id] = ids[id] ? ids[id].textContent : null;
});
const tags = {};
created.forEach((e) => { tags[e.tagName] = (tags[e.tagName] || 0) + 1; });
const links = [];
Object.values(ids).forEach((root) => root.all().forEach((e) => {
  if (e.tagName === "A") { links.push(e.getAttribute("href")); }
}));
const buttons = [];
Object.values(ids).forEach((root) => root.all().forEach((e) => {
  if (e.tagName === "BUTTON") { buttons.push({text: e.textContent, disabled: !!e.disabled}); }
}));

process.stdout.write(JSON.stringify({
  panels, tags, links, buttons, violations, errors,
  fallbackHidden: ids["banner-fallback"] ? ids["banner-fallback"].hidden : null,
  pwned: globalThis.__pwned,
  scriptNodes: created.filter((e) => ["SCRIPT", "IFRAME", "OBJECT", "EMBED", "IMG", "SVG", "STYLE"].includes(e.tagName)).length
}));
