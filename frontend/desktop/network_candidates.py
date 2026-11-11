"""Reachable IPv4 candidates from the Windows neighbor and route tables."""

from __future__ import annotations

import ctypes
import ipaddress
import os
import socket
from ctypes import wintypes

AF_INET = 2


class _SockaddrIn(ctypes.Structure):
    _fields_ = [
        ("family", wintypes.USHORT),
        ("port", wintypes.USHORT),
        ("address", ctypes.c_ubyte * 4),
        ("zero", ctypes.c_ubyte * 8),
    ]


class _SockaddrIn6(ctypes.Structure):
    _fields_ = [
        ("family", wintypes.USHORT),
        ("port", wintypes.USHORT),
        ("flowinfo", wintypes.ULONG),
        ("address", ctypes.c_ubyte * 16),
        ("scope_id", wintypes.ULONG),
    ]


class _SockaddrInet(ctypes.Union):
    _fields_ = [("ipv4", _SockaddrIn), ("ipv6", _SockaddrIn6)]


class _IpAddressPrefix(ctypes.Structure):
    _fields_ = [("prefix", _SockaddrInet), ("prefix_length", ctypes.c_ubyte)]


class _MibIpNetRow2(ctypes.Structure):
    _fields_ = [
        ("address", _SockaddrInet),
        ("interface_index", wintypes.ULONG),
        ("interface_luid", ctypes.c_ulonglong),
        ("physical_address", ctypes.c_ubyte * 32),
        ("physical_address_length", wintypes.ULONG),
        ("state", wintypes.ULONG),
        ("flags", ctypes.c_ubyte),
        ("reachability_time", wintypes.ULONG),
    ]


class _MibIpNetTable2(ctypes.Structure):
    _fields_ = [("count", wintypes.ULONG), ("rows", _MibIpNetRow2 * 1)]


class _MibIpForwardRow2(ctypes.Structure):
    _fields_ = [
        ("interface_luid", ctypes.c_ulonglong),
        ("interface_index", wintypes.ULONG),
        ("destination_prefix", _IpAddressPrefix),
        ("next_hop", _SockaddrInet),
        ("site_prefix_length", ctypes.c_ubyte),
        ("valid_lifetime", wintypes.ULONG),
        ("preferred_lifetime", wintypes.ULONG),
        ("metric", wintypes.ULONG),
        ("protocol", wintypes.ULONG),
        ("loopback", ctypes.c_ubyte),
        ("autoconfigure_address", ctypes.c_ubyte),
        ("publish", ctypes.c_ubyte),
        ("immortal", ctypes.c_ubyte),
        ("age", wintypes.ULONG),
        ("origin", wintypes.ULONG),
    ]


class _MibIpForwardTable2(ctypes.Structure):
    _fields_ = [("count", wintypes.ULONG), ("rows", _MibIpForwardRow2 * 1)]


def candidate_addresses(
    interfaces: list[dict],
    neighbors: list[dict],
    routes: list[dict],
) -> list[dict]:
    active = {
        interface["index"]
        for interface in interfaces
        if interface["up"] and not interface["loopback"]
        and any(_valid_ipv4(entry["ip"]) for entry in interface["addresses"])
    }
    local = {
        entry["ip"]
        for interface in interfaces
        if interface["index"] in active
        for entry in interface["addresses"]
    }
    candidates = set()
    for row in neighbors:
        if row["interface_index"] in active and row["state"] >= 2:
            candidates.add((row["ip"], row["interface_index"]))
    for row in routes:
        if row["interface_index"] in active and row["prefix_length"] == 32:
            candidates.add((row["ip"], row["interface_index"]))
    return [
        {"ip": address, "interface_index": index}
        for address, index in sorted(candidates, key=lambda item: (item[1], ipaddress.ip_address(item[0])))
        if _valid_ipv4(address) and address not in local
    ]


def network_candidates(interfaces: list[dict]) -> list[dict]:
    if os.name != "nt":
        return []
    try:
        api = ctypes.WinDLL("iphlpapi", use_last_error=True)
        api.FreeMibTable.argtypes = [ctypes.c_void_p]
        api.FreeMibTable.restype = None
        neighbors = _read_table(api, "GetIpNetTable2", _MibIpNetTable2, _MibIpNetRow2, _neighbor)
        routes = _read_table(
            api, "GetIpForwardTable2", _MibIpForwardTable2, _MibIpForwardRow2, _host_route
        )
    except (AttributeError, OSError):
        return []
    return candidate_addresses(interfaces, neighbors, routes)


def _read_table(api, name, table_type, row_type, convert) -> list[dict]:
    getter = getattr(api, name)
    getter.argtypes = [wintypes.USHORT, ctypes.POINTER(ctypes.c_void_p)]
    getter.restype = wintypes.ULONG
    table_pointer = ctypes.c_void_p()
    result = getter(AF_INET, ctypes.byref(table_pointer))
    if result:
        raise ctypes.WinError(result)
    if not table_pointer.value:
        return []
    try:
        table = ctypes.cast(table_pointer, ctypes.POINTER(table_type)).contents
        rows = ctypes.cast(
            table_pointer.value + table_type.rows.offset, ctypes.POINTER(row_type)
        )
        return [convert(rows[index]) for index in range(table.count)]
    finally:
        api.FreeMibTable(table_pointer)


def _address(sockaddr: _SockaddrInet) -> str:
    if sockaddr.ipv4.family != AF_INET:
        return ""
    return socket.inet_ntoa(bytes(sockaddr.ipv4.address))


def _neighbor(row: _MibIpNetRow2) -> dict:
    return {
        "ip": _address(row.address),
        "interface_index": row.interface_index,
        "state": row.state,
    }


def _host_route(row: _MibIpForwardRow2) -> dict:
    return {
        "ip": _address(row.destination_prefix.prefix),
        "interface_index": row.interface_index,
        "prefix_length": row.destination_prefix.prefix_length,
    }


def _valid_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and not (
            address.is_unspecified or address.is_loopback or address.is_multicast
            or address == ipaddress.IPv4Address("255.255.255.255")
        )
    except ValueError:
        return False
