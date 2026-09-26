"""Automatic layout with zero overlapping lines.

The map is a tree, laid out top to bottom (internet -> gateway -> switches -> hosts -> guests ->
containers). Every parent's subtree gets its own horizontal strip, so lines from different parents
can never meet. Within one parent, lines are routed explicitly (never auto-routed):

  - a child directly under the parent gets a straight vertical line
  - children to the left each leave the parent's bottom edge at their own point (outermost child =
    outermost point) and turn at their own horizontal level (outermost = highest), mirrored on the
    right, so the lines nest inside each other and can't cross

Groups of leaf devices are drawn as lanes (a titled box holding a grid of small boxes); a Docker
host's containers go in one "Containers" box holding a lane per group.

check() then tests every pair of line segments and every line against every box it doesn't connect
to, so a layout that breaks the rule is never published.
"""
from dataclasses import dataclass, field
import itertools

from .model import KIND_ORDER, ip_key

BOX_H = 70
GATEWAY_H = 90
ITEM_H = 60
ITEM_GAP = 20          # between items inside a lane (both ways)
LANE_HEAD = 45
LANE_PAD = 25
SIBLING_GAP = 40       # between neighbouring subtrees
EXIT_STEP = 16         # between side-by-side exit points / turning levels
EXIT_MARGIN = 18       # first exit point from the box edge
BAND_PAD = 30          # space above the first and below the last turning level
EDGE_PAD = 8           # the checker's minimum distance between two lines
BOX_PAD = 5            # ... and between a line and a box it doesn't connect to


@dataclass
class Box:
    id: str
    x: float
    y: float
    w: float
    h: float
    role: str                    # node | lane | wrapper | item | legend | swatch
    style: str
    node: object = None          # model.Node for node/item boxes
    title: str = ""              # lanes, wrappers, legend text
    parent: str = ""             # box id this box sits inside (items in lanes, lanes in wrappers)


@dataclass
class Edge:
    src: str
    dst: str
    points: list                 # absolute [start, *bends, end]


@dataclass
class Scene:
    boxes: list = field(default_factory=list)
    edges: list = field(default_factory=list)

    def top(self):
        return [b for b in self.boxes if not b.parent]


class LayoutError(Exception):
    pass


# ---------------------------------------------------------------- sizing
def text_w(text, per_char=9.0):
    # emoji and wide glyphs count double
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text) * per_char


def label_name(node):
    return ("🌐 " if node.public else "") + node.name


def node_size(node):
    if node.kind == "internet":
        return 210, 110
    w = max(text_w(label_name(node), 10.0), text_w(node.sub(), 8.2)) + 50
    return max(210, min(360, round(w))), GATEWAY_H if node.kind == "gateway" or node.docker_host else BOX_H


def node_style(node):
    if node.kind == "internet":
        return "internet"
    if node.state != "running":
        return "stopped"
    return {"vm": "vm", "lxc": "lxc", "container": "docker"}.get(node.kind, "phys")


def lane_style(lane_kind, nodes):
    if lane_kind == "group":
        return "lane_docker"
    kinds = {n.kind for n in nodes}
    if kinds <= {"vm"}:
        return "lane_vm"
    if kinds <= {"lxc"}:
        return "lane_lxc"
    if kinds <= {"vm", "lxc"}:
        return "lane_vm"
    return "lane_phys"


