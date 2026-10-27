"""Built-in direct channel for the collector browser.

A tiny local HTTP CONNECT proxy (127.0.0.1, random port) that resolves host names with
public DNS servers (AliDNS / DNSPod) and connects directly. It sidesteps a broken system
resolver — e.g. fake 198.18.x.x addresses left behind after a VPN's TUN mode was turned
off — and the system proxy. It cannot sidestep a TUN adapter that is still running.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import random
import socket
import struct
import threading
import time

PUBLIC_DNS = ("223.5.5.5", "119.29.29.29", "223.6.6.6", "180.76.76.76")
FAKE_NETS = (ipaddress.ip_network("198.18.0.0/15"),)
_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()


def is_fake(ip: str) -> bool:
    try:
        return any(ipaddress.ip_address(ip) in net for net in FAKE_NETS)
    except ValueError:
        return False


def _query(name: str, server: str, timeout: float = 2.5) -> list[str]:
    """Minimal DNS A-record lookup over UDP (follows the CNAME chain inside the answer)."""
    qid = random.randint(0, 0xFFFF)
    packet = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").split("."):
        packet += bytes([len(label)]) + label.encode("idna")
    packet += b"\x00" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, 53))
        data = sock.recv(4096)
    rid, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
    if rid != qid or flags & 0x000F:
        return []
    pos = 12

    def skip_name(p):
        while True:
            length = data[p]
            if length == 0:
                return p + 1
            if length & 0xC0 == 0xC0:
                return p + 2
            p += length + 1

    for _ in range(qd):
        pos = skip_name(pos) + 4
    ips = []
    for _ in range(an):
        pos = skip_name(pos)
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[pos:pos + 4]))
        pos += rdlen
    return ips


def public_resolve(name: str) -> list[str]:
    """Real addresses from public DNS; [] if every server failed or answered with fake IPs."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and hit[0] > now:
            return hit[1]
    for server in PUBLIC_DNS:
        try:
            ips = [ip for ip in _query(name, server) if not is_fake(ip)]
        except (OSError, ValueError, IndexError, struct.error):
            continue
        if ips:
            with _cache_lock:
                _cache[name] = (now + 300, ips)
            return ips
    return []


def resolve(name: str) -> list[str]:
    try:
        ipaddress.ip_address(name)
        return [name]
    except ValueError:
        pass
    ips = public_resolve(name)
    if ips:
        return ips
    try:  # last resort: the system resolver, minus fake addresses
        return [ip for ip in {r[4][0] for r in socket.getaddrinfo(name, 443, socket.AF_INET)} if not is_fake(ip)]
    except OSError:
        return []


async def _pipe(reader, writer):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _connect(host, port):
    ips = await asyncio.get_running_loop().run_in_executor(None, resolve, host)
    last = None
    for ip in ips[:3]:
        try:
            return await asyncio.wait_for(asyncio.open_connection(ip, port), 8)
        except (OSError, asyncio.TimeoutError) as exc:
            last = exc
    raise ConnectionError(f"cannot reach {host}: {last or 'no address'}")


async def _handle(reader, writer):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        writer.close()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    if len(parts) < 3:
        writer.close()
        return
    method, target = parts[0].upper(), parts[1]
    try:
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            up_reader, up_writer = await _connect(host.strip("[]"), int(port or 443))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:  # plain http://host/path request
            from urllib.parse import urlsplit
            url = urlsplit(target)
            up_reader, up_writer = await _connect(url.hostname or "", url.port or 80)
            path = (url.path or "/") + (("?" + url.query) if url.query else "")
            up_writer.write(head.replace(target.encode("latin-1"), path.encode("latin-1"), 1))
            await up_writer.drain()
    except Exception:
        try:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
        return
    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


_server_url = ""
_server_lock = threading.Lock()


def relay_url() -> str:
    """Start the channel once per process and return its proxy URL (http://127.0.0.1:port)."""
    global _server_url
    with _server_lock:
        if _server_url:
            return _server_url
        ready = threading.Event()
        holder = {}

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            server = loop.run_until_complete(asyncio.start_server(_handle, "127.0.0.1", int(os.getenv("RELAY_PORT", "0"))))
            holder["port"] = server.sockets[0].getsockname()[1]
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, name="lead-radar-direct-channel", daemon=True).start()
        if not ready.wait(5):
            raise RuntimeError("内置直连通道启动失败")
        _server_url = f"http://127.0.0.1:{holder['port']}"
        return _server_url
