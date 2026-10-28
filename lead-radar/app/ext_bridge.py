"""Extension mode (4.2): one ordinary, long-running browser + the Lead Radar extension.

Why: driving a browser through Playwright/CDP marks it as automated (navigator.webdriver is
true) and Douyin/Xiaohongshu then drop the login or keep asking for verification; every search
also started and closed its own browser, fighting the login window over the profile folder.

Here the program starts one normal browser (Playwright's bundled Chromium, which still accepts
--load-extension) with a dedicated profile and the Lead Radar extension. The person logs in in
that browser and it simply stays open. The extension's background worker long-polls this server
for small commands ("open this URL in the collector tab", "run the comment reader on it") and
posts back the results. Nothing is spoofed or hidden: it is a regular browser with an extension,
reading pages the person's own account can see, at the same bounded pace as before.

ExtPage / ExtContext mimic the handful of Playwright Page/BrowserContext methods the collector
uses, so browser_collector keeps one collection flow for both modes.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import secrets
import subprocess
import threading
import time
from pathlib import Path

EXT_TIMEOUT = 45          # seconds to wait for one command's reply
CONNECT_WAIT = 30         # seconds to wait for a freshly started browser's extension to call in


class BridgeError(RuntimeError):
    pass


class Bridge:
    def __init__(self):
        self.commands: "queue.Queue[dict]" = queue.Queue()
        self.replies: dict[int, dict] = {}
        self.reply_ready = threading.Condition()
        self.ids = itertools.count(1)
        self.last_poll = 0.0
        self.version = ""
        self.token = ""
        self.ext_dir: Path | None = None
        self.profile: Path | None = None
        self.server = ""
        self.proc: subprocess.Popen | None = None
        self.launch_lock = threading.Lock()
        self._exe = None

    # ------------------------------------------------------------ setup
    def configure(self, data_dir: Path, server: str, scripts: dict[str, str]):
        """Write the extension (always matching this program version) and remember where it is."""
        data_dir.mkdir(parents=True, exist_ok=True)
        token_file = data_dir / "extension_token"
        try:
            self.token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            self.token = ""
        if not self.token:
            self.token = secrets.token_hex(16)
            token_file.write_text(self.token, encoding="utf-8")
        self.server = server
        self.ext_dir = data_dir / "extension"
        self.profile = data_dir / "browser_profile_ext"
        files = extension_files(server, self.token, scripts)
        self.version = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
        files["scripts.js"] = files["scripts.js"].replace("__LR_VERSION__", self.version)
        self.ext_dir.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (self.ext_dir / name).write_text(text, encoding="utf-8")

    def enabled(self) -> bool:
        if os.getenv("BROWSER_MODE", "extension").strip().lower() not in ("", "extension", "ext"):
            return False
        return self.ext_dir is not None and bool(self.executable())

    def executable(self) -> str:
        """A browser that still accepts --load-extension: Playwright's Chromium (Chrome for
        Testing). Branded Chrome dropped the switch in v137; Edge isn't documented, so it isn't used."""
        forced = os.getenv("BROWSER_EXECUTABLE", "").strip()
        if forced:
            return forced if os.path.exists(forced) else ""
        if self._exe is None:
            path = ""
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    path = pw.chromium.executable_path
            except Exception:
                path = ""
            self._exe = path if path and os.path.exists(path) else ""
        return self._exe

    # ------------------------------------------------------------ server side of the long-poll
    def poll(self, token: str, version: str, wait: float = 20.0):
        """Called by the extension: hand out the next command (or nothing after `wait` seconds)."""
        if not secrets.compare_digest(token or "", self.token or "-"):
            raise PermissionError("bad token")
        self.last_poll = time.time()
        if version and version != self.version:
            return {"id": 0, "cmd": "reload"}       # an old copy of the extension: make it reload itself
        try:
            return self.commands.get(timeout=wait)
        except queue.Empty:
            return {}
        finally:
            self.last_poll = time.time()

    def reply(self, token: str, data: dict):
        if not secrets.compare_digest(token or "", self.token or "-"):
            raise PermissionError("bad token")
        self.last_poll = time.time()
        with self.reply_ready:
            self.replies[int(data.get("id") or 0)] = data
            self.reply_ready.notify_all()

    def connected(self) -> bool:
        return time.time() - self.last_poll < 30

    # ------------------------------------------------------------ calling the extension
    def call(self, cmd: str, timeout: float = EXT_TIMEOUT, **args):
        cid = next(self.ids)
        self.commands.put({"id": cid, "cmd": cmd, **args})
        end = time.time() + timeout
        with self.reply_ready:
            while cid not in self.replies:
                left = end - time.time()
                if left <= 0:
                    raise BridgeError(f"采集浏览器没有响应（{cmd}）。如果它被关掉了，下次搜索会自动重新打开。")
                self.reply_ready.wait(min(left, 1.0))
            data = self.replies.pop(cid)
        if not data.get("ok"):
            raise BridgeError(str(data.get("error") or "扩展执行失败"))
        return data.get("result")

    # ------------------------------------------------------------ the browser itself
    def ensure_browser(self, route_args=(), note=None, start_url: str = "") -> bool:
        """Start the collector browser if it isn't running; wait for the extension to call in.
        Returns True when it had to be started."""
        with self.launch_lock:
            if self.connected():
                return False
            exe = self.executable()
            if not exe:
                raise BridgeError("没有找到程序自带的浏览器（Chromium）。请关闭程序后重新双击 start.bat 安装浏览器组件。")
            self.profile.mkdir(parents=True, exist_ok=True)
            _forget_window_position(self.profile)
            args = [exe, f"--user-data-dir={self.profile}", f"--load-extension={self.ext_dir}",
                    "--no-first-run", "--no-default-browser-check",
                    # keep pages working while the window is minimised / behind others
                    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--window-position=60,40", "--window-size=1300,860", *route_args]
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                args.append("--no-sandbox")
            if note:
                note("正在打开 Lead Radar 采集浏览器（第一次大约 10 秒）…")
            self.proc = subprocess.Popen(args + [start_url or (self.server + "/ext-home")],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            end = time.time() + CONNECT_WAIT
            while time.time() < end:
                if self.connected():
                    return True
                # (if the browser was already open, the new process hands over to it and exits; the
                # running copy's extension calls in again within a few seconds, so keep waiting)
                time.sleep(0.5)
            raise BridgeError("采集浏览器打开了，但 Lead Radar 扩展没有连上。请关掉这个浏览器后重试；"
                              "如果杀毒软件拦截了扩展，请把程序文件夹加入信任。")

    def pid(self) -> int:
        return self.proc.pid if self.proc and self.proc.poll() is None else 0


BRIDGE = Bridge()


def _forget_window_position(profile: Path):
    prefs = profile / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
        if isinstance(data.get("browser"), dict) and "window_placement" in data["browser"]:
            del data["browser"]["window_placement"]
            prefs.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass


# ---------------------------------------------------------------- Playwright look-alikes
class _Mouse:
    def __init__(self, page):
        self.page = page

    def move(self, *a, **k):
        pass

    def wheel(self, dx, dy):
        self.page._eval("scroll_window", dy)


class ExtPage:
    """The subset of Playwright's Page that browser_collector uses, executed by the extension."""
    viewport_size = {"width": 1300, "height": 860}
    ext = True

    def __init__(self, context, tab_id: int):
        self.context, self.tab_id, self.mouse = context, tab_id, _Mouse(self)
        self._closed = False

    def _eval(self, name, arg=None, timeout=EXT_TIMEOUT):
        return self.context.bridge.call("eval", tabId=self.tab_id, name=name, arg=arg, timeout=timeout)

    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        wait = "commit" if wait_until == "commit" else "dom"
        return self.context.bridge.call("goto", tabId=self.tab_id, url=url, wait=wait,
                                        ms=int(timeout), timeout=timeout / 1000 + 10)

    def evaluate(self, js, arg=None):
        name = self.context.script_names.get(js)
        if not name:
            raise BridgeError("扩展模式不支持这段页面脚本")
        return self._eval(name, arg)

    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)

    def wait_for_load_state(self, state="domcontentloaded", timeout=30000):
        self.context.bridge.call("wait_ready", tabId=self.tab_id, ms=int(timeout), timeout=timeout / 1000 + 10)

    def bring_to_front(self):
        self.context.bridge.call("activate", tabId=self.tab_id)

    def show_window(self, show: bool):
        self.context.bridge.call("show", tabId=self.tab_id, show=bool(show))
        if show:
            from .browser_collector import bring_to_front
            bring_to_front(self.context.bridge.pid(), wait_seconds=1)
        return True

    def click_text(self, texts):
        return self._eval("click_text", list(texts))

    def is_closed(self):
        if self._closed:
            return True
        try:
            return not self.context.bridge.call("tab_exists", tabId=self.tab_id, timeout=10)
        except BridgeError:
            return True

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.context.bridge.call("close_tab", tabId=self.tab_id, timeout=10)
            except BridgeError:
                pass


