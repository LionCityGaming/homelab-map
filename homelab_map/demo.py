"""A made-up homelab, for trying homelab-map before configuring anything (and for the README image).

  python -m homelab_map demo       writes data/output/demo.* (PNG/PDF too if the renderer is up)
"""
from .model import Graph, Node

GROUPS = {
    "🎬  Media Automation": [("Radarr", ":7878"), ("Sonarr", ":8989"), ("Prowlarr", ":9696"), ("Bazarr", ":6767"),
                            ("SABnzbd", ":8080"), ("Jellyseerr", ":5055")],
    "📺  Media Servers": [("Jellyfin", ":8096"), ("Tautulli", ":8181")],
    "📁  Photos & Documents": [("Immich", ":2283"), ("Paperless-ngx", ":8000")],
    "🧠  Knowledge & Productivity": [("BookStack", ":6875"), ("FreshRSS", ":8082"), ("Mealie", ":9925")],
    "⚙️  Infrastructure": [("Authentik", ":9000"), ("Homepage", ":3000"), ("Uptime Kuma", ":3001")],
    "🛠️  Dev & Tools": [("Gitea", ":3030"), ("code-server", ":8443")],
}
PUBLIC = {"Jellyseerr", "Jellyfin", "Immich", "Mealie", "Authentik"}
STOPPED = {"Bazarr"}


def graph():
    g = Graph("Fibre ISP")
    add = g.add
    add(Node("gw", "Gateway", "gateway", ip="192.168.1.1", parent="internet"))
    add(Node("sw1", "Office Switch", "switch", ip="192.168.1.2", parent="gw"))
    add(Node("sw2", "Garage Switch", "switch", ip="192.168.1.3", parent="gw"))
    add(Node("ap1", "Living Room AP", "ap", ip="192.168.1.4", parent="gw"))
    add(Node("pve", "Proxmox", "physical", ip="192.168.1.10", parent="sw1"))
    add(Node("nas", "NAS", "physical", ip="192.168.1.20", parent="sw1"))
    add(Node("pc", "Desktop PC", "physical", ip="192.168.1.50", parent="sw1"))
    add(Node("pi", "Raspberry Pi", "physical", ip="192.168.1.30", parent="sw2"))
    add(Node("printer", "3D Printer", "physical", ip="192.168.1.40", parent="sw2"))
    add(Node("laptop", "Laptop", "client", ip="192.168.1.60", parent="ap1"))
    add(Node("tablet", "Tablet", "client", ip="192.168.1.61", parent="ap1"))
    add(Node("docker", "Docker", "vm", ip="192.168.1.12", parent="pve", docker_host=True))
    add(Node("ha", "Home Assistant", "vm", ip="192.168.1.13", parent="pve", public=True,
             url="https://ha.example.com"))
    add(Node("dns", "Pi-hole", "lxc", ip="192.168.1.53", parent="pve"))
    add(Node("caddy", "Caddy", "lxc", ip="192.168.1.17", parent="pve"))
    add(Node("vw", "Vaultwarden", "lxc", ip="192.168.1.18", parent="pve", public=True,
             url="https://vault.example.com"))
    add(Node("pbs", "Proxmox Backup", "vm", ip="192.168.1.16", parent="nas"))
    add(Node("dev", "Dev VM", "vm", ip="192.168.1.31", parent="pve", state="stopped"))
    for title, items in GROUPS.items():
        for name, port in items:
            add(Node(f"c:{name}", name, "container", parent="docker", port=port, lane=title, lane_kind="group",
                     public=name in PUBLIC, state="stopped" if name in STOPPED else "running",
                     url=f"https://{name.lower().replace(' ', '')}.example.com" if name in PUBLIC else ""))
    for i, name in enumerate(["Kitchen Light", "Hallway Light", "Thermostat", "Doorbell", "TV", "Robot Vacuum"]):
        add(Node(f"iot{i}", name, "client", ip=f"192.168.10.{20 + i}", parent="gw",
                 lane="📶  IoT · 192.168.10.0/24", lane_kind="network"))
    for i, name in enumerate(["Phone", "Laptop", "Tablet"]):
        add(Node(f"wg{i}", name, "vpn", ip=f"10.8.0.{2 + i}", parent="gw",
                 lane="🔒  WireGuard · 10.8.0.0/24", lane_kind="vpn"))
    g.repair()
    return g
