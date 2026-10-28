"""4.2 extension mode: one normal browser + the Lead Radar extension, driven over a long-poll."""
import json
import threading
import time

import pytest

from app import browser_collector as bc
from app import ext_bridge as eb


@pytest.fixture
def bridge(tmp_path):
    b = eb.Bridge()
    b.configure(tmp_path, "http://127.0.0.1:8000", bc.SCRIPT_SOURCES)
    return b


def fake_extension(b, handler, stop):
    """Plays the extension's background worker: poll, run, reply."""
    def run():
        while not stop.is_set():
            cmd = b.poll(b.token, b.version, wait=0.2)
            if cmd and cmd.get("cmd"):
                try:
                    b.reply(b.token, {"id": cmd["id"], "ok": True, "result": handler(cmd)})
                except Exception as exc:
                    b.reply(b.token, {"id": cmd["id"], "ok": False, "error": str(exc)})
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_extension_files_contain_every_collector_script(bridge, tmp_path):
    ext = tmp_path / "extension"
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3 and "scripting" in manifest["permissions"]
    scripts = (ext / "scripts.js").read_text(encoding="utf-8")
    for name in list(bc.SCRIPT_SOURCES) + list(eb.EXTRA_SCRIPTS):
        assert json.dumps(name) in scripts
    assert bridge.token in scripts and bridge.version in scripts and "__LR_VERSION__" not in scripts
    assert "importScripts('scripts.js')" in (ext / "bridge.js").read_text(encoding="utf-8")


def test_token_persists_and_wrong_token_is_refused(bridge, tmp_path):
    again = eb.Bridge()
    again.configure(tmp_path, "http://127.0.0.1:8000", bc.SCRIPT_SOURCES)
    assert again.token == bridge.token
    with pytest.raises(PermissionError):
        bridge.poll("wrong", bridge.version, wait=0)


def test_old_extension_is_told_to_reload(bridge):
    assert bridge.poll(bridge.token, "oldversion", wait=0) == {"id": 0, "cmd": "reload"}


def test_call_roundtrip_and_errors(bridge):
    stop = threading.Event()
    fake_extension(bridge, lambda c: (_ for _ in ()).throw(RuntimeError("boom")) if c["cmd"] == "bad" else c["cmd"] + "!", stop)
    try:
        assert bridge.call("ping", timeout=5) == "ping!"
        assert bridge.connected()
        with pytest.raises(eb.BridgeError, match="boom"):
            bridge.call("bad", timeout=5)
    finally:
        stop.set()


def test_call_times_out_when_browser_is_gone(bridge):
    with pytest.raises(eb.BridgeError, match="没有响应"):
        bridge.call("ping", timeout=0.3)


def test_ext_page_runs_named_scripts_and_scrolls(bridge):
    seen = []

    def handler(c):
        seen.append({k: v for k, v in c.items() if k != "id"})
        if c["cmd"] == "window_new":
            return {"windowId": 7, "tabId": 70}
        if c["cmd"] == "tab_new":
            return 71
        if c["cmd"] == "eval" and c["name"] == "search":
            return [{"url": "https://www.bilibili.com/video/BV1xx"}]
        return True
    stop = threading.Event()
    fake_extension(bridge, handler, stop)
    try:
        ctx = eb.ExtContext(bridge, bc.EXT_SCRIPT_NAMES, background=True)
        page = ctx.pages[0]
        assert page.evaluate(bc.SEARCH_DOM_JS, "bilibili") == [{"url": "https://www.bilibili.com/video/BV1xx"}]
        page.goto("https://www.bilibili.com/", wait_until="commit")
        page.mouse.wheel(0, 900)
        assert ctx.new_page().tab_id == 71
        with pytest.raises(eb.BridgeError):
            page.evaluate("() => document.cookie")          # only the known reading scripts can run
        ctx.close()
    finally:
        stop.set()
    cmds = [c["cmd"] for c in seen]
    assert cmds == ["window_new", "eval", "goto", "eval", "tab_new", "close_window"]
    assert seen[0]["background"] is True and seen[2]["wait"] == "commit"
    assert seen[3] == {"cmd": "eval", "tabId": 70, "name": "scroll_window", "arg": 900}


def test_collector_session_uses_extension_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(eb.BRIDGE, "enabled", lambda: True)
    started = []
    monkeypatch.setattr(eb.BRIDGE, "ensure_browser", lambda route_args, note=None: started.append(route_args))

    class Ctx:
        ext, closed = True, False
        def __init__(self, bridge, names, background): self.background = background
        def close(self): Ctx.closed = True
    monkeypatch.setattr(eb, "ExtContext", Ctx)
    with bc._collector_session(tmp_path / "p", ["https://www.douyin.com/"], background=True) as (ctx, desc):
        assert ctx.background is True and "扩展模式" in desc
    assert started and Ctx.closed


def test_show_window_in_extension_mode():
    class Page:
        ext = True
        def show_window(self, show): return ("shown", show)
    assert bc._show_window(Page(), True) == ("shown", True)


def test_ext_endpoints(context, monkeypatch, tmp_path):
    m, client = context
    assert client.get("/api/ext/poll", headers={"X-LR-Token": "nope"}).status_code == 403
    m.BRIDGE.commands.put({"id": 99, "cmd": "ping"})
    got = client.get("/api/ext/poll", headers={"X-LR-Token": m.BRIDGE.token, "X-LR-Version": m.BRIDGE.version}).json()
    assert got == {"id": 99, "cmd": "ping"}
    assert client.post("/api/ext/reply", headers={"X-LR-Token": m.BRIDGE.token},
                       json={"id": 99, "ok": True, "result": 1}).json() == {"ok": True}
    assert m.BRIDGE.replies.pop(99)["result"] == 1
    assert "Lead Radar 采集浏览器" in client.get("/ext-home").text


def test_login_in_extension_mode_opens_tabs_in_running_browser(context, monkeypatch):
    m, client = context
    monkeypatch.setattr(m, "extension_mode", lambda: True)
    monkeypatch.setattr(m, "ensure_ext_browser", lambda d, note=None: None)
    calls = []
    monkeypatch.setattr(m.BRIDGE, "call", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setattr(m, "bring_to_front", lambda pid, wait_seconds=0: True)
    assert client.post("/api/browser-login", json={"platforms": ["douyin", "xhs"]}).status_code == 202
    for _ in range(50):
        if m.LOGIN_JOB["status"] != "running":
            break
        time.sleep(0.05)
    assert m.LOGIN_JOB["status"] == "done" and "不用关" in m.LOGIN_JOB["message"]
    assert calls[0][0] == "open_tabs" and calls[0][1]["urls"][0].startswith("https://www.douyin.com")
    assert not m.BROWSER_EXEC_LOCK.locked()          # logging in no longer blocks searches
