"""Automatic layout with zero overlapping lines.

The map is a tree, laid out top to bottom in rows (internet -> gateway -> switches -> hosts ->
guests -> containers). Every parent's subtree gets its own horizontal strip, so lines from
different parents can never meet. Within one parent, lines are routed explicitly (never
auto-routed):

  - a child directly under the parent gets a straight vertical line
  - children to the left each leave the parent's bottom edge at their own point (outermost child =
    outermost point) and turn at their own horizontal level (outermost = highest), mirrored on the
    right, so the lines nest inside each other and can't cross

The arrangement:
  - VPN clients sit in a lane beside the gateway, joined by a line out of its left side; other
    networks (IoT, guest) are lanes at the right end of the row below, joined by lines out of its
    right side (the outermost lane leaves highest)
  - a host's VMs and LXCs are drawn one by one, split either side of a straight line down to its
    Docker VM, which sits a row lower
  - a Docker host's containers are group lanes in a grid below it, each lane with its own line,
    running down a channel in the gap to the lane's left and entering its side (lower lanes use
    channels further out, so these nest too); the Docker host sits over the grid's middle column,
    clear of every channel

check() then tests every pair of line segments and every line against every box it doesn't connect
to, so a layout that breaks the rule is never published.
"""
from dataclasses import dataclass, field
import functools
import itertools

from .model import KIND_ORDER, ip_key

BOX_H = 70
GATEWAY_H = 90
ITEM_H = 60
ITEM_GAP = 20          # between items inside a lane (both ways)
LANE_HEAD = 45
LANE_PAD = 25
SIBLING_GAP = 40       # between neighbouring subtrees
LANE_STACK_GAP = 40    # between lanes in a grid column
STACK_GAP = 40         # between things stacked in a column
EXIT_STEP = 16         # between side-by-side exit points / turning levels
EXIT_MARGIN = 18       # first exit point from the box edge
SIDE_EXIT_TOP = 22     # first exit on a node's side, from its top
BAND_PAD = 30          # space above the first and below the last turning level
CHANNEL_GAP = 20       # from a grid column to its nearest channel
CHANNEL_STEP = 14      # between neighbouring channels
EDGE_PAD = 8           # the checker's minimum distance between two lines
BOX_PAD = 5            # ... and between a line and a box it doesn't connect to
MIN_STEP = EDGE_PAD + 3
CLEAR = EDGE_PAD + 4   # extra room beside a grid column's channels


@dataclass
class Box:
    id: str
    x: float
    y: float
    w: float
    h: float
    role: str                    # node | lane | item | legend | swatch
    style: str
    node: object = None          # model.Node for node/item boxes
    title: str = ""              # lanes, legend text
    parent: str = ""             # box id this box sits inside (items in lanes)


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
@functools.lru_cache(maxsize=8192)
def text_w(text, per_char=9.0):
    # emoji and wide glyphs count double
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text) * per_char


def label_name(node):
    return ("🌐 " if node.public else "") + node.name


def node_size(node):
    return _size(node.kind, label_name(node), node.sub(), node.docker_host)


@functools.lru_cache(maxsize=8192)
def _size(kind, name, sub, docker_host):
    if kind == "internet":
        return 210, 110
    w = max(text_w(name, 10.0), text_w(sub, 8.2)) + 50
    return max(210, min(360, round(w))), GATEWAY_H if kind == "gateway" or docker_host else BOX_H


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