class _Lane:
    """A titled box with a grid of item boxes. Size is known up front."""

    def __init__(self, key, title, nodes, style, cols=3):
        self.key, self.title, self.nodes, self.style = key, title, nodes, style
        self.cols = max(1, min(cols, len(nodes)))
        rows = (len(nodes) + self.cols - 1) // self.cols
        self.cw = max(170, min(320, round(max(max(text_w(label_name(n), 8.8), text_w(n.sub(), 7.8))
                                                  for n in nodes) + 34)))
        self.w = LANE_PAD * 2 + self.cols * self.cw + (self.cols - 1) * ITEM_GAP
        self.w = max(self.w, round(text_w(title, 10) + 60))
        self.h = LANE_HEAD + ITEM_GAP + rows * (ITEM_H + ITEM_GAP)

    def place(self, scene, x, y, parent=""):
        lid = f"lane:{self.key}"
        scene.boxes.append(Box(lid, x, y, self.w, self.h, "lane", self.style, title=self.title, parent=parent))
        inner = self.w - 2 * LANE_PAD
        cw = (inner - (self.cols - 1) * ITEM_GAP) / self.cols  # stretch items to fill a widened lane
        for i, n in enumerate(self.nodes):
            r, c = divmod(i, self.cols)
            scene.boxes.append(Box(f"item:{n.id}", LANE_PAD + c * (cw + ITEM_GAP),
                                   LANE_HEAD + ITEM_GAP + r * (ITEM_H + ITEM_GAP), cw, ITEM_H, "item",
                                   node_style(n), node=n, parent=lid))
        return lid


class _Wrapper:
    """A Docker host's containers: one box holding a lane per group, in a few columns."""

    def __init__(self, key, title, lanes, columns):
        self.key, self.title, self.lanes = key, title, lanes
        ncols = max(1, min(columns, len(lanes)))
        self.cols = [[] for _ in range(ncols)]
        heights = [0] * ncols
        for lane in lanes:  # fill the shortest column next, keeping the order readable
            c = heights.index(min(heights))
            self.cols[c].append(lane)
            heights[c] += lane.h + ITEM_GAP
        self.colw = [max(l.w for l in col) if col else 0 for col in self.cols]
        for col, cw in zip(self.cols, self.colw):
            for lane in col:
                lane.w = cw
        self.w = LANE_PAD * 2 + sum(self.colw) + (ncols - 1) * ITEM_GAP
        self.w = max(self.w, round(text_w(title, 10) + 60))
        self.h = LANE_HEAD + ITEM_GAP + max(heights) + LANE_PAD - ITEM_GAP

    def place(self, scene, x, y):
        wid = f"wrap:{self.key}"
        scene.boxes.append(Box(wid, x, y, self.w, self.h, "wrapper", "wrapper", title=self.title))
        cx = LANE_PAD
        for col, cw in zip(self.cols, self.colw):
            cy = LANE_HEAD + ITEM_GAP
            for lane in col:
                lane.place(scene, cx, cy, parent=wid)
                cy += lane.h + ITEM_GAP
            cx += cw + ITEM_GAP
        return wid


# ---------------------------------------------------------------- tree
class _Tree:
    """One node and the row of things hanging off it."""

    def __init__(self, node, depth):
        self.node, self.depth = node, depth
        self.w, self.h = node_size(node)
        self.items = []          # _Tree | _Lane | _Wrapper, left to right
        self.x = self.y = 0      # of the node's own box
        self.sub_w = 0


def _sort_key(n):
    return (KIND_ORDER.get(n.kind, 99), ip_key(n.ip), n.name.lower())


def build_tree(graph, cfg):
    children = {}
    for n in graph.nodes.values():
        if n.parent:
            children.setdefault(n.parent, []).append(n)
    threshold = cfg["layout"]["lane_threshold"]
    group_order = list(cfg["containers"]["groups"])

    def make(node, depth):
        t = _Tree(node, depth)
        kids = sorted(children.get(node.id, []), key=_sort_key)
        containers = [k for k in kids if k.kind == "container"]
        kids = [k for k in kids if k.kind != "container"]
        lanes = {}
        for k in kids:
            if k.lane:
                lanes.setdefault((k.lane_kind, k.lane), []).append(k)
        boxes = [k for k in kids if not k.lane]
        leaves = [k for k in boxes if k.id not in children]
        if len(leaves) > threshold:  # too many to draw one by one: collect them in a lane
            boxes = [k for k in boxes if k.id in children]
            virtual = all(k.kind in ("vm", "lxc") for k in leaves)
            lanes[("auto", "🖥️  Guests" if virtual else "💻  Devices")] = leaves
        vpn = [(key, v) for key, v in lanes.items() if key[0] == "vpn"]
        other = [(key, v) for key, v in lanes.items() if key[0] != "vpn"]
        for (kind, title), members in vpn:
            t.items.append(_Lane(f"{node.id}:{title}", title, members, lane_style(kind, members)))
        t.items += [make(k, depth + 1) for k in boxes]
        for (kind, title), members in other:
            t.items.append(_Lane(f"{node.id}:{title}", title, members, lane_style(kind, members)))
        if containers:
            groups = {}
            for c in containers:
                groups.setdefault(c.lane, []).append(c)

            def gkey(title):
                return (0, group_order.index(title)) if title in group_order else \
                    (2, "") if title.endswith("Other") else (1, title.split("  ", 1)[-1].lower())
            glanes = [_Lane(f"{node.id}:{g}", g, sorted(m, key=lambda c: c.name.lower()), "lane_docker")
                      for g, m in sorted(groups.items(), key=lambda kv: gkey(kv[0]))]
            t.items.append(_Wrapper(node.id, "🐳  Containers", glanes, cfg["containers"]["lane_columns"]))
        return t

    return make(graph.nodes["internet"], 0)


