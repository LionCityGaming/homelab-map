"""Turn a laid-out Scene into draw.io XML, in a light and a dark version with fixed palettes.

Every colour is written as light-dark(light, dark) and resolved per file, so each file looks the
same in any viewer theme. Edges carry explicit exit/entry points and bend points, so draw.io draws
exactly the routed path and never re-routes it.
"""
import html
import re
import xml.etree.ElementTree as ET

from .layout import Box, layout, check, LayoutError, text_w
from .palette import resolve, ld

INTER = "fontFamily=Inter;fontSource=https%3A%2F%2Ffonts.googleapis.com%2Fcss%3Ffamily%3DInter;"
# The IPs use JetBrains Mono inside the label HTML; draw.io only downloads fonts named by a cell's
# fontFamily, so the (invisible) backing sheet names it to get it loaded.
MONO = "fontFamily=JetBrains Mono;fontSource=https%3A%2F%2Ffonts.googleapis.com%2Fcss%3Ffamily%3DJetBrains%2BMono;"
UPDATED = "{{UPDATED}}"
HEADER_H = 190
LEGEND = [("phys", "Physical Devices"), ("lxc", "LXC Containers"), ("vm", "Virtual Machines"),
          ("docker", "Docker Containers"), ("stopped", "Stopped")]


def make_styles(c):
    def box(key):
        return f"fillColor={ld(c[key]['fill'])};strokeColor={ld(c[key]['border'])};"
    base = (f"rounded=1;whiteSpace=wrap;html=1;fontStyle=0;fontColor={ld(c['text'])};arcSize=20;strokeWidth=2;"
            f"align=center;verticalAlign=middle;fontSize=16;" + INTER)
    lane = (f"swimlane;startSize=45;fontColor={ld(c['text'])};fontSize=16;fontStyle=1;rounded=1;arcSize=12;"
            f"align=center;verticalAlign=middle;strokeWidth=2;html=1;" + INTER)
    return {
        "internet": "ellipse;shape=cloud;whiteSpace=wrap;html=1;strokeWidth=2;fontSize=18;"
                    f"fontColor={ld(c['text'])};" + box("internet") + INTER,
        "phys": base + box("physical"),
        "lxc": base + box("lxc"),
        "vm": base + box("vm"),
        "docker": base + box("container"),
        "stopped": base + "dashed=1;" + box("stopped") + f"fontColor={ld(c['stopped_text'])};",
        "lane_phys": lane + box("physical"),
        "lane_lxc": lane + box("lxc"),
        "lane_vm": lane + box("vm"),
        "lane_docker": lane + box("container"),
        "wrapper": lane + "dashed=1;" + box("containers_box"),
        "legend": INTER + f"text;whiteSpace=wrap;html=1;fontSize=16;fontColor={ld(c['text'])};"
                          "labelBackgroundColor=none;align=left;verticalAlign=middle;",
        "sheet": f"rounded=0;whiteSpace=wrap;html=1;fillColor={ld(c['background'])};strokeColor=none;movable=0;"
                 "resizable=0;selectable=0;deletable=0;" + MONO,
        "edge": f"endArrow=none;rounded=1;shape=link;html=1;strokeColor={ld(c['lines'])};",
    }


def label(node, subtext):
    return (f"<b>{'🌐 ' if node.public else ''}{html.escape(node.name)}</b><br>"
            f'<span style="font-family:JetBrains Mono,monospace;font-size:13px;color:{subtext};">'
            f"{html.escape(node.sub())}</span>")


def header(scene, title, used_styles, public, subtext):
    """Title, "Last updated" line and a legend of only what's on the map, above the tree."""
    scene.boxes.append(Box("title", 0, 0, 900, 45, "legend", "legend", title=f"<b>{html.escape(title)}</b>"))
    scene.boxes.append(Box("updated", 0, 48, 500, 28, "legend", "legend",
                           title=f'<span style="color:{subtext};">Last updated: {UPDATED}</span>'))
    x = 0
    entries = [(s, t) for s, t in LEGEND if s in used_styles]
    if public:
        entries.append(("public", "Public via reverse proxy"))
    for style, text in entries:
        if style == "public":
            scene.boxes.append(Box("legend:public-icon", x, 104, 36, 30, "legend", "legend", title="🌐"))
        else:
            scene.boxes.append(Box(f"legend:{style}", x, 107, 36, 24, "swatch", style))
        w = round(text_w(text, 9.5)) + 20
        scene.boxes.append(Box(f"legend:{style}:text", x + 46, 104, w, 30, "legend", "legend", title=text))
        x += 46 + w + 30


def build(graph, cfg):
    """Graph -> (light_xml, dark_xml, scene). Raises LayoutError if the layout breaks the rules."""
    colors = resolve(cfg)
    scene = layout(graph, cfg, top=HEADER_H)
    used = {b.style for b in scene.boxes if b.role in ("node", "item")}
    header(scene, cfg["title"], used, any(n.public for n in graph.nodes.values()), ld(colors["subtext"]))
    problems = check(scene)
    if problems:
        raise LayoutError("; ".join(problems[:10]))
    xml = to_xml(scene, make_styles(colors), colors)
    return _variant(xml, dark=False), _variant(xml, dark=True), scene


