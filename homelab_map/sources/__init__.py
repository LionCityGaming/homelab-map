"""Data sources. Each one fetches raw data (cached, so a source that's briefly down doesn't blank
the map) and then adds or updates nodes in the graph."""
import http.cookiejar
import json
import os
import ssl
import urllib.error
import urllib.request


class SourceError(Exception):
    pass


def opener(verify_tls=True, cookies=False):
    handlers = []
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if cookies:
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return urllib.request.build_opener(*handlers)


def request_json(op, url, data=None, headers=None, method=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"Accept": "application/json", **({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    req = urllib.request.Request(url, body, hdrs, method=method)
    with op.open(req, timeout=timeout) as resp:
        return json.load(resp), resp.headers


def cached(data_dir, name, fetch, graph):
    """Run fetch(); save the result. If it fails, fall back to the last good copy and warn."""
    path = os.path.join(data_dir, "cache", f"{name}.json")
    try:
        data = fetch()
    except Exception as e:  # any failure: network, auth, bad data
        reason = e.reason if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError) else e
        if os.path.exists(path):
            graph.warn(f"WARNING: {name}: using cached copy ({reason})")
            with open(path, encoding="utf8") as f:
                return json.load(f)
        graph.warn(f"WARNING: {name}: unavailable and nothing cached yet ({reason})")
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as f:
        json.dump(data, f, indent=1)
    return data