def _measure(t):
    row = 0
    for it in t.items:
        row += (_measure(it) if isinstance(it, _Tree) else it.w)
    row += SIBLING_GAP * max(0, len(t.items) - 1)
    # room for this node's exit points on each side
    side = (len(t.items) + 1) // 2
    t.w = max(t.w, 2 * (EXIT_MARGIN + side * EXIT_STEP + EDGE_PAD + 4))
    t.row_w = row
    t.sub_w = max(t.w, row)
    return t.sub_w


def _item_w(it):
    return it.sub_w if isinstance(it, _Tree) else it.w


def _place_x(t, left):
    start = left + (t.sub_w - t.row_w) / 2
    centres = []
    x = start
    for it in t.items:
        w = _item_w(it)
        if isinstance(it, _Tree):
            _place_x(it, x)
            centres.append(it.x + it.w / 2)
        else:
            it.x = x
            centres.append(x + w / 2)
        x += w + SIBLING_GAP
    row_centre = start + t.row_w / 2 if t.items else left + t.sub_w / 2
    # centre the node over the item nearest the middle when that one is close, so it gets a straight line
    near = min(centres, key=lambda c: abs(c - row_centre), default=row_centre)
    cx = near if abs(near - row_centre) <= t.w / 2 else row_centre
    t.x = min(max(cx - t.w / 2, left), left + t.sub_w - t.w)


def _collect(t, rows):
    rows.setdefault(t.depth, []).append(t)
    for it in t.items:
        if isinstance(it, _Tree):
            _collect(it, rows)


def layout(graph, cfg, top=0):
    root = build_tree(graph, cfg)
    _measure(root)
    _place_x(root, 0)
    rows = {}
    _collect(root, rows)

    # Rows: node boxes of one depth share a top line; lanes/boxes hanging off them start below the band
    scene = Scene()
    row_top = top
    bands = {}
    for depth in sorted(rows):
        trees = rows[depth]
        row_h = max(t.h for t in trees)
        for t in trees:
            t.y = row_top
        band = BAND_PAD * 2
        for t in trees:
            left = sum(1 for c in _centres(t) if c < t.x + t.w / 2 - 1)
            right = sum(1 for c in _centres(t) if c > t.x + t.w / 2 + 1)
            band = max(band, BAND_PAD * 2 + max(left, right) * EXIT_STEP)
        bands[depth] = (row_top + row_h, band)
        row_top += row_h + band

    for depth in sorted(rows):
        band_top, band = bands[depth]
        for t in rows[depth]:
            _fit(t)
            scene.boxes.append(Box(f"node:{t.node.id}", t.x, t.y, t.w, t.h, "node", node_style(t.node), node=t.node))
            targets = []
            for it in t.items:
                if isinstance(it, _Tree):
                    targets.append((f"node:{it.node.id}", it.x + it.w / 2, band_top + band))
                else:
                    iid = it.place(scene, it.x, band_top + band)
                    targets.append((iid, it.x + it.w / 2, band_top + band))
            _fanout(scene, t, targets, band_top)
    return scene


