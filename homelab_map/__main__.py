"""homelab-map command line.

  python -m homelab_map run      keep the map up to date (checks every `interval` seconds)
  python -m homelab_map once     one check, publish if changed, then exit
  python -m homelab_map check    show what was found and any warnings; publishes nothing
"""
import argparse
import os
import sys

from . import __version__
from .config import ConfigError, load
from .layout import LayoutError
from .palette import PaletteError


def print_tree(graph):
    kids = {}
    for n in graph.nodes.values():
        kids.setdefault(n.parent, []).append(n)

    def walk(node, depth):
        bits = [node.kind]
        if node.ip and node.kind != "container":
            bits.append(node.ip)
        if node.kind == "container":
            bits.append(node.port)
        if node.state != "running":
            bits.append("STOPPED")
        if node.public:
            bits.append("public")
        if node.lane:
            bits.append(f"in lane '{node.lane.split('  ', 1)[-1]}'")
        print(f"{'  ' * depth}- {node.name}  ({', '.join(b for b in bits if b)})")
        for k in sorted(kids.get(node.id, []), key=lambda n: (n.kind == "container", n.lane, n.name.lower())):
            walk(k, depth + 1)

    walk(graph.nodes["internet"], 0)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="homelab-map", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "once", "check"])
    parser.add_argument("--config", default=os.environ.get("HOMELAB_MAP_CONFIG", "/config/config.yaml"))
    parser.add_argument("--data", help="data directory (default: data_dir in config, /data)")
    parser.add_argument("--force", action="store_true", help="publish even if nothing changed (once)")
    parser.add_argument("--version", action="version", version=f"homelab-map {__version__}")
    args = parser.parse_args(argv)
    try:
        cfg = load(args.config)
    except ConfigError as e:
        print(f"config problem: {e}", file=sys.stderr)
        return 2
    if args.data:
        cfg["data_dir"] = args.data
    try:
        os.makedirs(os.path.join(cfg["data_dir"], "output"), exist_ok=True)
        probe = os.path.join(cfg["data_dir"], ".write-test")
        open(probe, "w").close()
        os.remove(probe)
    except OSError:
        print(f"can't write to {cfg['data_dir']}. If Docker created ./data as root, fix it with:\n"
              f"  sudo chown -R {os.getuid() if hasattr(os, 'getuid') else 1000}:"
              f"{os.getgid() if hasattr(os, 'getgid') else 1000} data", file=sys.stderr)
        return 2

    from . import drawio, runner
    from .collect import collect
    if args.command == "check":
        graph = collect(cfg)
        print_tree(graph)
        print()
        try:
            _, _, scene = drawio.build(graph, cfg)
            print(f"layout OK: {len(scene.edges)} lines, no overlaps")
        except (LayoutError, PaletteError) as e:
            print(f"layout problem: {e}")
            return 1
        for w in graph.warnings:
            print(w)
        if graph.guesses:
            print("\nContainers grouped by best guess. To keep (or fix) these, paste into config.yaml:\n")
            print("containers:\n  groups:")
            groups = {}
            for title, cname in graph.guesses:
                groups.setdefault(title, []).append(cname)
            for title, names in sorted(groups.items(), key=lambda kv: kv[0].split("  ", 1)[-1]):
                print(f'    "{title}": [{", ".join(sorted(names))}]')
        return 0
    if args.command == "once":
        state = runner.load_state(cfg["data_dir"])
        try:
            runner.run_once(cfg, state, force=args.force)
        finally:
            runner.save_state(cfg["data_dir"], state)
        return 0
    runner.log(f"homelab-map {__version__} starting, checking every {cfg['interval']}s")
    runner.run_forever(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
