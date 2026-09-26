"""Which lane (group) each container is drawn in.

In order: a homelab-map.group label on the container, then containers.groups in config.yaml, then
(if auto_group is on) a best guess from what the image says about itself: its own labels, its GitHub
repository's description and topics, and its Docker Hub description. Guesses produce a warning so
you can confirm them in config.yaml; anything that can't be placed goes in "Other".
"""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import time
import urllib.request

OTHER = "📦  Other"

DEFAULT_KEYWORDS = {
    "🎬  Media Automation": ["radarr", "sonarr", "lidarr", "readarr", "whisparr", "prowlarr", "jackett", "indexer",
                            "usenet", "torrent", "bittorrent", "qbittorrent", "transmission", "deluge", "nzb",
                            "sabnzbd", "nzbget", "pvr", "subtitle", "bazarr", "movie", "tv show", "media request",
                            "overseerr", "jellyseerr", "seerr", "downloader", "youtube downloader", "recyclarr",
                            "profilarr", "flaresolverr", "unpackerr", "tdarr"],
    "📺  Media Servers": ["plex", "jellyfin", "emby", "iptv", "live tv", "epg", "tautulli", "poster", "title card",
                         "streaming", "kometa", "maintainerr", "dispatcharr", "threadfin", "xteve"],
    "📚  Reading & Listening": ["book", "ebook", "comic", "manga", "audiobook", "podcast", "music", "epub", "opds",
                               "reader", "kobo", "calibre", "kavita", "komga", "navidrome", "audiobookshelf"],
    "📁  Photos & Documents": ["photo", "document", "pdf", "scan", "ocr", "paperless", "file sharing", "screenshot",
                              "gallery", "immich", "photoprism", "stirling"],
    "🧠  Knowledge & Productivity": ["wiki", "note", "bookmark", "rss", "feed", "news", "recipe", "meal", "budget",
                                    "finance", "subscription", "expense", "knowledge", "documentation", "todo",
                                    "task", "calendar", "bookstack", "linkding", "mealie"],
    "🏠  Home Automation": ["home assistant", "homeassistant", "zigbee", "zwave", "z wave", "mqtt", "mosquitto",
                           "node red", "nodered", "esphome", "smart home", "matter"],
    "🛠️  Dev & Tools": ["git", "gitea", "forgejo", "code", "ide", "developer", "vscode", "file manager",
                       "file browser", "terminal", "database admin", "registry", "mcp", "ci"],
    "📈  Monitoring": ["monitoring", "uptime", "metric", "grafana", "prometheus", "log viewer", "logs", "netdata",
                      "alert", "notification", "gotify", "ntfy", "dozzle", "healthcheck"],
    "⚙️  Infrastructure": ["reverse proxy", "authentication", "sso", "identity", "dns", "backup", "security",
                          "crowdsec", "container management", "portainer", "komodo", "vpn", "tunnel", "cloudflared",
                          "traefik", "caddy", "nginx", "authentik", "authelia", "dashboard", "startpage", "homepage",
                          "watchtower", "update"],
    "🎮  Games & Misc": ["game", "gaming", "epic", "steam", "gog", "sponsorblock", "minecraft"],
    "🖨️  3D Printing": ["3d print", "3d printer", "printer", "bambu", "octoprint", "klipper", "slicer", "filament"],
}
# Descriptions inherited from a base image say nothing about the app itself
BASE_IMAGE_BLURBS = ("Ubuntu is a Debian-based", "This image is used to start", "Unprivileged NGINX Dockerfiles",
                     "Base images for", "Alpine Linux")


def _norm(text):
    return re.sub(r"[-_/.:]+", " ", text.lower())


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-map", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def image_ref(image):
    ref = image.split("@")[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]
    return ref


