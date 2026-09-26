"""Load config.yaml, fill in defaults, and substitute ${VARS} from the environment.

Secrets never go in config.yaml itself: write `password: ${UNIFI_PASS}` and put the value in
.env (docker compose passes it in as an environment variable).
"""
import copy
import os
import re

import yaml

DEFAULTS = {
    "title": "Homelab Network Map",
    "interval": 60,                      # seconds between checks; 0 = run once and exit
    "date_format": "%Y-%m-%d %H:%M",     # for the "Last updated" line
    "theme": "dark",                     # which version is rendered to PNG/SVG and published
    "palette": "default",                # default | vivid | pastel | mono
    "colors": {},                        # override any colour; see palette.py
    "internet_label": "Internet",
    "data_dir": "/data",
    "sources": {
        "unifi": {
            "enabled": False,
            "url": "",
            "username": "",
            "password": "",
            "site": "default",
            "verify_tls": False,
            "clients": "fixed_ip",       # fixed_ip | all | none
            "lane_networks": "auto",     # "auto" = every network except the gateway's own, or a list of names
            "wireguard": True,
            "guess_guests": True,        # VM MACs sharing a switch port with a physical host hang off that host
        },
        "proxmox": {
            "enabled": False,
            "url": "",
            "token_id": "",              # user@realm!tokenname
            "token_secret": "",
            "verify_tls": False,
            "include_stopped": True,
        },
        "docker": {
            "hosts": [],                 # [{name?, ip, endpoint}]
        },
        "caddy": {
            "enabled": False,
            "caddyfile": "",             # path to a mounted Caddyfile
            "ssh": "",                   # or user@host to read it over SSH
            "ssh_key": "",
            "path": "/etc/caddy/Caddyfile",
            "public_snippets": ["public"],
            "public_hosts": [],          # regexes matched against the site address
        },
    },
    "devices": [],
    "overrides": {"rename": {}, "hide": [], "parent": {}, "kind": {}},
    "containers": {
        "groups": {},                    # "Group title": [container names]
        "names": {},                     # container name -> display name
        "ports": {},                     # container name -> port to show (host networking)
        "hide": [],
        "auto_group": True,              # guess a group for unlisted containers from their description
        "lookup_online": True,           # allow GitHub / Docker Hub lookups for those guesses
        "keywords": {},                  # extra or replacement keyword lists per group
        "lane_columns": 3,               # columns of container lanes under a Docker host (made odd)
    },
    "layout": {
        "lane_threshold": 6,             # more leaf devices than this under one parent become a lane
        "compact": True,                 # stack short branches and pick lane columns to waste the least space
    },
    "outputs": {
        "files": {"enabled": True, "formats": ["drawio", "png", "pdf"]},
        "bookstack": {"enabled": False, "url": "", "token_id": "", "token_secret": "", "page_id": 0},
        "web": {"enabled": True, "port": 8080},
    },
    "render": {"export_url": "http://drawio-export:8000"},
    "notify": {
        "gotify": {"url": "", "token": ""},
        "ntfy": {"url": "", "token": ""},
        "webhook": {"url": ""},
    },
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECRET_KEYS = {"password", "token", "token_secret", "token_id", "secret", "pass"}
_SECRETS = set()


def redact(text):
    """Mask every credential from the config in text that's about to be shown, logged or sent.
    Error messages from libraries sometimes quote what they were given (a header, a URL), and a
    credential must never reach a warning, alert, log or the web viewer that way."""
    text = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _remember_secrets(value, key=""):
    if isinstance(value, dict):
        for k, v in value.items():
            _remember_secrets(v, k)
    elif isinstance(value, list):
        for v in value:
            _remember_secrets(v, key)
    elif isinstance(value, str) and key in SECRET_KEYS and len(value) >= 4:  # never empty: that would mask everything
        _SECRETS.add(value)


class ConfigError(Exception):
    pass


def _expand(value):
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in os.environ:
                raise ConfigError(f"config refers to ${{{m.group(1)}}} but it isn't set (add it to .env)")
            return os.environ[m.group(1)].strip("\r\n")  # .env files saved on Windows end lines with \r
        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path):
    if not os.path.exists(path):
        raise ConfigError(f"no config at {path} (copy config.example.yaml to get started)")
    with open(path, encoding="utf8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} should be a YAML mapping")
    cfg = _merge(DEFAULTS, _expand(raw))
    _remember_secrets(cfg)
    # a source is enabled just by being configured, unless it says enabled: false
    for name in ("unifi", "proxmox", "caddy"):
        src = raw.get("sources", {}).get(name)
        if isinstance(src, dict) and "enabled" not in src:
            cfg["sources"][name]["enabled"] = True
    for key in ("bookstack",):
        out = raw.get("outputs", {}).get(key)
        if isinstance(out, dict) and "enabled" not in out:
            cfg["outputs"][key]["enabled"] = True
    return cfg
