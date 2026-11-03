"""Identify the OS account behind an accepted loopback TCP connection."""
import ipaddress
import os

import psutil


def _address(value):
    ip = ipaddress.ip_address(value)
    return getattr(ip, "ipv4_mapped", None) or ip


def same_account(connection):
    """Match the reverse socket and its owner. An unavailable owner grants no access."""
    try:
        client_ip, client_port = connection.getpeername()[:2]
        server_ip, server_port = connection.getsockname()[:2]
        client_ip, server_ip = _address(client_ip), _address(server_ip)
        if not client_ip.is_loopback or not server_ip.is_loopback:
            return False
        for entry in psutil.net_connections(kind="tcp"):
            if (not entry.pid or not entry.laddr or not entry.raddr or entry.laddr.port != client_port
                    or entry.raddr.port != server_port or _address(entry.laddr.ip) != client_ip
                    or _address(entry.raddr.ip) != server_ip):
                continue
            peer = psutil.Process(entry.pid)
            if os.name == "nt":
                return peer.username().casefold() == psutil.Process().username().casefold()
            return peer.uids().effective == os.geteuid()
    except (OSError, ValueError, psutil.Error):
        pass
    return False