class ExtContext:
    """One collector window (normal or minimised) in the running browser."""
    ext = True

    def __init__(self, bridge: Bridge, script_names: dict, background: bool):
        self.bridge, self.script_names, self.keep_open = bridge, script_names, False
        info = bridge.call("window_new", background=bool(background))
        self.window_id = info["windowId"]
        self.pages = [ExtPage(self, info["tabId"])]

    def new_page(self):
        tab = self.bridge.call("tab_new", windowId=self.window_id)
        page = ExtPage(self, tab)
        self.pages.append(page)
        return page

    def cookies(self):
        return self.bridge.call("cookies", timeout=15) or []

    def close(self):
        if self.keep_open:
            return
        try:
            self.bridge.call("close_window", windowId=self.window_id, timeout=10)
        except BridgeError:
            pass


# ---------------------------------------------------------------- the extension source
EXTRA_SCRIPTS = {
    "scroll_window": "(dy) => { window.scrollBy(0, dy || 900); return true; }",
    "ready_state": "() => document.readyState",
    "click_text": r"""(texts) => {
  const all = [...document.querySelectorAll('button, a, span, div, li')];
  let clicked = 0;
  for (const want of texts) {
    const el = all.find(e => e.getClientRects().length && (e.innerText || '').trim() === want && !e.querySelector('*:not(i):not(svg):not(path)'));
    if (el) { el.click(); clicked++; }
  }
  return clicked;
}""",
}