# ---------------------------------------------------------------- building blocks
class _Lane:
    """A titled box with a grid of item boxes."""

    def __init__(self, key, title, nodes, style, cols=3):
        self.key, self.title, self.nodes, self.style = key, title, nodes, style
        self.cols = max(1, min(cols, len(nodes)))
        rows = (len(nodes) + self.cols - 1) // self.cols
        self.cw = max(170, min(320, round(max(max(text_w(label_name(n), 8.8), text_w(n.sub(), 7.8))
                                                  for n in nodes) + 34)))
        self.w = LANE_PAD * 2 + self.cols * self.cw + (self.cols - 1) * ITEM_GAP
        self.w = max(self.w, round(text_w(title, 10) + 60))
        self.h = LANE_HEAD + ITEM_GAP + rows * (ITEM_H + ITEM_GAP)
        self.x = self.y = 0
        self.side = False        # joined from the parent's side (gateway network lanes)

    def place(self, scene, x, y):
        self.x, self.y = x, y
        lid = f"lane:{self.key}"
        scene.boxes.append(Box(lid, x, y, self.w, self.h, "lane", self.style, title=self.title))
        inner = self.w - 2 * LANE_PAD
        cw = (inner - (self.cols - 1) * ITEM_GAP) / self.cols  # stretch items to fill a widened lane
        for i, n in enumerate(self.nodes):
            r, c = divmod(i, self.cols)
            scene.boxes.append(Box(f"item:{n.id}", LANE_PAD + c * (cw + ITEM_GAP),
                                   LANE_HEAD + ITEM_GAP + r * (ITEM_H + ITEM_GAP), cw, ITEM_H, "item",
                                   node_style(n), node=n, parent=lid))
        return lid


class _Grid:
    """A Docker host's container groups: lanes in an odd number of columns, each lane with its own
    line. Lane i of a column is fed by the i-th channel out from that column's left edge."""

    def __init__(self, lanes, columns):
        n = max(1, min(columns, len(lanes)))
        if n % 2 == 0:  # odd, so there's a middle column for the host to sit over
            n = n + 1 if len(lanes) > n else n - 1
        self.cols = [[] for _ in range(n)]
        heights = [0] * n
        for lane in lanes:  # fill the shortest column next, keeping the order readable
            c = heights.index(min(heights))
            self.cols[c].append(lane)
            heights[c] += lane.h + LANE_STACK_GAP
        self.mid = n // 2
        self.colw = [max(l.w for l in col) for col in self.cols]
        self.h = max(heights) - LANE_STACK_GAP
        self.x = 0
        self._layout()

    def widen_middle(self, width):
        """The host sits over the middle column, which must be at least as wide as the host."""
        if self.colw[self.mid] < width:
            self.colw[self.mid] = width
            self._layout()

    def _layout(self):
        self.col_x, x = [], 0
        for col, cw in zip(self.cols, self.colw):
            x += CHANNEL_GAP + (len(col) - 1) * CHANNEL_STEP + CLEAR  # room for this column's channels
            self.col_x.append(x)
            x += cw
        self.w = x
        for col, cw in zip(self.cols, self.colw):
            for lane in col:
                lane.w = cw

    def mid_centre(self):
        return self.col_x[self.mid] + self.colw[self.mid] / 2

    def lane_count(self):
        return sum(len(c) for c in self.cols)

    def channel_xs(self):
        return [self.x + cx - CHANNEL_GAP - i * CHANNEL_STEP
                for col, cx in zip(self.cols, self.col_x) for i in range(len(col))]

    def place(self, scene, y):
        """Place the lanes; return one fanout target per lane: (dst, channel x, entry y, entry x)."""
        targets = []
        for col, cx in zip(self.cols, self.col_x):
            ly = y
            for i, lane in enumerate(col):
                dst = lane.place(scene, self.x + cx, ly)
                targets.append((dst, self.x + cx - CHANNEL_GAP - i * CHANNEL_STEP, ly + LANE_HEAD / 2, self.x + cx))
                ly += lane.h + LANE_STACK_GAP
        return targets


class _Col:
    """A parent's children stacked in one column instead of side by side, using space that would
    otherwise be empty below a short one. The top one is fed from above as usual; each one below is fed
    by its own channel down the column's left side and enters from the side (lower ones use channels
    further out, so the lines nest). The parent sits clear of the channels."""

    def __init__(self, members):
        self.members = members
        self.margin = CHANNEL_GAP + (len(members) - 2) * CHANNEL_STEP + CLEAR
        self.w = self.x = 0

    def measure(self):
        self.w = self.margin + max(_item_w(m) for m in self.members)

    def channel(self, j):
        return self.x + self.margin - CHANNEL_GAP - (j - 1) * CHANNEL_STEP

    def channels(self):
        return [self.channel(j) for j in range(1, len(self.members))]

    def top_x(self):
        m = self.members[0]
        return m.x + m.w / 2


