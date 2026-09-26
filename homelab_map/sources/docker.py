"""Docker: the containers on each Docker host, whether they're running, and their ports.

Talk to Docker through a read-only socket proxy (see docker-compose.yml), never an open socket:
  endpoint: tcp://docker-proxy:2375      the proxy that ships with this compose file
  endpoint: tcp://192.168.1.20:2375      a proxy running on another Docker host
  endpoint: unix:///var/run/docker.sock  mounted socket (read-only), if you must

Which containers are drawn:
  - anything with a homelab-map.hide=true label, or listed under containers.hide: never
  - helpers (databases, caches, workers, sidecar proxies...) by name: never
  - otherwise, containers that publish a port; plus, for a compose project where nothing publishes
    a port (a bot, a background job), its first container ("no web UI")
Labels you can put on a container: homelab-map.name, homelab-map.group, homelab-map.port,
homelab-map.hide.
"""
import http.client
import json
import os
import re
import socket
import urllib.parse

from ..model import Node
from .. import grouping
from . import cached

HELPER_RE = re.compile(r"(^|[-_])(db|database|postgres|postgresql|pg|mariadb|mysql|mongo|mongodb|redis|valkey|"
                       r"memcached|broker|rabbitmq|worker|proxy|gotenberg|tika|machine[-_]learning|ml|cache|"
                       r"migrate|init|cron|backup)([-_]?\d+)?$")
NAME_SUFFIX_RE = re.compile(r"[-_](server|app|web|ui|frontend)$")
# How popular apps write their own names, for containers named in lower case
KNOWN_NAMES = {
    "sabnzbd": "SABnzbd", "nzbget": "NZBGet", "qbittorrent": "qBittorrent", "freshrss": "FreshRSS",
    "bookstack": "BookStack", "metube": "MeTube", "filebrowser": "File Browser", "titlecardmaker": "TitleCardMaker",
    "plexautolanguages": "Plex Auto Languages", "isponsorblocktv": "iSponsorBlockTV", "bentopdf": "BentoPDF",
    "droppedneedle": "DroppedNeedle", "flaresolverr": "FlareSolverr", "jellyseerr": "Jellyseerr",
    "homeassistant": "Home Assistant", "nodered": "Node-RED", "node-red": "Node-RED", "uptime-kuma": "Uptime Kuma",
    "adguardhome": "AdGuard Home", "adguard": "AdGuard Home", "pihole": "Pi-hole", "nginx-proxy-manager":
    "Nginx Proxy Manager", "npm": "Nginx Proxy Manager", "stirling-pdf": "Stirling-PDF", "it-tools": "IT-Tools",
    "influxdb": "InfluxDB", "zigbee2mqtt": "Zigbee2MQTT", "esphome": "ESPHome", "paperless": "Paperless-ngx",
    "paperless-ngx": "Paperless-ngx", "paperless-gpt": "Paperless-GPT", "photoprism": "PhotoPrism",
    "audiobookshelf": "Audiobookshelf", "calibre-web": "Calibre-Web", "mealie": "Mealie", "wallos": "Wallos",
    "crowdsec": "CrowdSec", "code-server": "code-server", "gitea": "Gitea", "forgejo": "Forgejo",
    "vaultwarden": "Vaultwarden", "nextcloud": "Nextcloud", "syncthing": "Syncthing", "mosquitto": "Mosquitto",
    "unifi": "UniFi", "wireguard": "WireGuard", "wg-easy": "wg-easy", "tailscale": "Tailscale",
}
WORD_CASE = {"ui": "UI", "mcp": "MCP", "ha": "HA", "gpt": "GPT", "pdf": "PDF", "tv": "TV", "api": "API", "dns": "DNS",
             "vpn": "VPN", "db": "DB", "ai": "AI", "llm": "LLM", "rss": "RSS", "ssh": "SSH", "crowdsec": "CrowdSec"}


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DockerClient:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def _conn(self):
        u = urllib.parse.urlparse(self.endpoint)
        if u.scheme == "unix":
            return _UnixConnection(u.path)
        if u.scheme in ("tcp", "http"):
            return http.client.HTTPConnection(u.hostname, u.port or 2375, timeout=10)
        if u.scheme == "https":
            return http.client.HTTPSConnection(u.hostname, u.port or 2376, timeout=10)
        raise ValueError(f"unsupported Docker endpoint {self.endpoint}")

    def get(self, path):
        conn = self._conn()
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"Docker API {path}: HTTP {resp.status} {body[:200]!r}")
            return json.loads(body)
        finally:
            conn.close()

    def containers(self):
        return self.get("/containers/json?all=1")

    def image_labels(self, image):
        try:
            return (self.get(f"/images/{urllib.parse.quote(image, safe='')}/json").get("Config") or {}).get("Labels") or {}
        except Exception:
            return {}


