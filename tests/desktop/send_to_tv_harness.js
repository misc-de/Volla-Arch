// Runs the add-on's background.js in node with a stand-in for Firefox's `browser` API, then the
// scenario given on stdin: the body of an async function (fake, ctx) whose return value (or, if
// none, `fake`) is printed as JSON. The background's functions and constants are in scope.
//
//   node send_to_tv_harness.js path/to/background.js < scenario
"use strict";

const fs = require("fs");
const vm = require("vm");

const fake = {
  listeners: {}, native: [], replies: [], menus: [], removed: 0, updates: [], refreshes: 0,
  notifications: [], scripts: [], scriptFails: false,
};

function event(name) {
  return { addListener: (f) => { fake.listeners[name] = f; } };
}

const browser = {
  i18n: {
    getMessage: (key, subs) => (subs && subs.length ? `${key}(${subs.join(",")})` : key),
  },
  runtime: {
    onInstalled: event("installed"),
    onStartup: event("startup"),
    // A reply is an object, a function giving one (or a promise), or missing: then the host is
    // not installed and Firefox rejects
    sendNativeMessage: async (host, request) => {
      fake.native.push([host, request]);
      const reply = fake.replies.shift();
      if (reply === undefined) {
        throw new Error("No such native application");
      }
      return typeof reply === "function" ? reply() : reply;
    },
  },
  menus: {
    removeAll: async () => { fake.removed++; fake.menus = []; },
    create: (props) => { fake.menus.push(props); },
    update: async (id, props) => { fake.updates.push([id, props]); },
    refresh: async () => { fake.refreshes++; },
    onShown: event("shown"),
    onHidden: event("hidden"),
    onClicked: event("clicked"),
  },
  notifications: {
    create: async (props) => { fake.notifications.push(props.message); return "n"; },
  },
  scripting: {
    executeScript: async (props) => {
      fake.scripts.push(props.target.tabId);
      if (fake.scriptFails) {
        throw new Error("Missing host permission for the tab");
      }
      return [];
    },
  },
  pageAction: { onClicked: event("pageAction") },
};

const ctx = vm.createContext({ browser, URL, console, setTimeout });
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx, { filename: process.argv[2] });
const scenario = fs.readFileSync(0, "utf8");

(async () => {
  const run = new vm.Script(`(async (fake, ctx) => {\n${scenario}\n})`).runInContext(ctx);
  const result = await run(fake, ctx);
  process.stdout.write(JSON.stringify(result === undefined ? fake : result));
})().catch((e) => {
  process.stderr.write(String(e && e.stack ? e.stack : e));
  process.exit(1);
});