# ---------------------------------------------------------------- tree
class _Tree:
    """One node and the row of things hanging off it."""

    def __init__(self, node, depth):
        self.node, self.depth = node, depth
        self.w, self.h = node_size(node)
        self.items = []          # _Tree | _Lane, left to right (the row below)
        self.grid = None         # _Grid of container groups, for a Docker host
        self.hoisted = None      # a Docker host child drawn a row lower, between split guests
        self.split = 0           # with hoisted: how many row items go left of it
        self.vpn = None          # a lane beside this node (the gateway's VPN clients)
        self.vpn_pad = 0         # room kept on the left for it
        self.x = self.y = 0      # of the node's own box
        self.sub_w = self.row_w = 0
        self.step = EXIT_STEP


def _is_leaf(it):
    return isinstance(it, _Lane) or (isinstance(it, _Tree) and not it.items and not it.grid and not it.hoisted)


def _col_of(t):
    return next((it for it in t.items if isinstance(it, _Col)), None)


def _shift_depth(it, by):
    if isinstance(it, _Tree):
        it.depth += by
        for c in it.items + ([it.hoisted] if it.hoisted else []):
            _shift_depth(c, by)
    elif isinstance(it, _Col):
        for m in it.members:
            _shift_depth(m, by)
    else:
        it.depth = getattr(it, "depth", 0) + by


def _max_depth(it):
    """The deepest row anything in `it` reaches (lanes and grids hang one row below their node)."""
    if isinstance(it, _Tree):
        d = it.depth + (1 if (any(isinstance(c, _Lane) for c in it.items) or it.grid) else 0)
        for c in it.items + ([it.hoisted] if it.hoisted else []):
            d = max(d, _max_depth(c))
        return d
    if isinstance(it, _Col):
        return max(_max_depth(m) for m in it.members)
    return it.depth


def _sort_key(n):
    return (KIND_ORDER.get(n.kind, 99), ip_key(n.ip), n.name.lower())


def _children(graph):
    """Each node's children, sorted once (the search builds the tree many times)."""
    children = {}
    for n in graph.nodes.values():
        if n.parent:
            children.setdefault(n.parent, []).append(n)
    for kids in children.values():
        kids.sort(key=_sort_key)
    return children


