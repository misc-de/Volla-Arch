// Send to TV: plays a YouTube video on the TV that Schwupp is connected to right now. The phone
// only hands over the link; the TV plays it itself in its YouTube app. Two ways in: a long press
// on a link to a video (context menu) and the button in the address bar on a video page. The
// native host send-to-tv-host (package plinius-send-to-tv) talks to Schwupp.
"use strict";

const HOST = "de.cais.send_to_tv";
const MENU_LINK = "send-to-tv-link";
const MENU_PAGE = "send-to-tv-page";
const YT_HOSTS = ["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"];
// Links to a video, for the context menu (the page action has its own list in the manifest)
const LINK_PATTERNS = [
  "*://www.youtube.com/watch*", "*://m.youtube.com/watch*", "*://youtube.com/watch*",
  "*://music.youtube.com/watch*", "*://www.youtube.com/shorts/*", "*://m.youtube.com/shorts/*",
  "*://www.youtube.com/live/*", "*://m.youtube.com/live/*", "*://www.youtube.com/embed/*",
  "*://youtu.be/*",
];
const PAGE_PATTERNS = LINK_PATTERNS;

function msg(key, ...subs) {
  return browser.i18n.getMessage(key, subs);
}

// Whether a URL names one video, as Schwupp's YouTube source reads it
function isVideoUrl(text) {
  let url;
  try {
    url = new URL(text);
  } catch (e) {
    return false;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return false;
  }
  const host = url.hostname.toLowerCase();
  if (host === "youtu.be") {
    return /^\/[^/]+/.test(url.pathname);
  }
  if (!YT_HOSTS.includes(host)) {
    return false;
  }
  if (url.pathname === "/watch") {
    return Boolean(url.searchParams.get("v"));
  }
  return /^\/(?:shorts|live|embed|v)\/[^/]+/.test(url.pathname);
}

// The host's answer, null when it is not installed or failed
async function ask(request) {
  try {
    return await browser.runtime.sendNativeMessage(HOST, request);
  } catch (e) {
    return null;
  }
}

function tvName(reply) {
  return (reply && reply.name) || msg("theTv");
}

function menuState(status) {
  if (!status || !status.connected) {
    return { title: msg("notConnected"), enabled: false };
  }
  if (!status.links) {
    return { title: msg("noLinks", tvName(status)), enabled: false };
  }
  return { title: msg("playOn", tvName(status)), enabled: true };
}

async function createMenus() {
  await browser.menus.removeAll();
  browser.menus.create({
    id: MENU_LINK, title: msg("playOnTv"), contexts: ["link"], targetUrlPatterns: LINK_PATTERNS,
  });
  browser.menus.create({
    id: MENU_PAGE, title: msg("playOnTv"), contexts: ["page", "video"],
    documentUrlPatterns: PAGE_PATTERNS,
  });
}

// A menu that was closed (or replaced by the next one) before the host answered is left alone
let shownMenu = 0;

async function onMenuShown(info) {
  const ours = info.menuIds.filter((id) => id === MENU_LINK || id === MENU_PAGE);
  if (ours.length === 0) {
    return;
  }
  const serial = ++shownMenu;
  const state = menuState(await ask({ action: "status" }));
  if (serial !== shownMenu) {
    return;
  }
  for (const id of ours) {
    await browser.menus.update(id, state);
  }
  await browser.menus.refresh();
}

function onMenuHidden() {
  shownMenu++;
}

function notify(text) {
  return browser.notifications.create({
    type: "basic", iconUrl: "send-to-tv.svg", title: msg("name"), message: text,
  });
}

// The video in the browser stops once the TV has it, so the sound does not play twice
function pauseAll() {
  for (const video of document.querySelectorAll("video")) {
    video.pause();
  }
}

async function pauseVideos(tab) {
  if (!tab || tab.id === undefined || tab.id < 0) {
    return;
  }
  try {
    await browser.scripting.executeScript({ target: { tabId: tab.id }, func: pauseAll });
  } catch (e) {
    // A page the add-on may not touch (another origin): the video keeps playing there
  }
}

function errorText(reply) {
  switch (reply.error) {
    case "no-tv":
      return msg("noTv");
    case "no-links":
      return msg("noLinks", tvName(reply));
    case "bad-url":
      return msg("notAVideo");
    default:
      return msg("failed", reply.detail || msg("unknownError"));
  }
}

async function send(url, tab) {
  if (!isVideoUrl(url)) {
    await notify(msg("notAVideo"));
    return;
  }
  const reply = await ask({ action: "play", url });
  if (!reply) {
    await notify(msg("noHost"));
    return;
  }
  const texts = { playing: "playing", starting: "starting", code: "code" };
  if (reply.result in texts) {
    await pauseVideos(tab);
    await notify(msg(texts[reply.result], tvName(reply)));
    return;
  }
  await notify(errorText(reply));
}

function onMenuClicked(info, tab) {
  const url = info.menuItemId === MENU_LINK ? info.linkUrl : info.pageUrl || (tab && tab.url);
  if (info.menuItemId === MENU_LINK || info.menuItemId === MENU_PAGE) {
    return send(url, tab);
  }
  return undefined;
}

function onPageAction(tab) {
  return send(tab.url, tab);
}

browser.runtime.onInstalled.addListener(createMenus);
browser.runtime.onStartup.addListener(createMenus);
browser.menus.onShown.addListener(onMenuShown);
browser.menus.onHidden.addListener(onMenuHidden);
browser.menus.onClicked.addListener(onMenuClicked);
browser.pageAction.onClicked.addListener(onPageAction);