def to_xml(scene, styles, colors):
    model = ET.Element("mxGraphModel", dict(dx="1600", dy="1000", grid="1", gridSize="10", guides="1", tooltips="1",
                                            connect="1", arrows="1", fold="1", page="0", pageScale="1",
                                            math="0", shadow="0", background=ld(colors["background"])))
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    top = scene.top()
    x0 = min(b.x for b in top) - MARGIN
    y0 = min(b.y for b in top) - MARGIN
    x1 = max(b.x + b.w for b in top) + MARGIN
    y1 = max(b.y + b.h for b in top) + MARGIN
    _cell(root, "sheet", "", styles["sheet"], x0, y0, x1 - x0, y1 - y0, "1")
    ids = {b.id: f"c{i}" for i, b in enumerate(scene.boxes)}
    subtext = ld(colors["subtext"])
    for b in scene.boxes:
        if b.role in ("node", "item"):
            value, style = label(b.node, subtext), styles[b.style] + ("fontSize=18;" if b.role == "node" else "")
        elif b.role in ("lane", "wrapper"):
            value, style = html.escape(b.title), styles[b.style]
        elif b.role == "swatch":
            value, style = "", styles[b.style] + "arcSize=30;"
        else:
            value = b.title
            style = styles["legend"] + ("fontSize=30;" if b.id == "title" else "")
            if b.title == "🌐":
                style += "align=center;fontSize=18;"
        _cell(root, ids[b.id], value, style, b.x, b.y, b.w, b.h, ids[b.parent] if b.parent else "1",
              link=b.node.url if b.node is not None and b.node.url else "")
    geo = {b.id: b for b in top}
    for i, e in enumerate(scene.edges):
        s, t = geo[e.src], geo[e.dst]
        (sx, sy), (tx, ty) = e.points[0], e.points[-1]
        style = (styles["edge"] + f"exitX={(sx - s.x) / s.w:.4f};exitY={(sy - s.y) / s.h:.4f};exitDx=0;exitDy=0;"
                 f"entryX={(tx - t.x) / t.w:.4f};entryY={(ty - t.y) / t.h:.4f};entryDx=0;entryDy=0;")
        c = ET.SubElement(root, "mxCell", id=f"e{i}", value="", style=style, edge="1", parent="1",
                          source=ids[e.src], target=ids[e.dst])
        g = ET.SubElement(c, "mxGeometry", relative="1")
        g.set("as", "geometry")
        if len(e.points) > 2:
            arr = ET.SubElement(g, "Array")
            arr.set("as", "points")
            for px, py in e.points[1:-1]:
                ET.SubElement(arr, "mxPoint", x=f"{px:g}", y=f"{py:g}")
    return ET.tostring(model, encoding="unicode")


MARGIN = 80


def links(scene):
    """Where each box with a URL ends up in the PNG (which starts at the backing sheet's corner, scale 1),
    so the web viewer can make them clickable."""
    top = scene.top()
    x0, y0 = min(b.x for b in top) - MARGIN, min(b.y for b in top) - MARGIN
    x1, y1 = max(b.x + b.w for b in top) + MARGIN, max(b.y + b.h for b in top) + MARGIN
    boxes = {b.id: b for b in scene.boxes}

    def absolute(b):
        x, y = b.x, b.y
        while b.parent:
            b = boxes[b.parent]
            x, y = x + b.x, y + b.y
        return x, y

    areas = []
    for b in scene.boxes:
        if b.node is not None and b.node.url:
            x, y = absolute(b)
            areas.append({"x": round(x - x0), "y": round(y - y0), "w": round(b.w), "h": round(b.h),
                          "url": b.node.url, "name": b.node.name})
    return {"width": round(x1 - x0), "height": round(y1 - y0), "areas": areas}


def _cell(root, cid, value, style, x, y, w, h, parent, link=""):
    """A box. With a link it's wrapped in a UserObject, so clicking it (in draw.io or the SVG) opens the URL."""
    if link:
        obj = ET.SubElement(root, "UserObject", id=cid, label=value, link=link)
        c = ET.SubElement(obj, "mxCell", style=style, vertex="1", parent=parent)
    else:
        c = ET.SubElement(root, "mxCell", id=cid, value=value, style=style, vertex="1", parent=parent)
    ET.SubElement(c, "mxGeometry", x=f"{x:g}", y=f"{y:g}", width=f"{w:g}", height=f"{h:g}").set("as", "geometry")


def _variant(xml, dark):
    model = re.sub(r"light-dark\((#[0-9A-Fa-f]+),(#[0-9A-Fa-f]+)\)", r"\2" if dark else r"\1", xml)
    return (f'<mxfile host="homelab-map"><diagram name="Network Map" id="network-map">'
            f'{model}</diagram></mxfile>')