def build_tree(graph, cfg, opts=None, children=None):
    opts = opts or {"lane_cols": {}, "grid_cols": {}}
    if children is None:
        children = _children(graph)
    threshold = cfg["layout"]["lane_threshold"]
    group_order = list(cfg["containers"]["groups"])

    def make(node, depth):
        t = _Tree(node, depth)
        kids = children.get(node.id, [])
        containers = [k for k in kids if k.kind == "container"]
        kids = [k for k in kids if k.kind != "container"]
        lanes = {}
        for k in kids:
            if k.lane:
                lanes.setdefault((k.lane_kind, k.lane), []).append(k)
        boxes = [k for k in kids if not k.lane]
        # many plain devices (not VMs or LXCs, which are always drawn one by one) go in a lane
        leaves = [k for k in boxes if k.id not in children and k.kind not in ("vm", "lxc")]
        if len(leaves) > threshold:
            boxes = [k for k in boxes if k not in leaves]
            lanes[("auto", "💻  Devices")] = leaves
        def lanes_of(kinds):
            return [_Lane(f"{node.id}:{title}", title, m, lane_style(kind, m),
                          cols=opts["lane_cols"].get(f"{node.id}:{title}", 3))
                    for (kind, title), m in lanes.items() if kind in kinds]
        vpn, networks, auto = lanes_of({"vpn"}), lanes_of({"network"}), lanes_of({"auto", "group"})
        if node.kind == "gateway":
            if vpn:
                t.vpn = vpn.pop(0)
            for lane in networks[-4:]:  # joined from the gateway's right side (four fit on it)
                lane.side = True
        # networks last, so the ones joined from the side are at the right end of the row
        t.items = vpn + [make(k, depth + 1) for k in boxes] + auto + networks
        if containers:
            groups = {}
            for c in containers:
                groups.setdefault(c.lane, []).append(c)

            def gkey(title):
                return (0, group_order.index(title)) if title in group_order else \
                    (2, "") if title.endswith("Other") else (1, title.split("  ", 1)[-1].lower())
            t.grid = _Grid([_Lane(f"{node.id}:{g}", g, sorted(m, key=lambda c: c.name.lower()), "lane_docker",
                                  cols=opts["lane_cols"].get(f"{node.id}:{g}", 3))
                            for g, m in sorted(groups.items(), key=lambda kv: gkey(kv[0]))],
                           opts["grid_cols"].get(node.id, cfg["containers"]["lane_columns"]))
        # a host whose only non-leaf child is a Docker host (with just containers): draw that one a
        # row lower, under a straight line, with the rest split either side of it
        dockers = [it for it in t.items if isinstance(it, _Tree) and it.grid and not it.items]
        # (only beside plain boxes: a lane can be tall enough to reach down into the grid)
        if len(dockers) == 1 and not t.grid and not t.vpn and all(
                isinstance(it, _Tree) and _is_leaf(it) for it in t.items if it is not dockers[0]):
            d = dockers[0]
            t.items.remove(d)
            t.hoisted, t.split = d, (len(t.items) + 1) // 2
            d.depth = depth + 2
        for it in t.items:
            if isinstance(it, _Lane):
                it.depth = depth + 1
        stack = opts.get("stack", {}).get(node.id)
        if stack and not t.hoisted:
            i, j, reverse = stack
            movable = [it for it in t.items if not (isinstance(it, _Lane) and it.side)]
            if 0 <= i < j < len(movable):
                members = movable[i:j + 1]
                if reverse:
                    members = members[::-1]
                # each lower one starts below everything above it (rows are fixed up again after placing)
                for a, b in zip(members, members[1:]):
                    _shift_depth(b, _max_depth(a) + 1 - (depth + 1))
                pos = t.items.index(movable[i])
                t.items = [it for it in t.items if it not in members]
                t.items.insert(pos, _Col(members))
        return t

    return make(graph.nodes["internet"], 0)


