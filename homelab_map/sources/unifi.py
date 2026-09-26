"""UniFi Network: the physical tree (gateway -> switches/APs -> clients), networks and WireGuard peers.

Needs a local, read-only account on the console (Settings > Admins & Users > add a "View Only" admin
restricted to local access). Works with UniFi OS consoles (UCG, UDM, UDR, UXG, Cloud Key Gen2+) and
with the self-hosted Network application.
"""
import ipaddress
import urllib.error

from ..model import Node, norm_mac, is_virtual_mac
from . import SourceError, cached, opener, request_json

GATEWAY_TYPES = {"udm", "uxg", "ugw", "ucg"}


def fetch(cfg):
    op = opener(cfg["verify_tls"], cookies=True)
    url = cfg["url"].rstrip("/")
    creds = {"username": cfg["username"], "password": cfg["password"]}
    try:  # UniFi OS console
        _, headers = request_json(op, f"{url}/api/auth/login", creds, method="POST")
        base, v2 = f"{url}/proxy/network/api/s/{cfg['site']}", f"{url}/proxy/network/v2/api/site/{cfg['site']}"
        auth = {"X-CSRF-Token": headers.get("X-Csrf-Token", "")}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise SourceError(f"login failed (HTTP {e.code}): check the username and password")
        request_json(op, f"{url}/api/login", creds, method="POST")  # self-hosted Network application
        base, v2, auth = f"{url}/api/s/{cfg['site']}", f"{url}/v2/api/site/{cfg['site']}", {}

    def get(path, root=None):
        data, _ = request_json(op, (root or base) + path, headers=auth)
        return data.get("data", data) if isinstance(data, dict) else data

    raw = {
        "devices": [{"name": d.get("name") or d.get("model"), "type": d.get("type"), "mac": d.get("mac"),
                     "ip": d.get("ip"), "uplink_mac": (d.get("uplink") or {}).get("uplink_mac"),
                     "uplink_port": (d.get("uplink") or {}).get("uplink_remote_port")}
                    for d in get("/stat/device")],
        "networks": [{"id": n["_id"], "name": n.get("name"), "purpose": n.get("purpose"),
                      "subnet": n.get("ip_subnet"), "vpn_type": n.get("vpn_type")}
                     for n in get("/rest/networkconf")],
        "users": [{"mac": u.get("mac"), "name": u.get("name") or u.get("hostname"), "fixed": u.get("use_fixedip"),
                   "ip": u.get("fixed_ip") or u.get("last_ip"), "network": u.get("last_connection_network_name"),
                   "uplink_mac": u.get("last_uplink_mac"), "wired": bool(u.get("is_wired"))}
                  for u in get("/rest/user")],
        "online": [{"mac": s.get("mac"), "name": s.get("name") or s.get("hostname"), "ip": s.get("ip"),
                    "network": s.get("network"), "wired": bool(s.get("is_wired")),
                    "uplink_mac": s.get("last_uplink_mac") or s.get("sw_mac") or s.get("ap_mac"),
                    "port": s.get("last_uplink_remote_port") or s.get("sw_port")}
                   for s in get("/stat/sta")],
        "wireguard": [],
    }
    if cfg["wireguard"]:
        for n in raw["networks"]:
            if n["purpose"] == "remote-user-vpn" and "wireguard" in (n["vpn_type"] or ""):
                try:
                    users = get(f"/wireguard/{n['id']}/users", root=v2)
                except Exception:
                    continue  # older firmware: no peer list
                raw["wireguard"] += [{"network": n["name"], "name": u.get("name"), "ip": u.get("interface_ip")}
                                     for u in users]
    return raw


def _subnet(cidr):
    try:
        return str(ipaddress.ip_interface(cidr).network)
    except ValueError:
        return cidr or ""


def _in(ip, cidr):
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_interface(cidr).network
    except ValueError:
        return False


