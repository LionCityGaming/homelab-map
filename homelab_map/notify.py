"""Alerts to Gotify, ntfy and/or a generic webhook (JSON POST: {"title": ..., "message": ...})."""
import json
import urllib.request


def send(cfg, title, message, log=print):
    log(f"{title}: {message}")
    g, n, w = cfg["gotify"], cfg["ntfy"], cfg["webhook"]
    targets = []
    if g["url"] and g["token"]:
        targets.append(urllib.request.Request(
            g["url"].rstrip("/") + "/message", json.dumps({"title": title, "message": message, "priority": 6}).encode(),
            {"Content-Type": "application/json", "X-Gotify-Key": g["token"]}))
    if n["url"]:
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "world_map"}
        if n.get("token"):
            headers["Authorization"] = f"Bearer {n['token']}"
        targets.append(urllib.request.Request(n["url"], message.encode(), headers))
    if w["url"]:
        targets.append(urllib.request.Request(w["url"], json.dumps({"title": title, "message": message}).encode(),
                                              {"Content-Type": "application/json"}))
    for req in targets:
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"notification to {req.full_url.split('?')[0]} failed: {e}")
