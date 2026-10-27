"""Network self-check for the collector browser: DNS, direct TLS, TLS via the system proxy,
and a real page load with the collector's own proxy settings. Read-only diagnostics."""
from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .browser_collector import (_launch, _route_candidates, _system_proxy, describe_route,
                                friendly_net_error, proxy_settings)

TARGETS = [
    ("抖音", "www.douyin.com"), ("小红书", "www.xiaohongshu.com"), ("微博", "weibo.com"),
    ("知乎", "www.zhihu.com"), ("B站", "www.bilibili.com"), ("快手", "www.kuaishou.com"),
    ("百度贴吧", "tieba.baidu.com"), ("TikTok（境外）", "www.tiktok.com"),
]
# Clash / sing-box / Surge "fake-ip" DNS hands out addresses from this block; it means the
# proxy app captures traffic at the system level (TUN), below anything the browser controls.
FAKE_IP_NETS = [ipaddress.ip_network("198.18.0.0/15")]
TIMEOUT = 6


def _ms(start):
    return int((time.monotonic() - start) * 1000)


def _resolve(host):
    try:
        ips = sorted({row[4][0] for row in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except OSError as exc:
        return [], f"解析失败：{exc}"
    return ips, ""


def _is_fake_ip(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in FAKE_IP_NETS)


def _tls(sock, host):
    ctx = ssl.create_default_context()
    with ctx.wrap_socket(sock, server_hostname=host) as tls:
        return tls.version() or "TLS"


def _describe(exc):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "证书被替换（代理/安全软件在拦截 HTTPS）"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "超时"
    if isinstance(exc, ConnectionResetError):
        return "连接被重置"
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒绝"
    if isinstance(exc, ssl.SSLError):
        return f"TLS 握手失败（{exc.reason or exc}）"
    return f"{type(exc).__name__}：{str(exc)[:80]}"


def direct_tls(host):
    start = time.monotonic()
    try:
        sock = socket.create_connection((host, 443), timeout=TIMEOUT)
        sock.settimeout(TIMEOUT)
        _tls(sock, host)
        return True, f"成功 {_ms(start)}ms"
    except Exception as exc:
        return False, _describe(exc)


def proxy_tls(host, proxy_url):
    start = time.monotonic()
    p = urlparse(proxy_url)
    try:
        sock = socket.create_connection((p.hostname, p.port or 80), timeout=TIMEOUT)
        sock.settimeout(TIMEOUT)
        if p.scheme.startswith("socks"):
            sock.sendall(b"\x05\x01\x00")
            if sock.recv(2) != b"\x05\x00":
                raise ConnectionError("SOCKS5 代理不接受无密码连接")
            name = host.encode()
            sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + (443).to_bytes(2, "big"))
            reply = sock.recv(10)
            if len(reply) < 2 or reply[1] != 0:
                raise ConnectionError(f"SOCKS5 代理拒绝转发（代码 {reply[1] if len(reply) > 1 else '?'}）")
        else:
            sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 4096:
                chunk = sock.recv(1024)
                if not chunk:
                    raise ConnectionResetError("代理直接断开了连接")
                head += chunk
            status = head.split(b"\r\n", 1)[0].decode("latin-1")
            if " 200" not in status:
                raise ConnectionError(f"代理返回 {status[:60]}")
        _tls(sock, host)
        return True, f"成功 {_ms(start)}ms"
    except Exception as exc:
        return False, _describe(exc)


def browser_load(hosts, route=None, channel=None):
    """Load each site in a throw-away profile using the given route (default: .env setting)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {h: (None, "未安装浏览器组件") for h in hosts}
    out = {}
    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, Path(tempfile.mkdtemp(prefix="lr-netcheck-")), headless=True, route=route, channel=channel)
            try:
                page = ctx.new_page()
                for host in hosts:
                    start = time.monotonic()
                    try:
                        page.goto(f"https://{host}/", wait_until="commit", timeout=15000)
                        out[host] = (True, f"能打开 {_ms(start)}ms")
                    except Exception as exc:
                        if "interrupted by another navigation" in str(exc):
                            out[host] = (True, f"能打开（有跳转）{_ms(start)}ms")  # the site answered and redirected
                        else:
                            out[host] = (False, friendly_net_error(exc))
            finally:
                ctx.close()
    except Exception as exc:
        return {h: (None, f"浏览器无法启动：{str(exc)[:120]}") for h in hosts}
    return out


def windows_proxy_registry():
    if sys.platform != "win32":
        return {}
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        info = {}
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
            try:
                info[name] = winreg.QueryValueEx(key, name)[0]
            except OSError:
                pass
        return info
    except OSError:
        return {}


TUN_WORDS = ("wintun", "tap-windows", "tap-win", "tap adapter", "mihomo", "clash", "meta", "sing-box", "singbox",
             "wireguard", "v2ray", "xray", "nekoray", "hiddify", "netch", "leigod", "openvpn", " tun", "tun ")


def windows_network():
    """Active adapters and their DNS servers (Windows only): shows whether a VPN's virtual
    adapter is still up and whether DNS still points at it after TUN mode was switched off."""
    if sys.platform != "win32":
        return {"adapters": [], "dns": []}
    import json
    import subprocess
    script = ("$a=@(Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name,InterfaceDescription);"
              "$d=@(Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object { $_.ServerAddresses } | "
              "Select-Object InterfaceAlias,@{n='Servers';e={$_.ServerAddresses -join ','}});"
              "@{adapters=$a;dns=$d} | ConvertTo-Json -Depth 3 -Compress")
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"adapters": [], "dns": []}
    as_list = lambda v: v if isinstance(v, list) else ([v] if v else [])
    return {"adapters": as_list(data.get("adapters")), "dns": as_list(data.get("dns"))}


def virtual_adapters(net):
    found = []
    for a in net.get("adapters", []):
        text = f" {a.get('Name', '')} {a.get('InterfaceDescription', '')} ".lower()
        if any(w in text for w in TUN_WORDS):
            found.append(f"{a.get('Name', '')}（{a.get('InterfaceDescription', '')}）")
    return found


def fake_dns_servers(net):
    return [f"{d.get('InterfaceAlias', '')}: {d.get('Servers', '')}" for d in net.get("dns", [])
            if any(_is_fake_ip(ip.strip()) for ip in str(d.get("Servers", "")).split(","))]


def flush_dns():
    """Clear the Windows DNS cache (fake 198.18.x.x answers survive after TUN is switched off)."""
    if sys.platform != "win32":
        return False, "仅 Windows 需要"
    import subprocess
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "ipconfig.exe")
    try:
        out = subprocess.run([exe, "/flushdns"], capture_output=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:200]
    text = out.stdout.decode("gbk", "replace").strip() or out.stderr.decode("gbk", "replace").strip()
    return out.returncode == 0, text[:300]


def run_check(include_browser: bool = True):
    system_proxy = _system_proxy()
    _, proxy_note = proxy_settings()
    hosts = [h for _, h in TARGETS]
    from .relay import public_resolve
    with ThreadPoolExecutor(max_workers=12) as pool:
        dns = dict(zip(hosts, pool.map(_resolve, hosts)))
        public = dict(zip(hosts, pool.map(public_resolve, hosts)))
        direct = dict(zip(hosts, pool.map(direct_tls, hosts)))
        via_proxy = dict(zip(hosts, pool.map(lambda h: proxy_tls(h, system_proxy), hosts))) if system_proxy else {}
    browser = browser_load(hosts) if include_browser else {}
    # Which automatic network option can open douyin.com? (the collector tries these in order)
    routes = []
    if include_browser:
        for route, channel in _route_candidates(Path(tempfile.gettempdir()) / "lr-netcheck-none" / "p"):
            ok, msg = browser_load(["www.douyin.com"], route, channel)["www.douyin.com"]
            if ok is None and channel:
                msg = "电脑上没有这个浏览器，跳过"
            routes.append({"route": describe_route(route, channel), "ok": ok, "detail": msg})
    rows = []
    for label, host in TARGETS:
        ips, dns_err = dns[host]
        rows.append({
            "name": label, "host": host,
            "dns": dns_err or ", ".join(ips[:3]), "fake_ip": any(_is_fake_ip(ip) for ip in ips),
            "public_dns": ", ".join(public[host][:2]) or "查不到",
            "direct_ok": direct[host][0], "direct": direct[host][1],
            "proxy_ok": via_proxy.get(host, (None, ""))[0], "proxy": via_proxy.get(host, (None, "未开系统代理"))[1],
            "browser_ok": browser.get(host, (None, ""))[0], "browser": browser.get(host, (None, "未检测"))[1],
        })
    net = windows_network()
    tun = virtual_adapters(net)
    fake_dns = fake_dns_servers(net)
    return {"system_proxy": system_proxy or "", "browser_proxy_mode": os.getenv("BROWSER_PROXY", "auto"),
            "browser_proxy_note": proxy_note, "windows_proxy": windows_proxy_registry(),
            "adapters": net.get("adapters", []), "dns_servers": net.get("dns", []),
            "virtual_adapters": tun, "fake_dns_servers": fake_dns,
            "rows": rows, "routes": routes,
            "conclusion": conclude(rows, system_proxy, routes, tun=tun, fake_dns=fake_dns)}


def conclude(rows, system_proxy, routes=(), tun=(), fake_dns=()):
    domestic = [r for r in rows if "境外" not in r["name"]]
    bad = [r for r in domestic if r["browser_ok"] is False]
    working = [r["route"] for r in routes if r.get("ok")]
    if bad and working:
        return (f"默认方式打不开，但「{working[0]}」可以打开抖音。程序搜索、登录时会自动切换到这种方式，"
                "不需要你改任何设置；直接点「打开浏览器去登录」即可。")
    if tun:
        return ("虚拟网卡还在运行：" + "、".join(tun) + "。只在代理软件里关掉开关有时不够（软件的「服务模式/TUN 服务」还在跑）。"
                "请彻底退出代理软件（托盘图标右键 → 退出），或在代理软件设置里关闭「服务模式」，然后点「清除 DNS 缓存」再诊断一次。")
    if fake_dns:
        return ("虚拟网卡已经关了，但网卡的 DNS 还指向代理软件留下的地址（" + "；".join(fake_dns) + "）。"
                "请在 Windows「设置 → 网络 → 以太网/WLAN → DNS 服务器分配」改回「自动(DHCP)」，或重启电脑；"
                "程序内置的直连通道会绕开它，若上面仍显示打不开请把诊断结果发给技术人员。")
    if any(r["fake_ip"] for r in domestic):
        if all("198.18" not in (r.get("public_dns") or "") and r.get("public_dns") not in ("", "查不到") for r in domestic):
            return ("没发现运行中的虚拟网卡，但系统 DNS 缓存里还是代理软件留下的假地址（198.18.x.x）。"
                    "请点「清除 DNS 缓存」，然后重新诊断；还不行就重启一次电脑。")
        return ("域名被解析成 198.18.x.x，而且连公共 DNS 也被代理软件拦截了，说明虚拟网卡/TUN 实际上还在工作。"
                "请彻底退出代理软件（托盘图标右键 → 退出），或在它的设置里关闭「服务模式」，然后再诊断一次。")
    if not bad:
        if all(r["browser_ok"] for r in domestic):
            return "国内平台都能正常打开。如果登录窗口里还打不开，请关掉那个窗口重新点「打开浏览器去登录」。"
        return "直连检测完成。（浏览器检测未执行或未安装浏览器组件）"
    names = "、".join(r["name"] for r in bad)
    if any("证书" in (r.get("browser") or "") or "证书" in (r.get("direct") or "") for r in bad):
        return (f"{names}：HTTPS 证书被替换，说明有软件在拦截加密连接。常见是杀毒/安全软件的「HTTPS 扫描/网页防护」"
                "（360、火绒、卡巴斯基等），或代理软件的 MITM/证书功能。请在这些软件里关闭 HTTPS 扫描（或把 Lead Radar 加入信任），然后重试。")
    if all(r["direct_ok"] for r in bad):
        reason = next((r.get("browser") for r in bad if r.get("browser")), "")
        return (f"{names}：电脑能直接连上，但采集浏览器打不开（{reason[:60]}）。"
                "请试试在 .env 里设 BROWSER_PROXY=direct 后重启；还不行就把下面的诊断结果复制发给技术人员。")
    if system_proxy and all(r["proxy_ok"] for r in bad):
        return (f"{names}：直连不通，但走代理可以。请在 .env 里设置 BROWSER_PROXY=system（全部走代理）后重启 Lead Radar。")
    return (f"{names}：直连和代理都连不上。可能是网络本身、公司防火墙或代理软件拦截。"
            "请用电脑自带的 Edge 打开 www.douyin.com 试试；如果 Edge 也打不开，就是网络问题，与程序无关。")
