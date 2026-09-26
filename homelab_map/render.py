"""Render draw.io XML to PNG and PDF with draw.io's own export server (the drawio-export container).

The PNG gets the diagram embedded in a tEXt chunk named "mxfile", the way draw.io itself does it,
so opening the PNG in draw.io (or BookStack's drawing editor) gives you the editable diagram back.
"""
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _post(url, xml, fmt, timeout=180):
    body = urllib.parse.urlencode({"format": fmt, "xml": xml, "scale": "1", "border": "0"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, body), timeout=timeout) as resp:
        return resp.read()


def wait_ready(url, seconds=60):
    """The export server answers GET / with 400 once it's up (it only takes POSTs)."""
    deadline = time.time() + seconds
    while True:
        try:
            urllib.request.urlopen(url, timeout=3)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            if time.time() > deadline:
                raise RuntimeError(f"draw.io export server at {url} isn't answering")
            time.sleep(1)


def png(url, xml):
    data = _post(url, xml, "png")
    if data[:8] != PNG_SIGNATURE:
        raise RuntimeError(f"export server returned something other than a PNG: {data[:120]!r}")
    return embed(data, xml)


def pdf(url, xml):
    """(The export server does PNG, JPG and PDF; it has no SVG.) Links in the map stay clickable in the PDF."""
    data = _post(url, xml, "pdf")
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"export server returned something other than a PDF: {data[:120]!r}")
    return data


def embed(png_bytes, xml):
    text = b"mxfile\x00" + urllib.parse.quote(xml, safe="").encode()
    chunk = struct.pack(">I", len(text)) + b"tEXt" + text + struct.pack(">I", zlib.crc32(b"tEXt" + text) & 0xFFFFFFFF)
    ihdr_end = 8 + 4 + 4 + 13 + 4  # signature + IHDR (length, type, data, CRC)
    return png_bytes[:ihdr_end] + chunk + png_bytes[ihdr_end:]