def _centres(t):
    return [it.x + it.w / 2 if isinstance(it, _Tree) else it.x + it.w / 2 for it in t.items]


MIN_STEP = EDGE_PAD + 3


def _fit(t):
    """Make sure each side's exit points fit in the node's half, clear of a straight line down the
    middle: tighten their spacing first, then widen the box (around its centre) if that isn't enough."""
    pcx = t.x + t.w / 2
    n = max(sum(1 for c in _centres(t) if c < pcx - 1), sum(1 for c in _centres(t) if c > pcx + 1))
    room = t.w / 2 - EXIT_MARGIN - EDGE_PAD - 2
    t.step = EXIT_STEP if n <= 1 else min(EXIT_STEP, room / (n - 1)) if n > 1 else EXIT_STEP
    if t.step < MIN_STEP:
        t.step = MIN_STEP
        t.w = 2 * (EXIT_MARGIN + (n - 1) * MIN_STEP + EDGE_PAD + 2)
        t.x = pcx - t.w / 2


def _fanout(scene, t, targets, band_top):
    src = f"node:{t.node.id}"
    px, pw, pb = t.x, t.w, t.y + t.h
    pcx = px + pw / 2
    step = getattr(t, "step", EXIT_STEP)
    left = sorted((tg for tg in targets if tg[1] < pcx - 1), key=lambda tg: tg[1])
    right = sorted((tg for tg in targets if tg[1] > pcx + 1), key=lambda tg: -tg[1])
    for dst, cx, ctop in targets:
        if abs(cx - pcx) <= 1:
            scene.edges.append(Edge(src, dst, [(pcx, pb), (pcx, ctop)]))
    for side, sign, edge_x in ((left, 1, px), (right, -1, px + pw)):
        for i, (dst, cx, ctop) in enumerate(side):
            ex = edge_x + sign * (EXIT_MARGIN + i * step)
            lvl = band_top + BAND_PAD + i * EXIT_STEP
            scene.edges.append(Edge(src, dst, [(ex, pb), (ex, lvl), (cx, lvl), (cx, ctop)]))


# ---------------------------------------------------------------- check
def _segs(pts):
    return list(zip(pts, pts[1:]))


def _near(a, b, pad):
    (ax1, ay1), (ax2, ay2) = a
    (bx1, by1), (bx2, by2) = b
    return (min(ax1, ax2) - pad <= max(bx1, bx2) and min(bx1, bx2) - pad <= max(ax1, ax2) and
            min(ay1, ay2) - pad <= max(by1, by2) and min(by1, by2) - pad <= max(ay1, ay2))


def check(scene):
    """Problems as text: lines that touch or cross, lines through boxes, overlapping boxes."""
    problems = []
    boxes = {b.id: b for b in scene.top()}
    for e1, e2 in itertools.combinations(scene.edges, 2):
        for i, a in enumerate(_segs(e1.points)):
            for j, b in enumerate(_segs(e2.points)):
                if e1.src == e2.src and i == 0 and j == 0:
                    continue  # stubs leaving the same box sit side by side
                if _near(a, b, EDGE_PAD):
                    problems.append(f"line {e1.src} -> {e1.dst} touches {e2.src} -> {e2.dst}")
    for e in scene.edges:
        for b in boxes.values():
            if b.id in (e.src, e.dst):
                continue
            for s in _segs(e.points):
                if _near(s, ((b.x, b.y), (b.x + b.w, b.y + b.h)), BOX_PAD):
                    problems.append(f"line {e.src} -> {e.dst} passes through {b.id}")
    for b1, b2 in itertools.combinations(boxes.values(), 2):
        if _near(((b1.x, b1.y), (b1.x + b1.w, b1.y + b1.h)), ((b2.x, b2.y), (b2.x + b2.w, b2.y + b2.h)), -1):
            problems.append(f"boxes {b1.id} and {b2.id} overlap")
    return sorted(set(problems))
