"""One check: collect, lay out, and if anything really changed, render and publish.

"Really changed" ignores the "Last updated" time, so nothing is re-rendered or re-published unless
the network did change; the time shown is when it last did. An output that fails is retried on the
next run. Alerts are sent once when a problem appears (and again only if it goes away and returns).
"""
from concurrent.futures import ThreadPoolExecutor
import datetime
import hashlib
import json
import os
import time

from . import drawio, layout, notify, render
from .collect import collect
from .config import redact
from .layout import LayoutError
from .outputs.bookstack import BookStack


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {redact(msg)}", flush=True)


def load_state(data_dir):
    path = os.path.join(data_dir, "state.json")
    if os.path.exists(path):
        with open(path, encoding="utf8") as f:
            return json.load(f)
    return {}


def save_state(data_dir, state):
    with open(os.path.join(data_dir, "state.json"), "w", encoding="utf8") as f:
        json.dump(state, f, indent=2)


def alert(cfg, state, key, title, message):
    message = redact(message)
    alerts = state.setdefault("alerts", {})
    if alerts.get(key) != message:
        notify.send(cfg["notify"], title, message, log)
        alerts[key] = message


def clear(state, key):
    state.setdefault("alerts", {}).pop(key, None)


def run_once(cfg, state, force=False):
    data_dir = cfg["data_dir"]
    out_dir = os.path.join(data_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    graph = collect(cfg)

    # Warnings: alert only the ones that are new since the last run
    seen = set(state.get("warnings", []))
    fresh = [w for w in graph.warnings if w not in seen]
    if fresh:
        notify.send(cfg["notify"], "homelab-map", "\n".join(fresh)[-1500:], log)
    state["warnings"] = graph.warnings

    layout.set_previous(state.get("layout_opts"))  # keep the map's arrangement stable across restarts
    try:
        light, dark, scene = drawio.build(graph, cfg)
        state["layout_opts"] = layout.get_previous()
        clear(state, "layout")
    except LayoutError as e:
        alert(cfg, state, "layout", "homelab-map: layout problem, map not updated", str(e))
        return
    chosen = dark if cfg["theme"] == "dark" else light
    digest = hashlib.sha256(chosen.encode()).hexdigest()
    if force:
        state.pop("published", None)  # every output again, even ones already up to date
    if digest != state.get("digest") or force:
        state["digest"] = digest
        state["updated"] = datetime.datetime.now().strftime(cfg["date_format"])
        log(f"map changed ({len(graph.nodes) - 1} devices)")
    stamp = state.get("updated", "")
    light, dark, chosen = (x.replace(drawio.UPDATED, stamp) for x in (light, dark, chosen))

    outputs = []
    files = cfg["outputs"]["files"]
    if files["enabled"] or cfg["outputs"]["web"]["enabled"]:
        outputs.append("files")
    if cfg["outputs"]["bookstack"]["enabled"]:
        outputs.append("bookstack")
    pending = [o for o in outputs if state.get("published", {}).get(o) != state["digest"]]
    if not pending:
        _status(out_dir, state)
        return

    # every picture needed by any pending output, drawn at the same time (the renderer takes several)
    needed = set()
    if "files" in pending:
        needed |= {"png", "pdf"} & (set(files["formats"]) | ({"png"} if cfg["outputs"]["web"]["enabled"] else set()))
    if "bookstack" in pending:
        needed.add("png")
    rendered, render_error = {}, None
    if needed:
        url = cfg["render"]["export_url"]
        try:
            render.wait_ready(url)
            with ThreadPoolExecutor(max_workers=len(needed)) as pool:
                jobs = {fmt: pool.submit(render.png if fmt == "png" else render.pdf, url, chosen) for fmt in needed}
                rendered = {fmt: job.result() for fmt, job in jobs.items()}
        except Exception as e:  # reported by each output that needed a picture
            render_error = e

    def get(fmt):
        if fmt not in rendered:
            raise render_error or RuntimeError(f"no {fmt} was rendered")
        return rendered[fmt]

    for out in pending:
        try:
            if out == "files":
                formats = set(files["formats"]) | ({"png", "drawio"} if cfg["outputs"]["web"]["enabled"] else set())
                if "drawio" in formats:
                    _write(out_dir, "map.drawio", chosen.encode())
                    _write(out_dir, "map-light.drawio", light.encode())
                    _write(out_dir, "map-dark.drawio", dark.encode())
                for fmt in ("png", "pdf"):
                    if fmt in formats:
                        _write(out_dir, f"map.{fmt}", get(fmt))
                _write(out_dir, "links.json", json.dumps(drawio.links(scene)).encode())
                result = f"wrote {', '.join(sorted(formats))} to {out_dir}"
            else:
                result = BookStack(cfg["outputs"]["bookstack"], cfg["title"]).publish(get("png"), state)
            state.setdefault("published", {})[out] = state["digest"]
            clear(state, f"output:{out}")
            log(f"{out}: {result}")
        except Exception as e:
            alert(cfg, state, f"output:{out}", f"homelab-map: {out} failed (will retry)", str(e)[:500])
    _status(out_dir, state)


def _write(out_dir, name, data):
    tmp = os.path.join(out_dir, f".{name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(out_dir, name))  # never serve a half-written file


def _status(out_dir, state):
    _write(out_dir, "status.json", json.dumps({"updated": state.get("updated", ""),
                                               "warnings": state.get("warnings", [])}, indent=1).encode())


def run_forever(cfg):
    data_dir = cfg["data_dir"]
    os.makedirs(os.path.join(data_dir, "output"), exist_ok=True)
    if cfg["outputs"]["web"]["enabled"]:
        from .outputs import web
        web.start(os.path.join(data_dir, "output"), int(cfg["outputs"]["web"]["port"]))
        log(f"web viewer on port {cfg['outputs']['web']['port']}")
    while True:
        started = time.time()
        state = load_state(data_dir)
        try:
            run_once(cfg, state)
            clear(state, "crash")
        except Exception as e:  # keep going; a source or output being down shouldn't stop the loop
            log(f"run failed: {e!r}")
            alert(cfg, state, "crash", "homelab-map: run failed", repr(e)[:500])
        save_state(data_dir, state)
        if not cfg["interval"]:
            return
        time.sleep(max(5, cfg["interval"] - (time.time() - started)))