MANIFEST = {
    "manifest_version": 3,
    "name": "Lead Radar 采集助手",
    "version": "4.2.0",
    "description": "让 Lead Radar 在这个浏览器里读取你能看到的公开作品和评论。只连接本机的 Lead Radar 程序。",
    "permissions": ["scripting", "tabs", "cookies", "alarms", "storage"],
    "host_permissions": ["<all_urls>"],
    "background": {"service_worker": "bridge.js"},
}

BRIDGE_JS = r"""importScripts('scripts.js');
const H = {'X-LR-Token': LR.token, 'X-LR-Version': LR.version, 'Content-Type': 'application/json'};
const sleep = ms => new Promise(r => setTimeout(r, ms));
let looping = false;

async function reply(id, ok, payload) {
  const body = JSON.stringify(ok ? {id, ok: true, result: payload ?? null} : {id, ok: false, error: String(payload)});
  for (let i = 0; i < 3; i++) {
    try { await fetch(LR.server + '/api/ext/reply', {method: 'POST', headers: H, body}); return; } catch (e) { await sleep(500); }
  }
}

async function runScript(tabId, name, arg) {
  const res = await chrome.scripting.executeScript({target: {tabId}, world: 'MAIN', func: SCRIPTS[name],
                                                    args: arg === undefined || arg === null ? [] : [arg]});
  return res && res[0] ? res[0].result : null;
}

async function waitReady(tabId, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      const state = await runScript(tabId, 'ready_state');
      if (state === 'interactive' || state === 'complete') return state;
    } catch (e) {
      if (/error page/i.test(String(e))) throw new Error('net::ERR_FAILED 页面打不开（网络或代理问题）');
    }
    await sleep(250);
  }
  return 'timeout';
}

const COMMANDS = {
  async ping() { return {version: LR.version}; },
  async reload() { setTimeout(() => chrome.runtime.reload(), 200); return true; },
  async window_new({background}) {
    const w = await chrome.windows.create({url: 'about:blank', focused: !background, left: 60, top: 40, width: 1300, height: 860});
    if (background) await chrome.windows.update(w.id, {state: 'minimized'});
    return {windowId: w.id, tabId: w.tabs[0].id};
  },
  async tab_new({windowId}) { const t = await chrome.tabs.create({windowId, active: false, url: 'about:blank'}); return t.id; },
  async goto({tabId, url, wait, ms}) {
    await chrome.tabs.update(tabId, {url});
    if (wait === 'commit') { await sleep(300); return true; }
    await sleep(300);
    await waitReady(tabId, ms || 30000);
    return true;
  },
  async wait_ready({tabId, ms}) { return waitReady(tabId, ms || 30000); },
  async eval({tabId, name, arg}) {
    try { return await runScript(tabId, name, arg); }
    catch (e) { if (/error page/i.test(String(e))) throw new Error('net::ERR_FAILED 页面打不开（网络或代理问题）'); throw e; }
  },
  async activate({tabId}) { await chrome.tabs.update(tabId, {active: true}); return true; },
  async show({tabId, show}) {
    const t = await chrome.tabs.get(tabId);
    if (show) { await chrome.windows.update(t.windowId, {state: 'normal'}); await chrome.windows.update(t.windowId, {focused: true, drawAttention: true}); }
    else await chrome.windows.update(t.windowId, {state: 'minimized'});
    return true;
  },
  async tab_exists({tabId}) { try { await chrome.tabs.get(tabId); return true; } catch (e) { return false; } },
  async close_tab({tabId}) { try { await chrome.tabs.remove(tabId); } catch (e) {} return true; },
  async close_window({windowId}) { try { await chrome.windows.remove(windowId); } catch (e) {} return true; },
  async open_tabs({urls}) {
    const w = await chrome.windows.create({url: urls, focused: true, left: 60, top: 40, width: 1300, height: 860});
    return {windowId: w.id};
  },
  async cookies() {
    // names and expiry only, never values
    const all = await chrome.cookies.getAll({});
    return all.map(c => ({domain: c.domain, name: c.name, value: c.value ? 'x' : '', expires: c.expirationDate || -1}));
  },
};

async function handle(cmd) {
  try {
    const fn = COMMANDS[cmd.cmd];
    if (!fn) throw new Error('unknown command ' + cmd.cmd);
    await reply(cmd.id, true, await fn(cmd));
  } catch (e) { await reply(cmd.id, false, e && e.message ? e.message : e); }
}

async function loop() {
  if (looping) return;
  looping = true;
  try {
    while (true) {
      chrome.runtime.getPlatformInfo(() => {});   // an extension API call keeps the worker alive
      let cmd = null;
      try {
        const r = await fetch(LR.server + '/api/ext/poll', {headers: H});
        if (r.status === 403) { await sleep(5000); continue; }
        cmd = await r.json();
      } catch (e) { await sleep(2000); continue; }
      if (cmd && cmd.cmd) handle(cmd);   // not awaited: keep polling while a page loads
    }
  } finally { looping = false; }
}
chrome.runtime.onStartup.addListener(loop);
chrome.runtime.onInstalled.addListener(loop);
chrome.alarms.create('lead-radar-keepalive', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(loop);
loop();
"""


def extension_files(server: str, token: str, scripts: dict[str, str]) -> dict[str, str]:
    body = ",\n".join(f"  {json.dumps(name)}: {src}" for name, src in {**scripts, **EXTRA_SCRIPTS}.items())
    scripts_js = (f"const LR = {{server: {json.dumps(server)}, token: {json.dumps(token)}, version: '__LR_VERSION__'}};\n"
                  f"const SCRIPTS = {{\n{body}\n}};\n")
    return {"manifest.json": json.dumps(MANIFEST, ensure_ascii=False, indent=1),
            "bridge.js": BRIDGE_JS, "scripts.js": scripts_js}
