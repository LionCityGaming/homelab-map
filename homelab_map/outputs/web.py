"""A small built-in viewer: the latest map (services with a URL are clickable), downloads, warnings.

It has no login. Keep the port on your LAN, or put it behind your reverse proxy and SSO.
"""
import functools
import http.server
import threading

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Network Map</title>
<style>
  :root { color-scheme: light dark; --bg:#f4f5f7; --fg:#1a1a2e; --muted:#5b6475; --card:#fff; --line:#d9dce3; --warn:#b45309; }
  @media (prefers-color-scheme: dark) { :root { --bg:#16171b; --fg:#f1f5f9; --muted:#9aa3b2; --card:#1e1f24; --line:#30323a; --warn:#f59e0b; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  header { display:flex; flex-wrap:wrap; gap:8px 20px; align-items:center; padding:14px 16px; }
  h1 { font-size:19px; margin:0; }
  .muted { color:var(--muted); }
  nav { display:flex; gap:12px; }
  nav a { color:inherit; }
  main { padding:0 16px 24px; }
  .map { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:auto; }
  .stage { position:relative; width:max-content; }
  .fit .stage { width:100%; }
  .stage img { display:block; }
  .fit .stage img { width:100%; height:auto; }
  .stage a { position:absolute; border-radius:10px; }
  .stage a:hover, .stage a:focus-visible { outline:3px solid #3b82f6; outline-offset:2px; }
  #warnings { margin:16px 0 0; padding-left:20px; }
  #warnings li { color:var(--warn); }
  button { font:inherit; padding:4px 12px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:inherit; cursor:pointer; }
</style></head>
<body>
<header>
  <h1>Network Map</h1>
  <span class="muted">Last updated: <span id="updated">…</span></span>
  <nav><a href="map.png" download>PNG</a><a href="map.pdf" download>PDF</a><a href="map.drawio" download>draw.io</a></nav>
  <button id="fit" type="button">Actual size</button>
</header>
<main>
  <div class="map fit" id="box"><div class="stage" id="stage"><img id="map" src="map.png" alt="Network map"></div></div>
  <ul id="warnings"></ul>
</main>
<script>
let last = null;
const box = document.getElementById('box'), fit = document.getElementById('fit'), stage = document.getElementById('stage');
fit.onclick = () => { box.classList.toggle('fit'); fit.textContent = box.classList.contains('fit') ? 'Actual size' : 'Fit to width'; };
async function links() {
  try {
    const l = await (await fetch('links.json', {cache: 'no-store'})).json();
    stage.querySelectorAll('a').forEach(a => a.remove());
    for (const a of l.areas) {
      const el = document.createElement('a');
      el.href = a.url; el.target = '_blank'; el.rel = 'noopener'; el.title = a.name + ' — ' + a.url;
      el.setAttribute('aria-label', a.name);
      Object.assign(el.style, {left: a.x / l.width * 100 + '%', top: a.y / l.height * 100 + '%',
                               width: a.w / l.width * 100 + '%', height: a.h / l.height * 100 + '%'});
      stage.append(el);
    }
  } catch (e) {}
}
async function poll() {
  try {
    const s = await (await fetch('status.json', {cache: 'no-store'})).json();
    document.getElementById('updated').textContent = s.updated || 'never';
    const ul = document.getElementById('warnings'); ul.replaceChildren();
    for (const w of s.warnings || []) { const li = document.createElement('li'); li.textContent = w; ul.append(li); }
    if (last !== null && s.updated !== last) { document.getElementById('map').src = 'map.png?' + Date.now(); links(); }
    last = s.updated;
  } catch (e) {}
}
links(); poll(); setInterval(poll, 15000);
</script>
</body></html>
"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def start(directory, port):
    with open(f"{directory}/index.html", "w", encoding="utf8") as f:
        f.write(PAGE)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), functools.partial(_Quiet, directory=directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