def fetch(client):
    out = []
    for c in client.containers():
        labels = c.get("Labels") or {}
        ports = sorted({p["PublicPort"] for p in c.get("Ports", [])
                        if p.get("PublicPort") and p.get("Type") == "tcp" and p.get("PrivatePort") != 22})
        private = sorted({p["PrivatePort"] for p in c.get("Ports", []) if p.get("PrivatePort")})
        out.append({"name": c["Names"][0].lstrip("/"), "image": c.get("Image", ""), "state": c.get("State", ""),
                    "ports": ports, "private_ports": private, "labels": labels,
                    "project": labels.get("com.docker.compose.project", ""),
                    "host_network": (c.get("HostConfig") or {}).get("NetworkMode") == "host"})
    return out


def display_name(c, names):
    if c["name"] in names:
        return names[c["name"]]
    if c["labels"].get("homelab-map.name"):
        return c["labels"]["homelab-map.name"]
    name = c["name"]
    base = NAME_SUFFIX_RE.sub("", name)
    if c["project"] and base.replace("_", "-") == c["project"].replace("_", "-"):
        name = base  # immich_server in project immich -> Immich
    key = name.lower().replace("_", "-")
    if key in KNOWN_NAMES:
        return KNOWN_NAMES[key]
    words = [w for w in re.split(r"[-_ ]+", name) if w]
    return " ".join(KNOWN_NAMES.get(w.lower()) or WORD_CASE.get(w.lower()) or (w[:1].upper() + w[1:]) for w in words)


def visible(containers, cfg):
    hide = set(cfg["hide"])
    keep = []
    for c in containers:
        if c["name"] in hide or c["labels"].get("homelab-map.hide", "").lower() in ("1", "true", "yes"):
            continue
        forced = c["name"] in cfg["groups_flat"] or "homelab-map.group" in c["labels"]
        if not forced and HELPER_RE.search(c["name"]):
            continue
        keep.append((c, forced))
    shown = [c for c, forced in keep if forced or c["ports"] or c["name"] in cfg["ports"]
             or "homelab-map.port" in c["labels"]]
    projects_shown = {c["project"] for c in shown if c["project"]}
    for c, _ in keep:  # a stack with no web UI at all still gets one box
        if c not in shown and (not c["project"] or c["project"] not in projects_shown):
            shown.append(c)
            if c["project"]:
                projects_shown.add(c["project"])
    return shown


def apply(graph, cfg, ccfg, data_dir, describe_cache):
    ccfg = dict(ccfg, groups_flat={n for names in ccfg["groups"].values() for n in names})
    for host in cfg["hosts"]:
        client = DockerClient(host["endpoint"])
        key = "docker-" + re.sub(r"[^a-z0-9]+", "-", (host.get("name") or host.get("ip") or host["endpoint"]).lower())
        raw = cached(data_dir, key, lambda: fetch(client), graph)
        if raw is None:
            continue
        # A stopped container publishes no ports: remember the last ones seen
        ports_file = os.path.join(data_dir, "cache", f"{key}-ports.json")
        known = json.load(open(ports_file, encoding="utf8")) if os.path.exists(ports_file) else {}
        for c in raw:
            if c["ports"]:
                known[c["name"]] = c["ports"]
            elif c["state"] != "running" and c["name"] in known:
                c["ports"] = known[c["name"]]
        with open(ports_file, "w", encoding="utf8") as f:
            json.dump(known, f, indent=1)
        node = graph.find(host["ip"]) if host.get("ip") else None
        if node is None and host.get("name"):
            node = graph.find(host["name"])
        if node is None:
            parent = graph.find(host["parent"]) if host.get("parent") else None
            node = graph.add(Node(f"docker:{key}", host.get("name") or host.get("ip") or "Docker host",
                                  host.get("kind", "physical"), ip=host.get("ip", ""), source="docker",
                                  parent=parent.id if parent else ""))
        elif host.get("name"):
            node.name = host["name"]
        node.docker_host = True
        for c in visible(raw, ccfg):
            if c["name"] in ccfg["ports"]:
                port = f":{ccfg['ports'][c['name']]}"
            elif c["labels"].get("homelab-map.port"):
                port = f":{c['labels']['homelab-map.port']}"
            elif c["ports"]:
                port = f":{c['ports'][0]}"  # the Caddy source may pick a better one later
            else:
                port = "host network" if c["host_network"] else "no web UI"
            graph.add(Node(f"ctr:{node.id}:{c['name']}", display_name(c, ccfg["names"]), "container",
                           parent=node.id, port=port, state="running" if c["state"] == "running" else "stopped",
                           lane_kind="group", source="docker",
                           extra={"container": c["name"], "image": c["image"], "ports": c["ports"],
                                  "private_ports": c["private_ports"], "labels": c["labels"],
                                  "project": c["project"]}))
        grouping.assign(graph, node, ccfg, lambda image: client.image_labels(image), describe_cache)