def _measure(t):
    for it in t.items:
        if isinstance(it, _Tree):
            _measure(it)
        elif isinstance(it, _Col):
            for m in it.members:
                if isinstance(m, _Tree):
                    _measure(m)
            it.measure()
    if t.grid:
        n = t.grid.lane_count() + len(t.items)
        if n > 1:  # room for one exit per line on the busier side
            t.w = max(t.w, 2 * (EXIT_MARGIN + (n - 1) * MIN_STEP + EDGE_PAD + 2))
        t.grid.widen_middle(t.w)
    if t.hoisted:
        _measure(t.hoisted)
    widths = [_item_w(it) for it in t.items]
    if t.hoisted:
        # left items, a gap for the straight line down, right items; the Docker host and its grid are
        # centred on that gap, below
        left_w = sum(widths[:t.split]) + SIBLING_GAP * max(0, t.split - 1)
        right_w = sum(widths[t.split:]) + SIBLING_GAP * max(0, len(widths) - t.split - 1)
        gap = 2 * SIBLING_GAP
        d = t.hoisted
        lo = max(left_w + gap / 2 if t.split else 0, d.grid.mid_centre(), d.w / 2, t.w / 2)
        hi = max(right_w + gap / 2 if len(widths) > t.split else 0, d.grid.w - d.grid.mid_centre(), d.w / 2, t.w / 2)
        t.anchor, t.row_w, t.sub_w = lo, lo + hi, lo + hi
        return t.sub_w
    row = sum(widths) + SIBLING_GAP * max(0, len(widths) - 1)
    if t.grid:
        row += (SIBLING_GAP if widths else 0) + t.grid.w
    # room for this node's exit points on each side
    side = (len(t.items) + 1) // 2
    t.w = max(t.w, 2 * (EXIT_MARGIN + side * EXIT_STEP + EDGE_PAD + 4))
    t.row_w = row
    t.sub_w = max(t.w, row)
    if _col_of(t):
        # sized for all its lines now (so it never widens later), then placed once to see whether
        # moving it clear of the column's channels needs more room on the right
        n = _line_count(t)
        if n > 1:
            t.w = max(t.w, 2 * (EXIT_MARGIN + (n - 1) * MIN_STEP + EDGE_PAD + 2))
            t.sub_w = max(t.sub_w, t.w)
        _place_x(t, 0)
        t.sub_w = max(t.sub_w, t.x + t.w)
    if t.vpn:
        # the VPN lane sits just left of the node. Size the node for all its lines now (so it never
        # widens later), see exactly where it lands, and reserve only the room the lane still needs.
        n = len([it for it in t.items if not (isinstance(it, _Lane) and it.side)])
        if n > 1:
            t.w = max(t.w, 2 * (EXIT_MARGIN + (n - 1) * MIN_STEP + EDGE_PAD + 2))
            t.sub_w = max(t.sub_w, t.w)
        t.vpn_pad = 0
        _place_x(t, 0)
        t.vpn_pad = max(0, t.vpn.w + SIBLING_GAP - t.x)
        t.sub_w += t.vpn_pad
    return t.sub_w


def _item_w(it):
    return it.sub_w if isinstance(it, _Tree) else it.w


def _line_count(t):
    """How many lines leave t's bottom edge."""
    return (sum(len(it.members) if isinstance(it, _Col) else 1 for it in t.items
                if not (isinstance(it, _Lane) and it.side))
            + (1 if t.hoisted else 0) + (t.grid.lane_count() if t.grid else 0))


def _place_x(t, left):
    if t.vpn:  # room kept on the left for the VPN lane, if the node needed it (see _measure)
        outer_left = left
        left += t.vpn_pad
        inner = t.sub_w - t.vpn_pad
    else:
        inner = t.sub_w
    if t.hoisted:
        centre = left + t.anchor
        x = centre - SIBLING_GAP - sum(_item_w(it) for it in t.items[:t.split]) - SIBLING_GAP * max(0, t.split - 1)
        for i, it in enumerate(t.items):
            if i == t.split:
                x = centre + SIBLING_GAP
            _place_item(it, x)
            x += _item_w(it) + SIBLING_GAP
        t.x = centre - t.w / 2
        d = t.hoisted
        _place_x(d, centre - d.grid.mid_centre())
        return
    start = left + (inner - t.row_w) / 2
    if _col_of(t) and t.sub_w > max(t.row_w, t.w):
        start = left  # the extra room is on the right, for the node clear of the column's channels
    centres = []
    x = start
    for it in t.items:
        _place_item(it, x)
        if not isinstance(it, _Col):  # never centre over a column: its lower lines come in from the side
            centres.append(x + (it.x - x + it.w / 2 if isinstance(it, _Tree) else it.w / 2))
        x += _item_w(it) + SIBLING_GAP
    if t.grid:
        t.grid.x = x if t.items else start
        t.x = t.grid.x + t.grid.mid_centre() - t.w / 2  # over the middle column, clear of all channels
        if t.vpn:
            t.vpn.x = max(t.x - SIBLING_GAP - t.vpn.w, outer_left)
        return
    row_centre = start + t.row_w / 2 if t.items else left + inner / 2
    # centre the node over the item nearest the middle when that one is close, so it gets a straight line
    near = min(centres, key=lambda c: abs(c - row_centre), default=row_centre)
    cx = near if abs(near - row_centre) <= t.w / 2 or len(centres) == 1 else row_centre
    t.x = min(max(cx - t.w / 2, left), left + inner - t.w)
    col = _col_of(t)
    if col:  # the column's channels must not run under the node, or its lines would turn both ways
        chs = col.channels()
        lo, hi = min(chs) - CLEAR, max(chs) + CLEAR
        if not (t.x + t.w < lo or t.x > hi):
            t.x = hi
    if t.vpn:
        t.vpn.x = max(t.x - SIBLING_GAP - t.vpn.w, outer_left)


