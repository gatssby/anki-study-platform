const assert = require("assert");
const path = require("path");

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }
  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }
  setItem(key, value) {
    this.values.set(key, String(value));
  }
  removeItem(key) {
    this.values.delete(key);
  }
}

class Element {}
class HTMLFormElement extends Element {}
class HTMLDetailsElement extends Element {}

global.Element = Element;
global.HTMLFormElement = HTMLFormElement;
global.HTMLDetailsElement = HTMLDetailsElement;

const listeners = {};
let moduleDetails = [];
const storage = new MemoryStorage();
const scrollCalls = [];

global.document = {
  readyState: "loading",
  documentElement: { scrollHeight: 1000 },
  body: { scrollHeight: 1000 },
  addEventListener(type, callback) {
    listeners[type] = listeners[type] || [];
    listeners[type].push(callback);
  },
  querySelector(selector) {
    return selector === "[data-un-modules]" ? {} : null;
  },
  querySelectorAll(selector) {
    return selector === "[data-un-module-details][data-un-module-id]" ? moduleDetails : [];
  },
};

global.window = {
  location: {
    origin: "https://cronograma.test",
    pathname: "/",
    search: "?date=2026-07-10&fo_view=tudo",
  },
  innerHeight: 600,
  scrollY: 750,
  pageYOffset: 750,
  sessionStorage: storage,
  history: { scrollRestoration: "auto" },
  requestAnimationFrame(callback) {
    callback();
  },
  setTimeout(callback) {
    callback();
  },
  scrollTo(_x, y) {
    scrollCalls.push(y);
    this.scrollY = y;
  },
};

require(path.resolve(__dirname, "../app/static/scroll-preserve.js"));
const api = window.CronogramaScrollPreserve;
assert(api, "API de preservação não exposta");

class ActionForm extends HTMLFormElement {
  constructor(returnTo, options = {}) {
    super();
    this.method = "post";
    this.returnTo = returnTo;
    this.preserveScroll = options.preserveScroll !== false;
    this.preserveModules = Boolean(options.preserveModules);
  }
  querySelector(selector) {
    return selector === 'input[name="return_to"]' ? { value: this.returnTo } : null;
  }
  matches(selector) {
    if (selector === "[data-preserve-scroll]") return this.preserveScroll;
    if (selector === "[data-preserve-un-modules]") return this.preserveModules;
    return false;
  }
  closest() {
    return null;
  }
}

const submitListener = listeners.submit[0];
assert(submitListener, "listener de submit ausente");
for (const actionName of ["fo", "un-toggle", "un-module", "exercise-status", "exercise-reschedule"]) {
  const targetPath = `/?date=2026-07-10&fo_view=tudo&action=${actionName}`;
  submitListener({
    target: new ActionForm(targetPath, { preserveModules: actionName.startsWith("un-") }),
  });
  const saved = JSON.parse(storage.getItem("cronograma_scroll_restore"));
  assert.strictEqual(saved.path, targetPath, `scroll não salvo para ${actionName}`);
  assert.strictEqual(saved.scrollY, window.scrollY);
}

function details(id, open) {
  const item = new HTMLDetailsElement();
  item.id = id;
  item.open = open;
  item.getAttribute = (name) => (name === "data-un-module-id" ? item.id : null);
  item.matches = (selector) => selector === "[data-un-module-details]";
  return item;
}

moduleDetails = [details("modulo-a", true), details("modulo-b", true)];
api.saveUnOpenModules();
moduleDetails = [details("modulo-a", false)];
api.restoreUnOpenModules();
assert.strictEqual(moduleDetails[0].open, true, "módulo remanescente não foi reaberto");

window.scrollY = 900;
window.pageYOffset = 900;
api.saveScrollPosition();
api.restoreScrollPosition();
assert.strictEqual(scrollCalls.at(-1), 400, "scroll não foi limitado à posição disponível mais próxima");

api.bindUnModuleState();
const clickListener = listeners.click.at(-1);
const openDetails = details("modulo-controle", true);
const summary = {
  closest(selector) {
    return selector === "[data-un-module-details]" ? openDetails : null;
  },
};
const control = new Element();
control.closest = (selector) => {
  if (selector === "[data-un-module-details] summary") return summary;
  if (selector === "button, input, select, textarea, label, a") return control;
  return null;
};
const queuedTimeouts = [];
window.setTimeout = (callback) => queuedTimeouts.push(callback);
clickListener({ target: control });
openDetails.open = false;
queuedTimeouts.forEach((callback) => callback());
assert.strictEqual(openDetails.open, true, "controle no summary alterou o estado do módulo");

console.log("dashboard_scroll_state_test=ok");
