"""Proxmox VE: which VMs and LXCs run on which host, and whether they're running.

Needs an API token with read-only access:
  Datacenter > Permissions > API Tokens > Add (user root@pam or a dedicated user, untick
  "Privilege Separation" or give the token itself the role), then
  Datacenter > Permissions > Add > API Token Permission: path "/", role "PVEAuditor".
Guests are matched to devices UniFi already knows by MAC address, so they keep the names you gave
them in UniFi; guests UniFi doesn't know are added with their Proxmox name.
"""
import re
import urllib.parse

from ..model import Node, norm_mac
from . import cached, opener, request_json

MAC_RE = re.compile(r"(?:hwaddr=|=)([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


def fetch(cfg):
    op = opener(cfg["verify_tls"])
    base = cfg["url"].rstrip("/") + "/api2/json"
    auth = {"Authorization": f"PVEAPIToken={cfg['token_id']}={cfg['token_secret']}"}

    def get(path):
        return request_json(op, base + path, headers=auth)[0]["data"]

    hosts = [{"name": n["name"], "ip": n.get("ip", "")} for n in get("/cluster/status") if n.get("type") == "node"]
    guests = []
    for r in get("/cluster/resources?type=vm"):
        if r.get("template"):
            continue
        g = {"vmid": r["vmid"], "name": r.get("name") or str(r["vmid"]), "type": r["type"], "node": r["node"],
             "status": r.get("status"), "macs": [], "ips": []}
        path = f"/nodes/{urllib.parse.quote(r['node'])}/{r['type']}/{r['vmid']}"
        try:
            conf = get(f"{path}/config")
            g["macs"] = [norm_mac(m) for k, v in conf.items() if re.fullmatch(r"net\d+", k)
                         for m in MAC_RE.findall(str(v))]
        except Exception:
            pass
        if g["status"] == "running":
            try:
                if r["type"] == "lxc":
                    ifaces = get(f"{path}/interfaces")
                    g["ips"] = [i["inet"].split("/")[0] for i in ifaces if i.get("inet") and i["name"] != "lo"]
                else:  # needs the QEMU guest agent; fine if it isn't there
                    ifaces = get(f"{path}/agent/network-get-interfaces")["result"]
                    g["ips"] = [a["ip-address"] for i in ifaces if i.get("name") != "lo"
                                for a in i.get("ip-addresses", []) if a.get("ip-address-type") == "ipv4"]
            except Exception:
                pass
        guests.append(g)
    return {"hosts": hosts, "guests": guests}


def _find_host(graph, host, raw, url_host):
    """A Proxmox node whose reported IP matches no device (Proxmox takes it from /etc/hosts, which
    can be out of date). Try its name, then the address in the configured URL (single node), then
    the device UniFi already shows most of its guests under."""
    named = graph.by_name(host["name"])
    if named is not None and named.kind not in ("vm", "lxc", "container"):
        return named
    if len(raw["hosts"]) == 1 and url_host:
        by_url = graph.by_ip(url_host)
        if by_url is not None:
            return by_url
    parents = {}
    for g in raw["guests"]:
        if g["node"] == host["name"]:
            n = next((graph.by_mac(m) for m in g["macs"] if graph.by_mac(m)), None)
            if n is not None and n.parent in graph.nodes and graph.nodes[n.parent].kind not in ("switch", "ap", "gateway"):
                parents[n.parent] = parents.get(n.parent, 0) + 1
    if parents:
        return graph.nodes[max(parents, key=parents.get)]
    return None


def apply(graph, cfg, data_dir):
    raw = cached(data_dir, "proxmox", lambda: fetch(cfg), graph)
    if not raw:
        return
    host_ids = {}
    url_host = urllib.parse.urlparse(cfg["url"]).hostname or ""
    for h in raw["hosts"]:
        node = graph.by_ip(h["ip"]) if h["ip"] else None
        if node is None:
            node = _find_host(graph, h, raw, url_host)
            if node is not None and h["ip"]:
                graph.warn(f"WARNING: Proxmox node {h['name']} reports its IP as {h['ip']}, but it's {node.ip or '?'} "
                           f"on the network (a stale address in its /etc/hosts?); matched it to {node.name}")
        if node is None:
            node = graph.add(Node(f"pve:{h['name']}", h["name"], "physical", ip=h["ip"], source="proxmox"))
        elif node.kind == "vm":  # UniFi's guess was wrong: this is a host
            node.kind = "physical"
        host_ids[h["name"]] = node.id
    for g in raw["guests"]:
        if g["status"] != "running" and not cfg["include_stopped"]:
            continue
        node = next((graph.by_mac(m) for m in g["macs"] if graph.by_mac(m)), None)
        if node is None and g["ips"]:
            node = graph.by_ip(g["ips"][0])
        if node is None:
            node = graph.add(Node(f"pve:{g['node']}:{g['vmid']}", g["name"], "vm",
                                  ip=g["ips"][0] if g["ips"] else "", source="proxmox"))
        node.kind = "lxc" if g["type"] == "lxc" else "vm"
        node.parent = host_ids.get(g["node"], node.parent)
        node.state = "running" if g["status"] == "running" else "stopped"
        node.lane, node.lane_kind = "", ""
        node.extra["vmid"] = g["vmid"]