def _place_item(it, x):
    if isinstance(it, _Tree):
        _place_x(it, x)
    elif isinstance(it, _Col):
        it.x = x
        for m in it.members:
            _place_item(m, x + it.margin)
    else:
        it.x = x


def _collect(t, rows):
    rows.setdefault(t.depth, []).append(t)
    for it in t.items + ([t.hoisted] if t.hoisted else []):
        for m in (it.members if isinstance(it, _Col) else [it]):
            if isinstance(m, _Tree):
                _collect(m, rows)


def _from_side(t, it, margin=20):
    """A network lane is joined from the node's right side only if it really is to the right."""
    return isinstance(it, _Lane) and it.side and it.x + it.w / 2 > t.x + t.w + margin


def _targets_x(t, margin=20):
    """Where each line out of t's bottom arrives, horizontally."""
    xs = []
    for it in t.items:
        if isinstance(it, _Col):
            xs += [it.top_x()] + it.channels()
        elif not _from_side(t, it, margin):
            xs.append(it.x + it.w / 2)
    if t.hoisted:
        xs.append(t.hoisted.x + t.hoisted.w / 2)
    if t.grid:
        xs += t.grid.channel_xs()
    return xs


def _fit(t):
    """Each side's exit points must fit in the node's half, clear of a straight line down the middle:
    tighten their spacing first, then widen the box (around its centre) if that isn't enough."""
    pcx = t.x + t.w / 2
    xs = _targets_x(t)
    n = max(sum(1 for c in xs if c < pcx - 1), sum(1 for c in xs if c > pcx + 1))
    room = t.w / 2 - EXIT_MARGIN - EDGE_PAD - 2
    t.step = EXIT_STEP if n <= 1 else min(EXIT_STEP, room / (n - 1))
    if t.step < MIN_STEP:
        t.step = MIN_STEP
        t.w = 2 * (EXIT_MARGIN + (n - 1) * MIN_STEP + EDGE_PAD + 2)
        t.x = pcx - t.w / 2


def layout(graph, cfg, top=0, left=0):
    """The finished layout, with lane and grid column counts chosen to waste the least space."""
    return _layout(graph, cfg, top, left, _compact(graph, cfg) if cfg["layout"].get("compact", True) else None)


# ---------------------------------------------------------------- compacting
_COMPACT_CACHE = {}  # the network's structure rarely changes: reuse the last choice for it


def _area(graph, cfg, opts, children=None):
    """The map's area, measured without drawing it (the search compares hundreds of these)."""
    size = _layout(graph, cfg, 0, 0, opts, children, size_only=True)
    if size is None:
        return float("inf")  # never choose an arrangement that couldn't be made to fit
    return size[0] * size[1]


def _knobs(root):
    """Every lane (its item columns) and every container grid (its lane columns) in the tree."""
    out = []

    def lane(l):
        out.append(("lane_cols", l.key, list(range(1, min(len(l.nodes), 6) + 1))))

    def walk(t):
        items = [x for it in t.items for x in (it.members if isinstance(it, _Col) else [it])]
        movable = [it for it in items if not (isinstance(it, _Lane) and it.side)]
        if not t.hoisted and len(movable) >= 2:
            n = len(movable)
            out.append(("stack", t.node.id, [None] + [(i, j, r) for i in range(n) for j in range(i + 1, min(n, i + 3))
                                                       for r in (False, True)]))
        for it in items + ([t.vpn] if t.vpn else []) + ([t.hoisted] if t.hoisted else []):
            walk(it) if isinstance(it, _Tree) else lane(it)
        if t.grid:
            n = t.grid.lane_count()
            out.append(("grid_cols", t.node.id, [c for c in range(1, min(n, 7) + 1, 2)]))
            for col in t.grid.cols:
                for l in col:
                    lane(l)
    walk(root)
    return out


