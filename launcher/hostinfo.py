"""Who this server is on the network, worked out rather than configured.

The launcher used to hand out links built from a hostname typed once into
settings.cmd. The office DHCP does not promise the same address twice, so
after a reboot that string could name an address the machine no longer has,
and every link the dashboard printed was dead while the server itself was
fine.

Two things fix that. Links are built from the address the browser actually
used to reach us, so they can never be stale. And the machine's *name* is
offered for sharing, because that is the part that survives a reboot when the
address does not.
"""
from __future__ import annotations

import socket

import psutil

# Addresses that identify the loopback interface rather than this machine's
# place on the network, and the link-local block Windows invents when DHCP
# has not answered yet.
_LOOPBACK = "127."
_LINK_LOCAL = "169.254."


def computer_name() -> str:
    """The machine's own name, which DHCP does not change."""
    try:
        return socket.gethostname().split(".")[0]
    except OSError:
        return "this-server"


def lan_addresses() -> list[str]:
    """Every IPv4 address this machine holds on a real network, best first.

    The primary one is whichever interface the operating system would route
    out of, which is the one colleagues can reach. Opening a UDP socket is
    how that is discovered without sending anything.
    """
    found: list[str] = []

    primary = _primary_address()
    if primary:
        found.append(primary)

    try:
        interfaces = psutil.net_if_addrs()
    except Exception:  # pragma: no cover - psutil failing is not fatal here
        interfaces = {}

    for addresses in interfaces.values():
        for entry in addresses:
            if entry.family != socket.AF_INET:
                continue
            address = entry.address
            if address.startswith(_LOOPBACK) or address.startswith(_LINK_LOCAL):
                continue
            if address not in found:
                found.append(address)

    return found


def _primary_address() -> str | None:
    """The address on the interface that reaches the rest of the network.

    Nothing is sent: connecting a UDP socket only asks the routing table which
    local address would be used, which works on a LAN with no internet too.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if address.startswith(_LOOPBACK) or address.startswith(_LINK_LOCAL):
        return None
    return address


def share_host() -> str:
    """The host to put in a link meant to outlive a reboot.

    The computer's name, whenever we have one worth using. An address is
    offered only as a fallback, because the address is the part that moves.
    """
    name = computer_name()
    if name and name not in ("localhost", "this-server"):
        return name
    addresses = lan_addresses()
    return addresses[0] if addresses else "localhost"


def summary(port: int) -> dict:
    """What the Server tab shows about reaching this machine."""
    name = computer_name()
    addresses = lan_addresses()
    return {
        "computer_name": name,
        "addresses": addresses,
        "share_url": f"http://{share_host()}:{port}/",
        "address_urls": [f"http://{a}:{port}/" for a in addresses],
        "name_is_usable": bool(name and name not in ("localhost", "this-server")),
    }
