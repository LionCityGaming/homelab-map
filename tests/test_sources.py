import os
import tempfile
import unittest
from unittest import mock

from homelab_map import config, palette
from homelab_map.grouping import guess, DEFAULT_KEYWORDS
from homelab_map.model import Graph, Node, is_virtual_mac
from homelab_map.sources import caddy, docker, unifi

CADDYFILE = """
{
    email me@example.com
}
(public) {
    import base
}
# a comment { with braces }
radarr.example.com {
    import private
    reverse_proxy http://192.168.1.12:7878
}
immich.example.com, photos.example.com {
    import public
    reverse_proxy 192.168.1.12:2283
}
jf.example.com {
    reverse_proxy jellyfin:8096
}
ha.example.com {
    import public
    reverse_proxy 192.168.1.13:8123
}
"""


def ctr(name, ports=(), project="", state="running", labels=None, private=()):
    return {"name": name, "image": f"example/{name}:latest", "state": state, "ports": list(ports),
            "private_ports": list(private), "labels": labels or {}, "project": project, "host_network": False}


class CaddyTest(unittest.TestCase):
    def test_parse(self):
        sites = caddy.parse(CADDYFILE, ["public"], [])
        self.assertEqual(sites, [("radarr.example.com", "192.168.1.12:7878", False),
                                 ("immich.example.com", "192.168.1.12:2283", True),
                                 ("jf.example.com", "jellyfin:8096", False),
                                 ("ha.example.com", "192.168.1.13:8123", True)])

    def test_public_hosts_regex(self):
        sites = caddy.parse(CADDYFILE, [], [r"^jf\."])
        self.assertTrue(dict((a, p) for a, _, p in sites)["jf.example.com"])

    def test_apply_marks_containers_and_devices(self):
        g = Graph()
        host = g.add(Node("h", "Docker", "vm", ip="192.168.1.12", docker_host=True))
        ha = g.add(Node("ha", "Home Assistant", "vm", ip="192.168.1.13"))
        for name, ports, private in (("immich_server", [2283], [2283]), ("radarr", [7878], [7878]),
                                     ("jellyfin", [], [8096])):
            g.add(Node(f"c:{name}", name, "container", parent=host.id, port=":0",
                       extra={"container": name, "ports": ports, "private_ports": private}))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(caddy, "fetch", return_value={"text": CADDYFILE}):
            caddy.apply(g, {"public_snippets": ["public"], "public_hosts": []}, d)
        self.assertTrue(g.nodes["c:immich_server"].public)
        self.assertEqual(g.nodes["c:immich_server"].url, "https://immich.example.com")
        self.assertFalse(g.nodes["c:radarr"].public)
        self.assertEqual(g.nodes["c:jellyfin"].url, "https://jf.example.com")
        self.assertTrue(ha.public)


class DockerTest(unittest.TestCase):
    CFG = {"hide": ["watchtower"], "groups_flat": set(), "ports": {}, "names": {}}

    def test_visibility(self):
        raw = [ctr("immich_server", [2283], "immich"), ctr("immich_redis", [], "immich"),
               ctr("immich_machine_learning", [], "immich"), ctr("authentik-server", [9000], "authentik"),
               ctr("authentik-worker", [], "authentik"), ctr("gitea-db", [5432], "gitea"),
               ctr("gitea", [3000], "gitea"), ctr("watchtower", [], "watchtower"),
               ctr("plexautolanguages", [], "pal"), ctr("linkding-proxy", [1112], "linkding"),
               ctr("hidden", [80], labels={"homelab-map.hide": "true"})]
        shown = {c["name"] for c in docker.visible(raw, self.CFG)}
        self.assertEqual(shown, {"immich_server", "authentik-server", "gitea", "plexautolanguages"})

    def test_display_names(self):
        self.assertEqual(docker.display_name(ctr("immich_server", project="immich"), {}), "Immich")
        self.assertEqual(docker.display_name(ctr("code-server", project="code-server"), {}), "code-server")
        self.assertEqual(docker.display_name(ctr("sabnzbd"), {}), "SABnzbd")
        self.assertEqual(docker.display_name(ctr("crowdsec_web_ui"), {}), "CrowdSec Web UI")
        self.assertEqual(docker.display_name(ctr("x", labels={"homelab-map.name": "Nice"}), {}), "Nice")
        self.assertEqual(docker.display_name(ctr("x"), {"x": "From config"}), "From config")