def _compact(graph, cfg):
    """Try other column counts for each lane and grid, and stacking neighbouring children in a column,
    one at a time, keeping any that make the whole map smaller, until nothing helps."""
    signature = (cfg["layout"]["lane_threshold"], cfg["containers"]["lane_columns"], tuple(cfg["containers"]["groups"]),
                 tuple(sorted((n.id, n.parent, n.lane, n.kind, n.docker_host) for n in graph.nodes.values())))
    if signature in _COMPACT_CACHE:
        return _COMPACT_CACHE[signature]
    opts = {"lane_cols": {}, "grid_cols": {}, "stack": {}}
    best = _area(graph, cfg, opts)
    children = _children(graph)
    knobs = _knobs(build_tree(graph, cfg, opts, children))
    for _ in range(3):
        improved = False
        for kind, key, values in knobs:
            for v in values:
                if opts[kind].get(key) == v:
                    continue
                trial = {**opts, kind: {**opts[kind], key: v}}
                a = _area(graph, cfg, trial, children)
                if a < best * 0.995:
                    best, opts, improved = a, trial, True
        if not improved:
            break
    if len(_COMPACT_CACHE) > 20:
        _COMPACT_CACHE.clear()
    _COMPACT_CACHE[signature] = opts
    return opts


def _rows(root, top):
    """Node boxes of one depth share a top line; what hangs off them starts below the band."""
    rows = {}
    _collect(root, rows)
    row_tops, bands = {}, {}
    row_top = top
    for depth in range(max(max(rows), _max_depth(root)) + 2):
        trees = rows.get(depth, [])
        row_h = max((max(t.h, t.vpn.h if t.vpn else 0) for t in trees), default=0)
        for t in trees:
            t.y = row_top
        row_tops[depth] = row_top
        band = BAND_PAD * 2
        for t in trees:
            pcx = t.x + t.w / 2
            xs = _targets_x(t, margin=60)  # count borderline side lanes as bottom ones: never too small
            side = max(sum(1 for c in xs if c < pcx - 1), sum(1 for c in xs if c > pcx + 1))
            band = max(band, BAND_PAD * 2 + side * EXIT_STEP)
        bands[depth] = (row_top + row_h, band)
        row_top += row_h + band
    return rows, row_tops, bands


def _columns(t):
    for it in t.items + ([t.hoisted] if t.hoisted else []):
        if isinstance(it, _Col):
            yield it
            for m in it.members:
                if isinstance(m, _Tree):
                    yield from _columns(m)
        elif isinstance(it, _Tree):
            yield from _columns(it)


def _bottom(it, row_tops):
    """The lowest point of anything drawn for `it`."""
    if isinstance(it, _Lane):
        return row_tops[it.depth] + it.h
    if isinstance(it, _Col):
        return max(_bottom(m, row_tops) for m in it.members)
    b = it.y + max(it.h, it.vpn.h if it.vpn else 0)
    for c in it.items + ([it.hoisted] if it.hoisted else []):
        b = max(b, _bottom(c, row_tops))
    if it.grid:
        b = max(b, row_tops[it.depth + 1] + it.grid.h)
    return b


