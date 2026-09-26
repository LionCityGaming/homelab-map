"""Put the map on a BookStack page as a real draw.io drawing (editable in BookStack's editor).

Give it a page id. The first time, if the page has no drawing yet, one is added to the end of the
page; after that the drawing's image is replaced in place, so the page itself is never edited again.

The API token needs a BookStack user whose role has "Access System API" and can edit that page.
Create one under your user's profile > API Tokens.
"""
import json
import re
from html import escape
import urllib.request
import uuid

CRLF = "\r\n"


class BookStack:
    def __init__(self, cfg, title):
        self.api = cfg["url"].rstrip("/") + "/api"
        self.page_id = int(cfg["page_id"])
        self.title = title
        self.token = f"Token {cfg['token_id']}:{cfg['token_secret']}"

    def _json(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.api + path, body, method=method,
                                     headers={"Authorization": self.token, "Content-Type": "application/json",
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def _multipart(self, path, fields, png):
        """File uploads: multipart POST. For an update BookStack needs _method=PUT as a field, because
        PHP doesn't parse multipart bodies on a real PUT (the file would silently be dropped)."""
        boundary = uuid.uuid4().hex
        parts = [f"--{boundary}{CRLF}Content-Disposition: form-data; name=\"{k}\"{CRLF}{CRLF}{v}{CRLF}"
                 for k, v in fields.items()]
        head = "".join(parts) + (f"--{boundary}{CRLF}Content-Disposition: form-data; name=\"image\"; "
                                 f"filename=\"network-map.png\"{CRLF}Content-Type: image/png{CRLF}{CRLF}")
        body = head.encode() + png + f"{CRLF}--{boundary}--{CRLF}".encode()
        req = urllib.request.Request(self.api + path, body, method="POST",
                                     headers={"Authorization": self.token, "Accept": "application/json",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)

    def publish(self, png, state):
        page = self._json("GET", f"/pages/{self.page_id}")
        found = re.search(r'drawio-diagram="(\d+)"', page.get("html") or "")
        if found:
            self._multipart(f"/image-gallery/{found.group(1)}", {"_method": "PUT"}, png)
            state["bookstack_drawing"] = int(found.group(1))
            return f"updated drawing {found.group(1)} on page {self.page_id}"
        image = self._multipart("/image-gallery", {"type": "drawio", "uploaded_to": self.page_id,
                                                   "name": "network-map.png"}, png)
        html = (page.get("html") or "") + (f'<div drawio-diagram="{int(image["id"])}" contenteditable="false">'
                                           f'<img src="{escape(image["url"])}" alt="{escape(self.title)}"></div>')
        self._json("PUT", f"/pages/{self.page_id}", {"html": html})
        state["bookstack_drawing"] = image["id"]
        return f"added drawing {image['id']} to page {self.page_id}"
