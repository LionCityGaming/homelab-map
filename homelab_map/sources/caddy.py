"""Caddy: which services are reachable from the internet (drawn with a 🌐) and their URLs.

Reads the Caddyfile, either mounted into the container read-only (caddyfile: /caddy/Caddyfile) or
over SSH (ssh: root@192.168.1.17 with ssh_key). For SSH, use a key that can do nothing but print the
file; in the Caddy host's ~/.ssh/authorized_keys:
  command="cat /etc/caddy/Caddyfile",restrict,from="<homelab-map host IP>" ssh-ed25519 AAAA...

A site counts as public if it imports one of public_snippets (e.g. `import public`) or its address
matches one of public_hosts (regexes). Each site's reverse_proxy target is matched to a container
(host IP + published port, or container name + internal port) or to a device by IP.
"""
import re
import subprocess

from . import cached


def fetch(cfg):
    if cfg["caddyfile"]:
        with open(cfg["caddyfile"], encoding="utf8") as f:
            return {"text": f.read()}
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]
    if cfg["ssh_key"]:
        cmd += ["-i", cfg["ssh_key"]]
    text = subprocess.run(cmd + [cfg["ssh"], f"cat {cfg['path']}"], capture_output=True, text=True,
                          timeout=30, check=True).stdout
    if "reverse_proxy" not in text:
        raise RuntimeError("fetched Caddyfile has no reverse_proxy lines")
    return {"text": text}


def parse(text, public_snippets, public_hosts):
    """Site blocks -> [(address, target, public)]. target is 'host:port' as written."""
    sites, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        line = lines[i].split("#", 1)[0].rstrip() if not lines[i].lstrip().startswith("#") else ""
        head = re.match(r"^([^\s(#{][^{]*?)\s*\{\s*$", line)
        depth, body, i = line.count("{") - line.count("}"), [], i + 1
        while depth > 0 and i < len(lines):
            depth += lines[i].count("{") - lines[i].count("}")
            body.append(lines[i])
            i += 1
        if not head:
            continue
        address = head.group(1).split(",")[0].strip()
        block = "\n".join(body)
        target = re.search(r"reverse_proxy\s+(?:@\S+\s+)?(?:https?://)?([\w.\-]+:\d+)", block)
        if not target:
            continue
        imports = set(re.findall(r"import\s+(\S+)", block))
        public = bool(imports & set(public_snippets)) or any(re.search(p, address) for p in public_hosts)
        sites.append((address, target.group(1), public))
    return sites


def apply(graph, cfg, data_dir):
    raw = cached(data_dir, "caddy", lambda: fetch(cfg), graph)
    if not raw:
        return
    containers = [n for n in graph.nodes.values() if n.kind == "container"]
    hosts = {n.id: n for n in graph.nodes.values() if n.docker_host}
    for address, target, public in parse(raw["text"], cfg["public_snippets"], cfg["public_hosts"]):
        host_part, port = target.rsplit(":", 1)
        port = int(port)
        match = None
        if re.fullmatch(r"[\d.]+", host_part):
            docker_host = next((h for h in hosts.values() if h.ip == host_part), None)
            if docker_host:
                on_host = [c for c in containers if c.parent == docker_host.id]
                match = next((c for c in on_host if port in c.extra["ports"]), None)
                if match is None:  # host networking publishes no ports: try the subdomain (bambuddy.example.com)
                    sub = re.sub(r"^https?://", "", address).split(".")[0].lower()
                    match = next((c for c in on_host if not c.extra["ports"] and sub in (
                        c.extra["container"].lower(), c.name.lower().replace(" ", ""))), None)
                    if match:
                        match.port = f":{port}"
                if match is None:
                    graph.warn(f"WARNING: Caddy site {address} points at {target} but no container publishes that port")
                    continue
            else:
                match = graph.by_ip(host_part)
        else:  # Caddy on the same Docker network: reverse_proxy radarr:7878
            match = next((c for c in containers if c.extra["container"] == host_part
                          and (port in c.extra["private_ports"] or not c.extra["private_ports"])), None)
        if match is None:
            continue
        match.public = match.public or public
        match.url = match.url or ("https://" + re.sub(r"^https?://", "", address) if not address.startswith(":") else "")
        if match.kind == "container" and match.port.startswith(":") and port in match.extra["ports"]:
            match.port = f":{port}"  # show the port the site actually uses