def _layout(graph, cfg, top, left, opts, children=None, size_only=False):
    root = build_tree(graph, cfg, opts, children)
    _measure(root)
    _place_x(root, left)
    # things stacked in a column start below everything above them (tall lanes included): push each
    # one down by roughly its shortfall (an empty row is at least 60px), then check again
    settled = False
    for _ in range(500):
        rows, row_tops, bands = _rows(root, top)
        moved = False
        for col in _columns(root):
            for k in range(1, len(col.members)):
                above, below = col.members[k - 1], col.members[k]
                short = _bottom(above, row_tops) + STACK_GAP - row_tops[below.depth]
                if short > 0:
                    for m in col.members[k:]:
                        _shift_depth(m, max(1, int(short // (BAND_PAD * 2 + BOX_H))))
                    moved = True
                    break
            if moved:
                break
        if not moved:
            settled = True
            break
    if size_only:
        return (root.sub_w, _bottom(root, row_tops) - top) if settled else None
    scene = Scene()
    scene.settled = settled

    for depth in sorted(rows):
        band_top, band = bands[depth]
        for t in rows[depth]:
            _fit(t)
            nid = f"node:{t.node.id}"
            scene.boxes.append(Box(nid, t.x, t.y, t.w, t.h, "node", node_style(t.node), node=t.node))
            targets, side = [], []  # (dst, x, y where it arrives, side entry x or None)
            for it in t.items:
                if isinstance(it, _Col):
                    for j, m in enumerate(it.members):
                        if isinstance(m, _Tree):
                            dst, y, entry_y = f"node:{m.node.id}", m.y, m.y + m.h / 2
                        else:
                            y = row_tops[m.depth]
                            dst, entry_y = m.place(scene, m.x, y), y + LANE_HEAD / 2
                        if j == 0:
                            targets.append((dst, m.x + m.w / 2, y, None))
                        else:
                            targets.append((dst, it.channel(j), entry_y, m.x))
                elif isinstance(it, _Tree):
                    targets.append((f"node:{it.node.id}", it.x + it.w / 2, band_top + band, None))
                else:
                    lid = it.place(scene, it.x, band_top + band)
                    (side if _from_side(t, it) else targets).append((lid, it.x + it.w / 2, band_top + band, None))
            if t.hoisted:
                d = t.hoisted
                targets.append((f"node:{d.node.id}", d.x + d.w / 2, bands[d.depth - 1][0] + bands[d.depth - 1][1], None))
            if t.grid:
                targets += t.grid.place(scene, band_top + band)
            if t.vpn:
                lid = t.vpn.place(scene, t.vpn.x, t.y)
                y = t.y + t.h / 2
                scene.edges.append(Edge(nid, lid, [(t.x, y), (t.vpn.x + t.vpn.w, y)]))
            _fanout(scene, t, targets, band_top)
            _side_fanout(scene, t, side)
    return scene


def _fanout(scene, t, targets, band_top):
    src = f"node:{t.node.id}"
    px, pw, pb = t.x, t.w, t.y + t.h
    pcx = px + pw / 2
    straight = [tg for tg in targets if tg[3] is None and abs(tg[1] - pcx) <= 1]
    rest = [tg for tg in targets if tg not in straight]
    left = sorted((tg for tg in rest if tg[1] <= pcx), key=lambda tg: tg[1])
    right = sorted((tg for tg in rest if tg[1] > pcx), key=lambda tg: -tg[1])
    for dst, cx, y_end, _ in straight:
        scene.edges.append(Edge(src, dst, [(pcx, pb), (pcx, y_end)]))
    for side, sign, edge_x in ((left, 1, px), (right, -1, px + pw)):
        for i, (dst, cx, y_end, entry) in enumerate(side):
            ex = edge_x + sign * (EXIT_MARGIN + i * t.step)
            lvl = band_top + BAND_PAD + i * EXIT_STEP
            pts = [(ex, pb), (ex, lvl), (cx, lvl), (cx, y_end)]
            if entry is not None:
                pts.append((entry, y_end))
            scene.edges.append(Edge(src, dst, pts))


def _side_fanout(scene, t, targets):
    """Lanes joined from the node's right side: out sideways, across, then down into the lane's top.
    The outermost lane leaves highest, so the lines nest."""
    src = f"node:{t.node.id}"
    for i, (dst, cx, y_end, _) in enumerate(sorted(targets, key=lambda tg: -tg[1])):
        y = t.y + SIDE_EXIT_TOP + i * EXIT_STEP
        scene.edges.append(Edge(src, dst, [(t.x + t.w, y), (cx, y), (cx, y_end)]))


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
