"""Automatic discovery and network membership for personal LAN / tailnet use."""
from __future__ import annotations

import asyncio
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import struct
import time
import uuid

from .models import Endpoint
from .network import TailnetStatus, tailnet_status, tailscale_path

DISCOVERY_PORT = 49201
PROTOCOL = 'elink-discovery-v2'


def machine_id():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            identifier = winreg.QueryValueEx(key, 'MachineGuid')[0]
    except (ImportError, OSError):
        identifier = f'{socket.gethostname()}:{uuid.getnode()}'
    return hashlib.sha256(('elink-machine-v2:' + identifier).encode()).hexdigest()


def lan_interfaces():
    """Read actual IPv4 interface masks without guessing /24 or scanning subnets."""
    if os.name != 'nt':
        return []
    class Row(C.Structure):
        _fields_ = [('address', W.DWORD), ('index', W.DWORD), ('mask', W.DWORD),
                    ('broadcast', W.DWORD), ('size', W.DWORD), ('unused', W.WORD), ('kind', W.WORD)]
    size = W.ULONG(0)
    get_table = C.WinDLL('iphlpapi').GetIpAddrTable
    get_table.argtypes = [C.c_void_p, C.POINTER(W.ULONG), W.BOOL]
    get_table.restype = W.ULONG
    result = get_table(None, C.byref(size), False)
    if result not in (0, 122) or not 4 <= size.value <= 1024 * 1024:
        return []
    buffer = C.create_string_buffer(size.value)
    if get_table(buffer, C.byref(size), False):
        return []
    count = W.DWORD.from_buffer(buffer).value
    if 4 + count * C.sizeof(Row) > len(buffer):
        return []
    interfaces = []
    for index in range(count):
        row = Row.from_buffer(buffer, 4 + index * C.sizeof(Row))
        address, mask = (socket.inet_ntoa(struct.pack('=I', value)) for value in (row.address, row.mask))
        try:
            interface = ipaddress.ip_interface(f'{address}/{mask}')
            if (not interface.ip.is_loopback and not interface.ip.is_unspecified
                    and interface.network.prefixlen > 0 and not row.kind & (8 | 64)
                    and interface.ip not in ipaddress.ip_network('100.64.0.0/10')):
                interfaces.append(interface)
        except ValueError:
            continue
    return interfaces


def address_ip(value):
    value = ipaddress.ip_address(value.split('%', 1)[0])
    return value.ipv4_mapped if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped else value


class NetworkScope:
    def __init__(self):
        self.interfaces = lan_interfaces()
        self.tailnet = TailnetStatus()
        self.tail_addresses = set()
        self.refresh_lock = asyncio.Lock()
        self.updated = 0.0

    def allows(self, address):
        try:
            ip = address_ip(address)
            return (ip.is_loopback or str(ip) in self.tail_addresses or
                    any(ip.version == interface.version and ip in interface.network for interface in self.interfaces))
        except (ValueError, AttributeError):
            return False

    async def refresh(self):
        async with self.refresh_lock:
            if time.monotonic() - self.updated < 8:
                return
            interfaces = await asyncio.to_thread(lan_interfaces)
            try:
                status = await asyncio.to_thread(tailnet_status, tailscale_path())
            except Exception as exc:
                status = TailnetStatus(state='Unavailable', health=[str(exc)[:160]])
            self.interfaces, self.tailnet = interfaces, status
            self.tail_addresses = {str(address_ip(address)) for peer in status.peers
                                   for address in (peer.addresses or (peer.address,))} if status.ready else set()
            self.updated = time.monotonic()


class Responder(asyncio.DatagramProtocol):
    def __init__(self, scope, info):
        self.scope, self.info = scope, info
        self.transport = None
        self.window, self.count = 0.0, 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        if len(data) > 512 or not self.scope.allows(address[0]):
            return
        now = time.monotonic()
        if now - self.window >= 1:
            self.window, self.count = now, 0
        if self.count >= 50:
            return
        try:
            query = json.loads(data)
            nonce = query['nonce']
            if query.get('protocol') != PROTOCOL or not isinstance(nonce, str) or len(nonce) != 32:
                return
            self.count += 1
            self.transport.sendto(json.dumps(dict(self.info(), nonce=nonce)).encode(), address)
        except (ValueError, TypeError, KeyError, OSError):
            pass