def describe(image, image_labels, online):
    """What an image is: its OCI labels, its GitHub repo (description + topics), its Docker Hub page."""
    labels = image_labels(image) or {}
    texts = [labels.get("org.opencontainers.image.title", ""), labels.get("org.opencontainers.image.description", "")]
    ref = image_ref(image)
    if online:
        gh = re.search(r"github\.com/([^/]+/[^/#?\s]+)", labels.get("org.opencontainers.image.source", ""))
        repo = gh.group(1).removesuffix(".git") if gh else (
            "/".join(ref.split("/")[1:3]) if ref.startswith("ghcr.io/") else None)
        if repo:
            try:
                info = _fetch_json(f"https://api.github.com/repos/{repo}")
                texts += [info.get("description") or "", " ".join(info.get("topics") or [])]
            except Exception:
                pass
        if not ref.startswith(("ghcr.io/", "quay.io/", "gcr.io/", "registry.")):
            hub = ref.split("/", 1)[1] if ref.startswith(("lscr.io/", "docker.io/")) else ref
            try:
                texts.append(_fetch_json(f"https://hub.docker.com/v2/repositories/"
                                         f"{hub if '/' in hub else 'library/' + hub}/").get("description") or "")
            except Exception:
                pass
    return " ".join(t for t in texts if t and not any(b in t for b in BASE_IMAGE_BLURBS))


def guess(name, image, description, keywords):
    text = f" {_norm(name)} {_norm(image_ref(image))} {_norm(description)} "
    scores = {g: sum(1 for k in kws if re.search(rf"\b{re.escape(_norm(k))}s?\b", text))
              for g, kws in keywords.items()}
    top = max(scores.values(), default=0)
    best = [g for g, s in scores.items() if s == top]
    return best[0] if top and len(best) == 1 else None


def assign(graph, host, ccfg, image_labels, describe_cache):
    keywords = dict(DEFAULT_KEYWORDS)
    for title, words in (ccfg.get("keywords") or {}).items():
        keywords[title] = list(words)
    by_name = {}
    for title, names in ccfg["groups"].items():
        for n in names:
            by_name[str(n).lower()] = title
    containers = [n for n in graph.children(host.id) if n.kind == "container"]

    def pinned(node):
        return (node.extra["labels"].get("homelab-map.group") or by_name.get(node.extra["container"].lower())
                or by_name.get(node.name.lower()))

    # look up every image that needs it at the same time (each is a few web requests), not one by one;
    # each image is looked up once, and an empty result (e.g. offline) is retried after a day
    if ccfg["auto_group"]:
        todo = {}
        for node in containers:
            ref = image_ref(node.extra["image"])
            entry = describe_cache.get(ref)
            if not pinned(node) and (not entry or (not entry["text"] and time.time() - entry["at"] > 86400)):
                todo[ref] = node.extra["image"]
        if todo:
            with ThreadPoolExecutor(max_workers=8) as pool:
                texts = dict(zip(todo, pool.map(lambda image: describe(image, image_labels, ccfg["lookup_online"]),
                                                todo.values())))
            for ref, text in texts.items():
                describe_cache[ref] = {"text": text, "at": time.time()}
    guessed = []
    for node in containers:
        cname = node.extra["container"]
        title = pinned(node)
        if not title and ccfg["auto_group"]:
            entry = describe_cache.get(image_ref(node.extra["image"])) or {"text": ""}
            title = guess(cname, node.extra["image"], entry["text"], keywords)
            guessed.append(cname)
            graph.guesses.append((title or OTHER, cname))
        node.lane = title or OTHER
        if node.state != "running":
            graph.warn(f"WARNING: {node.name} ({cname}) on {host.name} is stopped")
    if guessed:
        names = ", ".join(sorted(guessed)[:8]) + (f" and {len(guessed) - 8} more" if len(guessed) > 8 else "")
        graph.warn(f"WARNING: {len(guessed)} container(s) on {host.name} were grouped by best guess ({names}). "
                   f"Run `check` to review, and pin them under containers.groups in config.yaml")