class GroupingTest(unittest.TestCase):
    def test_guesses(self):
        cases = {
            ("radarr", "lscr.io/linuxserver/radarr", ""): "🎬  Media Automation",
            ("audiobookshelf", "advplyr/audiobookshelf", "Self-hosted audiobook and podcast server"):
                "📚  Reading & Listening",
            ("dozzle", "amir20/dozzle", "Realtime log viewer for containers"): "📈  Monitoring",
            ("mystery", "someone/mystery", ""): None,
        }
        for (name, image, desc), want in cases.items():
            self.assertEqual(guess(name, image, desc, DEFAULT_KEYWORDS), want, name)


class PaletteTest(unittest.TestCase):
    def test_presets_and_overrides(self):
        c = palette.resolve({"palette": "vivid", "colors": {"lines": "#FF0000", "vm": {"fill": ["#111111", "#222222"]}}})
        self.assertEqual(c["lines"], ["#FF0000", "#FF0000"])
        self.assertEqual(c["vm"]["fill"], ["#111111", "#222222"])
        self.assertEqual(c["vm"]["border"], palette.PRESETS["vivid"]["vm"]["border"])
        self.assertEqual(c["background"], palette.PRESETS["default"]["background"])

    def test_bad_values(self):
        for bad in ({"palette": "nope"}, {"colors": {"nope": "#fff"}}, {"colors": {"lines": "red"}},
                    {"colors": {"vm": "#fff"}}):
            with self.assertRaises(palette.PaletteError):
                palette.resolve(bad)


class ConfigTest(unittest.TestCase):
    def test_env_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.yaml")
            with open(path, "w") as f:
                f.write("sources:\n  unifi:\n    url: https://gw\n    password: ${HM_TEST_PASS}\n")
            with mock.patch.dict(os.environ, {"HM_TEST_PASS": "s3cret"}):
                cfg = config.load(path)
            self.assertTrue(cfg["sources"]["unifi"]["enabled"])
            self.assertEqual(cfg["sources"]["unifi"]["password"], "s3cret")
            self.assertEqual(cfg["sources"]["unifi"]["clients"], "fixed_ip")
            self.assertFalse(cfg["sources"]["proxmox"]["enabled"])
            with self.assertRaises(config.ConfigError):
                config.load(path)  # variable no longer set