def apply(graph, cfg, data_dir):
    raw = cached(data_dir, "unifi", lambda: fetch(cfg), graph)
    if not raw:
        return
    lans = [n for n in raw["networks"] if n["purpose"] in ("corporate", "guest") and n["subnet"]]
    infra_ips = [d["ip"] for d in raw["devices"] if d["type"] not in GATEWAY_TYPES and d["ip"]]
    home = next((n for n in lans if any(_in(ip, n["subnet"]) for ip in infra_ips)), lans[0] if lans else None)

    # Gateway, switches, access points
    kinds = {"usw": "switch", "uap": "ap"}
    for d in raw["devices"]:
        kind = "gateway" if d["type"] in GATEWAY_TYPES else kinds.get(d["type"], "physical")
        ip = home["subnet"].split("/")[0] if kind == "gateway" and home else d["ip"]
        graph.add(Node(f"mac:{norm_mac(d['mac'])}", d["name"], kind, ip=ip, mac=norm_mac(d["mac"]),
                       parent="internet" if kind == "gateway" else f"mac:{norm_mac(d['uplink_mac'])}",
                       source="unifi"))
    gateway = graph.gateway()

    # Networks shown as lanes off the gateway rather than in the tree
    if cfg["lane_networks"] == "auto":
        lane_nets = {n["name"]: n for n in lans if n is not home}
    else:
        lane_nets = {n["name"]: n for n in lans if n["name"] in cfg["lane_networks"]}

    # Clients
    online = {norm_mac(s["mac"]): s for s in raw["online"]}
    wanted = {}
    if cfg["clients"] in ("fixed_ip", "all"):
        for u in raw["users"]:
            if u["fixed"] and u["ip"]:
                wanted[norm_mac(u["mac"])] = u
    if cfg["clients"] == "all":
        for mac, s in online.items():
            wanted.setdefault(mac, s)
    for mac, u in wanted.items():
        live = online.get(mac, {})
        name = u.get("name") or live.get("name") or mac
        ip = u.get("ip") or live.get("ip") or ""
        net_name = live.get("network") or u.get("network") or next(
            (n["name"] for n in lans if ip and _in(ip, n["subnet"])), "")
        node = Node(f"mac:{mac}", name.strip(), "client", ip=ip, mac=mac, network=net_name, source="unifi",
                    parent=f"mac:{norm_mac(live.get('uplink_mac') or u.get('uplink_mac'))}",
                    extra={"wired": live.get("wired", u.get("wired"))})
        node.extra["port"] = live.get("port") if node.extra["wired"] else None  # a switch port means nothing on Wi-Fi
        if net_name in lane_nets and gateway:
            net = lane_nets[net_name]
            node.parent, node.lane, node.lane_kind = gateway.id, f"📶  {net_name} · {_subnet(net['subnet'])}", "network"
        graph.add(node)

    # WireGuard peers
    for p in raw["wireguard"]:
        if not p["ip"] or not gateway:
            continue
        net = next((n for n in raw["networks"] if n["name"] == p["network"]), None)
        graph.add(Node(f"wg:{p['ip']}", p["name"] or p["ip"], "vpn", ip=p["ip"], parent=gateway.id,
                       lane=f"🔒  {p['network']} · {_subnet(net['subnet']) if net else ''}".rstrip(" ·"),
                       lane_kind="vpn", source="unifi"))

    # A client "on" the port another UniFi device plugs into is really behind that device (UniFi
    # sometimes reports a wired client at the gateway port its switch uplinks to); which of that
    # device's ports is unknown then.
    behind = {(f"mac:{norm_mac(d['uplink_mac'])}", d["uplink_port"]): f"mac:{norm_mac(d['mac'])}"
              for d in raw["devices"] if d["uplink_mac"] and d["uplink_port"]}
    for n in graph.nodes.values():
        if n.source == "unifi" and n.kind == "client" and not n.lane and (n.parent, n.extra.get("port")) in behind:
            n.parent, n.extra["port"] = behind[(n.parent, n.extra["port"])], None

    # VMs share their host's switch port: hang virtual MACs off the one physical device on that port
    if cfg["guess_guests"]:
        ports = {}
        for n in graph.nodes.values():
            if n.source == "unifi" and n.kind == "client" and n.extra.get("wired") and n.extra.get("port"):
                ports.setdefault((n.parent, n.extra["port"]), []).append(n)
        hosts = {}  # host id -> MAC prefixes of its guests
        for group in ports.values():
            physical = [n for n in group if not is_virtual_mac(n.mac)]
            if len(physical) == 1 and len(group) > 1:
                for n in group:
                    if n is not physical[0] and is_virtual_mac(n.mac):
                        n.parent, n.kind = physical[0].id, "vm"
                        hosts.setdefault(physical[0].id, set()).add(n.mac[:8])
        # Guests with no port to go on (offline, or reported on an upstream device): if exactly one
        # host somewhere behind that device already runs guests with the same MAC prefix, that's
        # almost certainly theirs.
        def behind_of(host_id, device_id):
            cur, seen = graph.nodes.get(host_id), set()
            while cur and cur.parent and cur.id not in seen:
                seen.add(cur.id)
                if cur.parent == device_id:
                    return True
                cur = graph.nodes.get(cur.parent)
            return False

        for n in graph.nodes.values():
            if (n.source == "unifi" and n.kind == "client" and not n.extra.get("port") and not n.lane
                    and n.extra.get("wired") and is_virtual_mac(n.mac)):
                cands = [h for h, prefixes in hosts.items() if n.mac[:8] in prefixes and behind_of(h, n.parent)]
                if len(cands) == 1:
                    n.parent, n.kind = cands[0], "vm"  # running or not is for the Proxmox source to say