@dataclass
class DiscoveredHost:
    id: str
    name: str
    routes: dict[str, str] = field(default_factory=dict)

    @property
    def address(self):
        return min(self.routes, key=lambda a: (self.routes[a] != '局域网', a))


def add_host(hosts, info, ip, scope, local_id=''):
    if not scope.allows(ip) or not isinstance(info, dict):
        return
    identifier, name, port = info.get('host_id'), info.get('name'), info.get('port')
    if (info.get('protocol') != 'elink' or info.get('version') != 2
            or not isinstance(identifier, str) or len(identifier) != 64
            or any(c not in '0123456789abcdef' for c in identifier) or identifier == local_id
            or not isinstance(name, str) or not 0 < len(name) <= 80
            or type(port) is not int or not 1024 <= port <= 65535):
        return
    endpoint = Endpoint(str(address_ip(ip)), port)
    host = hosts.setdefault(identifier, DiscoveredHost(identifier, name))
    host.routes[endpoint.authority] = 'Tailscale' if str(address_ip(ip)) in scope.tail_addresses else '局域网'


async def discover(scope, local_id='', manual_address='', *, duration=1.0, extra_targets=()):
    await scope.refresh()
    hosts, transports = {}, []
    nonce = secrets.token_hex(16)
    query = json.dumps(dict(protocol=PROTOCOL, nonce=nonce)).encode()
    class Collector(asyncio.DatagramProtocol):
        def datagram_received(self, data, address):
            if len(data) > 2048:
                return
            try:
                info = json.loads(data)
                if isinstance(info, dict) and info.get('nonce') == nonce:
                    add_host(hosts, info, address[0], scope, local_id)
            except (ValueError, TypeError):
                pass
    loop = asyncio.get_running_loop()
    try:
        targets = [(str(i.ip), [(str(i.network.broadcast_address), DISCOVERY_PORT)])
                   for i in scope.interfaces if i.network.prefixlen < 31]
        unicast = [(p.address, DISCOVERY_PORT) for p in scope.tailnet.peers if p.online and ':' not in p.address]
        targets.append(('0.0.0.0', unicast + list(extra_targets)))
        for bind, destinations in targets:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.bind((bind, 0))
                transport, _ = await loop.create_datagram_endpoint(Collector, sock=sock)
            except OSError:
                sock.close()
                continue
            transports.append(transport)
            for destination in destinations:
                transport.sendto(query, destination)
        await asyncio.sleep(duration)
    finally:
        for transport in transports:
            transport.close()
    # HTTPS fallback works when UDP discovery is filtered. Probe known tailnet peers
    # and the user's saved/manual address only; never scan an entire address range.
    candidates = {Endpoint(p.address).authority for p in scope.tailnet.peers if p.online}
    if manual_address:
        candidates.add(manual_address)
    known = {address for host in hosts.values() for address in host.routes}
    semaphore = asyncio.Semaphore(12)
    async def probe(address):
        from .core.transport import fingerprint, request
        async with semaphore:
            try:
                async with asyncio.timeout(2.5):
                    endpoint = Endpoint.parse(address)
                    resolved = await loop.getaddrinfo(endpoint.hostname, endpoint.port, type=socket.SOCK_STREAM)
                    ip = next(item[4][0] for item in resolved if scope.allows(item[4][0]))
                    endpoint = Endpoint(ip, endpoint.port)
                    fp = await fingerprint(endpoint)
                    info = await request(endpoint, fp, 'GET', '/v1/info')
                    add_host(hosts, info, ip, scope, local_id)
            except (Exception, asyncio.TimeoutError):
                pass
    await asyncio.gather(*(probe(a) for a in sorted(candidates - known)[:128]))
    return sorted(hosts.values(), key=lambda h: (h.name.casefold(), h.id))
