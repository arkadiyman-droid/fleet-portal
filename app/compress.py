"""
app/compress.py — gzip pages and data feeds before they leave the server

Every page goes out through a Cloudflare tunnel over the server's own internet
link (Wi-Fi, on beverly-pi). Cloudflare compresses for the visitor, but only
AFTER the page has crossed that link uncompressed: a 435 KB Live page or a
411 KB Analytics page spent 0.3-0.5 s on the way out. Compressed here they are
a tenth of the size. Only when the caller asks for it (Accept-Encoding: gzip,
which Cloudflare sends), only text the portal built itself (static files are
streamed from disk and left alone), and only above MIN_BYTES.
"""
import gzip

MIN_BYTES = 1024
LEVEL = 5                      # most of the gain at a fraction of level 9's CPU on a Pi
TYPES = {"text/html", "text/plain", "text/csv", "text/css", "application/json",
         "application/javascript", "text/javascript", "image/svg+xml"}


def wants_gzip(accept_encoding: str) -> bool:
    for part in (accept_encoding or "").lower().split(","):
        name, _, q = part.strip().partition(";")
        if name.strip() in ("gzip", "*"):
            q = q.strip()
            return not (q.startswith("q=") and q[2:].strip() in ("0", "0.0", "0.00", "0.000"))
    return False


def gzip_response(resp, accept_encoding: str):
    if (resp.status_code != 200 or resp.direct_passthrough or resp.is_streamed
            or "Content-Encoding" in resp.headers or resp.mimetype not in TYPES
            or not wants_gzip(accept_encoding)):
        return resp
    data = resp.get_data()
    if len(data) < MIN_BYTES:
        return resp
    resp.set_data(gzip.compress(data, compresslevel=LEVEL))
    resp.headers["Content-Encoding"] = "gzip"
    resp.vary.add("Accept-Encoding")
    return resp


def init_app(app):
    from flask import request

    @app.after_request
    def _gzip(resp):
        return gzip_response(resp, request.headers.get("Accept-Encoding", ""))
