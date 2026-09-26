"""Colours. Pick a preset with `palette:` and override any colour under `colors:` in config.yaml.

Every colour is a [light, dark] pair: the light file uses the first, the dark file the second.
Box types take fill and border pairs; the rest are single pairs.
"""
import copy
import re

PRESETS = {
    "default": {
        "physical": {"fill": ["#DAE8FC", "#1A5F7A"], "border": ["#6C8EBF", "#6BB8D4"]},
        "lxc": {"fill": ["#D5E8D4", "#1F4D38"], "border": ["#82B366", "#5FAF84"]},
        "vm": {"fill": ["#E1D5E7", "#3D2E5C"], "border": ["#9673A6", "#A991CC"]},
        "container": {"fill": ["#FFE6CC", "#7A4A1E"], "border": ["#D79B00", "#E8A96A"]},
        "stopped": {"fill": ["#EEEEEE", "#2A2B30"], "border": ["#9E9E9E", "#6B7280"]},
        "internet": {"fill": ["#F5F5F5", "#2D2F36"], "border": ["#666666", "#9CA3AF"]},
        "containers_box": {"fill": ["#FFF8F0", "#26221E"], "border": ["#D79B00", "#E8A96A"]},
        "background": ["#FFFFFF", "#1E1F24"],
        "text": ["#1A1A2E", "#F1F5F9"],
        "subtext": ["#4A5568", "#CBD5E0"],        # IPs, ports, "Last updated"
        "stopped_text": ["#8A8A8A", "#9CA3AF"],
        "lines": ["#1A1A2E", "#CBD5E0"],
    },
    "vivid": {
        "physical": {"fill": ["#BFDBFE", "#1E3A8A"], "border": ["#2563EB", "#60A5FA"]},
        "lxc": {"fill": ["#BBF7D0", "#14532D"], "border": ["#16A34A", "#4ADE80"]},
        "vm": {"fill": ["#E9D5FF", "#4C1D95"], "border": ["#9333EA", "#C084FC"]},
        "container": {"fill": ["#FED7AA", "#7C2D12"], "border": ["#EA580C", "#FB923C"]},
        "containers_box": {"fill": ["#FFF7ED", "#1C1410"], "border": ["#EA580C", "#FB923C"]},
    },
    "pastel": {
        "physical": {"fill": ["#E8F1FB", "#2C3E50"], "border": ["#A7C4E5", "#7FA7D1"]},
        "lxc": {"fill": ["#EAF6EA", "#2E4A3A"], "border": ["#A9D4A9", "#86B89A"]},
        "vm": {"fill": ["#F1EAF6", "#43395A"], "border": ["#C6B3D8", "#A796C4"]},
        "container": {"fill": ["#FDF1E4", "#5A4232"], "border": ["#EBC79E", "#C9A07A"]},
        "containers_box": {"fill": ["#FFFBF6", "#2A2622"], "border": ["#EBC79E", "#C9A07A"]},
    },
    "mono": {
        "physical": {"fill": ["#F3F4F6", "#2A2B30"], "border": ["#4B5563", "#D1D5DB"]},
        "lxc": {"fill": ["#E5E7EB", "#33343A"], "border": ["#374151", "#E5E7EB"]},
        "vm": {"fill": ["#D1D5DB", "#3F4046"], "border": ["#1F2937", "#F3F4F6"]},
        "container": {"fill": ["#FFFFFF", "#222328"], "border": ["#6B7280", "#9CA3AF"]},
        "containers_box": {"fill": ["#FAFAFA", "#1B1C20"], "border": ["#6B7280", "#9CA3AF"]},
    },
}
HEX = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


class PaletteError(Exception):
    pass


def resolve(cfg):
    """The preset named by `palette`, on top of the default one, with `colors` overrides on top."""
    name = cfg.get("palette", "default")
    if name not in PRESETS:
        raise PaletteError(f"palette '{name}' doesn't exist; choose one of: {', '.join(PRESETS)}")
    colors = copy.deepcopy(PRESETS["default"])
    for layer in (PRESETS[name], cfg.get("colors") or {}):
        for key, value in layer.items():
            if key not in colors:
                raise PaletteError(f"colors.{key} isn't a colour setting; use one of: {', '.join(colors)}")
            if isinstance(colors[key], dict):
                if not isinstance(value, dict):
                    raise PaletteError(f"colors.{key} needs fill: and border:")
                colors[key].update({k: _pair(f"colors.{key}.{k}", v) for k, v in value.items()})
            else:
                colors[key] = _pair(f"colors.{key}", value)
    return colors


def _pair(where, value):
    """A colour for both versions, or [light, dark]."""
    pair = [value, value] if isinstance(value, str) else list(value)
    if len(pair) != 2 or not all(isinstance(c, str) and HEX.match(c) for c in pair):
        raise PaletteError(f"{where} should be a colour like \"#DAE8FC\" or a pair [\"#light\", \"#dark\"]")
    return pair


def ld(pair):
    """As written into the XML; each output file keeps one side."""
    return f"light-dark({pair[0]},{pair[1]})"
