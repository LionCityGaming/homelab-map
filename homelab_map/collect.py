"""Build the graph: run every enabled source, add manual devices, then apply overrides."""
import json
import os

from .model import Graph, Node, KINDS, norm_mac
from .sources import caddy, docker, proxmox, unifi


def collect(cfg):
    graph = Graph(cfg["internet_label"])
    src, data_dir = cfg["sources"], cfg["data_dir"]
    if src["unifi"]["enabled"]:
        unifi.apply(graph, src["unifi"], data_dir)
    if src["proxmox"]["enabled"]:
        proxmox.apply(graph, src["proxmox"], data_dir)
    add_devices(graph, cfg["devices"])
    if src["docker"]["hosts"]:
        cache_file = os.path.join(data_dir, "cache", "descriptions.json")
        describe_cache = json.load(open(cache_file, encoding="utf8")) if os.path.exists(cache_file) else {}
        docker.apply(graph, src["docker"], cfg["containers"], data_dir, describe_cache)
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, "w", encoding="utf8") as f:
            json.dump(describe_cache, f, indent=1)
    apply_overrides(graph, cfg["overrides"])
    if src["caddy"]["enabled"]:
        caddy.apply(graph, src["caddy"], data_dir)
    graph.repair()
    return graph


def add_devices(graph, devices):
    """Devices from config.yaml: added, or merged into one a source already found (same MAC/IP/name)."""
    for d in devices:
        if not d.get("name"):
            graph.warn(f"WARNING: a device in config.yaml has no name: {d}")
            continue
        node = (graph.by_mac(d["mac"]) if d.get("mac") else None) or (graph.by_ip(d["ip"]) if d.get("ip") else None) \
            or graph.by_name(d["name"])
        if node is None:
            node = graph.add(Node(f"manual:{d['name']}", d["name"], "physical", source="manual"))
        node.name = d["name"]
        for key in ("ip", "lane"):
            if d.get(key):
                setattr(node, key, str(d[key]))
        if d.get("mac"):
            node.mac = norm_mac(d["mac"])
        if d.get("kind"):
            node.kind = d["kind"] if d["kind"] in KINDS else "physical"
        if d.get("lane"):
            node.lane_kind = d.get("lane_kind", "network")
        node.extra["wanted_parent"] = d.get("parent", "")
    for n in list(graph.nodes.values()):  # parents last, so a device can hang off one defined after it
        ref = n.extra.pop("wanted_parent", None)
        if ref:
            parent = graph.find(ref)
            if parent:
                n.parent = parent.id
            else:
                graph.warn(f"WARNING: {n.name}: parent '{ref}' in config.yaml matches no device")


def apply_overrides(graph, ov):
    for ref, name in (ov.get("rename") or {}).items():
        node = graph.find(ref)
        if node:
            node.name = str(name)
        else:
            graph.warn(f"WARNING: overrides.rename: '{ref}' matches no device")
    for ref, kind in (ov.get("kind") or {}).items():
        node = graph.find(ref)
        if node and kind in KINDS:
            node.kind = kind
    for ref, parent_ref in (ov.get("parent") or {}).items():
        node, parent = graph.find(ref), graph.find(parent_ref)
        if node and parent:
            node.parent = parent.id
            node.lane, node.lane_kind = ("", "") if node.lane_kind == "network" else (node.lane, node.lane_kind)
        else:
            graph.warn(f"WARNING: overrides.parent: '{ref}' or '{parent_ref}' matches no device")
    for ref in ov.get("hide") or []:
        node = graph.find(ref)
        if node:
            graph.remove(node.id)
