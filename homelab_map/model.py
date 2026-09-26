"""The network as a tree of nodes, built up by the sources and then laid out and drawn.

Every node hangs off exactly one parent (what it plugs into, or runs on). Sources add nodes and
look existing ones up by MAC, IP or name, so the same device found by UniFi and by Proxmox ends up
as one node.
"""
from dataclasses import dataclass, field
import ipaddress
import re

# Kinds, roughly top to bottom. The order is also how siblings are sorted left to right.
KINDS = ["internet", "gateway", "switch", "ap", "physical", "vm", "lxc", "client", "iot", "vpn", "container"]
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}

# MAC prefixes used by hypervisors for their guests (lower case, colon separated)
VIRTUAL_OUIS = ("bc:24:11", "52:54:00", "00:50:56", "00:0c:29", "00:15:5d", "02:11:32", "00:16:3e", "08:00:27")


def norm_mac(mac):
    return (mac or "").strip().lower().replace("-", ":")


def is_virtual_mac(mac):
    """A hypervisor's MAC prefix, or a locally administered address (second hex digit 2, 6, A or E),
    which hypervisors (e.g. Home Assistant OS) use too. Only meaningful for wired devices: phones
    randomise their Wi-Fi MACs the same way."""
    mac = norm_mac(mac)
    if mac.startswith(VIRTUAL_OUIS):
        return True
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except ValueError:
        return False


def ip_key(ip):
    try:
        return (0, int(ipaddress.ip_address(ip)))
    except ValueError:
        return (1, 0)


@dataclass
class Node:
    id: str
    name: str
    kind: str
    ip: str = ""
    mac: str = ""
    parent: str = ""            # id of the parent node ("" only for the internet root)
    lane: str = ""              # title of the lane it's drawn in, if any
    lane_kind: str = ""         # "network" | "vpn" | "group" (container group) | "auto"
    port: str = ""              # containers: ":8080", or "no web UI"
    state: str = "running"      # running | stopped
    public: bool = False        # reachable from the internet through the reverse proxy
    url: str = ""
    network: str = ""
    docker_host: bool = False   # its containers are drawn under it
    source: str = ""
    extra: dict = field(default_factory=dict)

    def sub(self):
        """Second line of the label."""
        if self.state != "running":
            return "stopped"
        if self.kind == "container":
            return self.port or "no web UI"
        return self.ip


class Graph:
    def __init__(self, internet_label="Internet"):
        self.nodes = {}
        self.warnings = []
        self.guesses = []       # (group title, container name) placed by best guess
        self.add(Node("internet", internet_label, "internet"))

    def add(self, node):
        self.nodes[node.id] = node
        return node

    def remove(self, node_id):
        """Remove a node; its children move up to its parent."""
        node = self.nodes.pop(node_id, None)
        if node:
            for n in self.nodes.values():
                if n.parent == node_id:
                    n.parent = node.parent

    def warn(self, message):
        from .config import redact
        message = redact(message)
        if message not in self.warnings:
            self.warnings.append(message)

    def by_mac(self, mac):
        mac = norm_mac(mac)
        return next((n for n in self.nodes.values() if mac and n.mac == mac), None)

    def by_ip(self, ip):
        return next((n for n in self.nodes.values() if ip and n.ip == ip and n.kind != "container"), None)

    def by_name(self, name):
        name = (name or "").strip().lower()
        return next((n for n in self.nodes.values() if n.name.strip().lower() == name), None)

    def find(self, ref):
        """A node by id, MAC, IP or name: how config.yaml refers to things."""
        if ref in self.nodes:
            return self.nodes[ref]
        ref = str(ref)
        if re.fullmatch(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", ref):
            return self.by_mac(ref)
        return self.by_ip(ref) or self.by_name(ref)

    def gateway(self):
        return next((n for n in self.nodes.values() if n.kind == "gateway"), None)

    def children(self, node_id):
        return [n for n in self.nodes.values() if n.parent == node_id]

    def repair(self):
        """Give every orphan a parent and break any cycles, so the tree is always drawable."""
        fallback = (self.gateway() or self.nodes["internet"]).id
        for n in self.nodes.values():
            if n.id == "internet":
                n.parent = ""
            elif n.parent not in self.nodes or n.parent == n.id:
                n.parent = fallback if n.id != fallback else "internet"
        for n in list(self.nodes.values()):
            seen, cur = set(), n
            while cur.parent:
                if cur.id in seen:
                    self.warn(f"WARNING: {cur.name} was part of a parent loop; attached it to the gateway")
                    cur.parent = fallback if cur.id != fallback else "internet"
                    break
                seen.add(cur.id)
                cur = self.nodes[cur.parent]
