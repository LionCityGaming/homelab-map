"""The layout must never produce touching lines, lines through boxes, or overlapping boxes, for any
shape of homelab. Generates lots of random ones and checks each."""
import random
import unittest

from homelab_map.config import DEFAULTS
from homelab_map.drawio import build
from homelab_map.layout import check, layout
from homelab_map.model import Graph, Node

WORDS = ["Plex", "Home Assistant", "Radarr", "NAS", "Office Switch", "A", "Living Room Access Point",
         "Paperless-NGX", "Z", "Very Long Device Name From The Vendor Default", "Pi-hole", "📺 TV"]


def random_graph(rng):
    g = Graph()
    gw = g.add(Node("gw", "Gateway", "gateway", ip="192.168.1.1", parent="internet"))
    n = 0

    def add(kind, parent, **kw):
        nonlocal n
        n += 1
        name = rng.choice(WORDS) + ("" if rng.random() < 0.5 else f" {n}")
        return g.add(Node(f"n{n}", name, kind, ip=f"192.168.{n // 250}.{n % 250}", parent=parent, **kw))

    for _ in range(rng.randint(0, 4)):
        sw = add(rng.choice(["switch", "ap"]), gw.id)
        for _ in range(rng.randint(0, 10)):
            dev = add(rng.choice(["physical", "client"]), sw.id)
            if rng.random() < 0.3:  # a hypervisor
                for _ in range(rng.randint(0, 12)):
                    guest = add(rng.choice(["vm", "lxc"]), dev.id, state=rng.choice(["running", "stopped"]))
                    if rng.random() < 0.15:
                        guest.docker_host = True
            elif rng.random() < 0.2:
                dev.docker_host = True
            if rng.random() < 0.1:  # a nested switch
                for _ in range(rng.randint(0, 5)):
                    add("client", dev.id)
    for host in [x for x in g.nodes.values() if x.docker_host]:
        for _ in range(rng.randint(0, 40)):
            c = add("container", host.id, port=f":{rng.randint(80, 60000)}", lane_kind="group",
                    public=rng.random() < 0.2)
            c.lane = rng.choice(["🎬  Media", "⚙️  Infrastructure", "📚  Reading", "📦  Other", "🛠️  Dev"])
    for title, kind in (("📶  IoT · 192.168.10.0/24", "network"), ("🔒  WireGuard · 10.8.0.0/24", "vpn"),
                        ("📶  Guest · 192.168.30.0/24", "network")):
        if rng.random() < 0.6:
            for _ in range(rng.randint(1, 25)):
                d = add("client", gw.id)
                d.lane, d.lane_kind = title, kind
    g.repair()
    return g


class LayoutTest(unittest.TestCase):
    def test_random_homelabs_have_no_overlaps(self):
        rng = random.Random(1234)
        cfg = dict(DEFAULTS)
        for i in range(400):
            g = random_graph(rng)
            scene = layout(g, cfg, top=190)
            problems = check(scene)
            self.assertEqual(problems, [], f"homelab #{i}: {problems[:5]}")

    def test_build_writes_both_variants(self):
        g = random_graph(random.Random(7))
        light, dark, scene = build(g, dict(DEFAULTS))
        self.assertNotIn("light-dark(", light)
        self.assertNotIn("light-dark(", dark)
        self.assertIn("#1E1F24", dark)
        self.assertIn("{{UPDATED}}", light)

    def test_empty_homelab(self):
        g = Graph()
        g.repair()
        self.assertEqual(check(layout(g, dict(DEFAULTS), top=190)), [])


if __name__ == "__main__":
    unittest.main()