class UnifiTest(unittest.TestCase):
    RAW = {
        "devices": [
            {"name": "Gateway", "type": "udm", "mac": "aa:00:00:00:00:01", "ip": "1.2.3.4", "uplink_mac": None,
             "uplink_port": None},
            {"name": "Switch", "type": "usw", "mac": "aa:00:00:00:00:02", "ip": "192.168.1.2",
             "uplink_mac": "aa:00:00:00:00:01", "uplink_port": 2},
            {"name": "AP", "type": "uap", "mac": "aa:00:00:00:00:03", "ip": "192.168.1.3",
             "uplink_mac": "aa:00:00:00:00:02", "uplink_port": 8},
        ],
        "networks": [{"id": "1", "name": "LAN", "purpose": "corporate", "subnet": "192.168.1.1/24", "vpn_type": None},
                     {"id": "2", "name": "IoT", "purpose": "corporate", "subnet": "192.168.10.1/24", "vpn_type": None},
                     {"id": "3", "name": "WG", "purpose": "remote-user-vpn", "subnet": "10.8.0.1/24",
                      "vpn_type": "wireguard-server"}],
        "users": [
            {"mac": "b4:00:00:00:00:10", "name": "Proxmox", "fixed": True, "ip": "192.168.1.10", "network": "LAN",
             "uplink_mac": "aa:00:00:00:00:02", "wired": True},
            {"mac": "bc:24:11:00:00:11", "name": "Plex", "fixed": True, "ip": "192.168.1.11", "network": "LAN",
             "uplink_mac": "aa:00:00:00:00:02", "wired": True},
            {"mac": "06:11:22:00:00:12", "name": "Home Assistant", "fixed": True, "ip": "192.168.1.13",
             "network": "LAN", "uplink_mac": "aa:00:00:00:00:02", "wired": True},
            {"mac": "bc:24:11:00:00:13", "name": "Offline LXC", "fixed": True, "ip": "192.168.1.14", "network": "LAN",
             "uplink_mac": "aa:00:00:00:00:02", "wired": True},
            {"mac": "bc:24:11:00:00:14", "name": "Misreported", "fixed": True, "ip": "192.168.1.15",
             "network": "LAN", "uplink_mac": "aa:00:00:00:00:01", "wired": True},
            {"mac": "bc:24:11:00:00:15", "name": "Offline on gateway", "fixed": True, "ip": "192.168.1.16",
             "network": "LAN", "uplink_mac": "aa:00:00:00:00:01", "wired": True},
            {"mac": "c2:00:00:00:00:20", "name": "Lamp", "fixed": True, "ip": "192.168.10.5", "network": "IoT",
             "uplink_mac": "aa:00:00:00:00:03", "wired": False},
            {"mac": "c2:00:00:00:00:21", "name": "Phone", "fixed": True, "ip": "192.168.1.80", "network": "LAN",
             "uplink_mac": "aa:00:00:00:00:02", "wired": False},
        ],
        "online": [
            {"mac": "b4:00:00:00:00:10", "ip": "192.168.1.10", "network": "LAN", "wired": True,
             "uplink_mac": "aa:00:00:00:00:02", "port": 3},
            {"mac": "bc:24:11:00:00:11", "ip": "192.168.1.11", "network": "LAN", "wired": True,
             "uplink_mac": "aa:00:00:00:00:02", "port": 3},
            {"mac": "06:11:22:00:00:12", "ip": "192.168.1.13", "network": "LAN", "wired": True,
             "uplink_mac": "aa:00:00:00:00:02", "port": 3},
            {"mac": "bc:24:11:00:00:14", "ip": "192.168.1.15", "network": "LAN", "wired": True,
             "uplink_mac": "aa:00:00:00:00:01", "port": 2},
            {"mac": "c2:00:00:00:00:20", "ip": "192.168.10.5", "network": "IoT", "wired": False,
             "uplink_mac": "aa:00:00:00:00:03", "port": 8},
        ],
        "wireguard": [{"network": "WG", "name": "Laptop", "ip": "10.8.0.2"}],
    }

    def test_tree(self):
        g = Graph()
        cfg = dict(config.DEFAULTS["sources"]["unifi"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(unifi, "fetch", return_value=self.RAW):
            unifi.apply(g, cfg, d)
        n = {x.name: x for x in g.nodes.values()}
        self.assertEqual(n["Gateway"].ip, "192.168.1.1")                  # LAN address, not WAN
        self.assertEqual(n["Switch"].parent, n["Gateway"].id)
        self.assertEqual(n["AP"].parent, n["Switch"].id)
        self.assertEqual(n["Plex"].parent, n["Proxmox"].id)                 # same switch port as the host
        self.assertEqual(n["Home Assistant"].parent, n["Proxmox"].id)       # locally administered MAC
        self.assertEqual(n["Offline LXC"].parent, n["Proxmox"].id)          # offline, same MAC prefix
        self.assertEqual(n["Misreported"].parent, n["Proxmox"].id)          # reported on the gateway port
        self.assertEqual(n["Offline on gateway"].parent, n["Proxmox"].id)   # offline, remembered on the gateway
        self.assertEqual(n["Lamp"].parent, n["Gateway"].id)                 # IoT lane off the gateway
        self.assertIn("IoT", n["Lamp"].lane)
        self.assertEqual(n["Phone"].parent, n["Switch"].id)                 # Wi-Fi: never a guest
        self.assertEqual(n["Laptop"].lane_kind, "vpn")

    def test_virtual_mac(self):
        self.assertTrue(is_virtual_mac("BC:24:11:aa:bb:cc"))
        self.assertTrue(is_virtual_mac("06:11:22:00:00:00"))
        self.assertFalse(is_virtual_mac("b4:2e:99:a8:50:a2"))


if __name__ == "__main__":
    unittest.main()
