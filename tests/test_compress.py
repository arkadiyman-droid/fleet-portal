"""Pages leave the server gzipped when the caller asks (Cloudflare does), so a
400 KB page does not cross the server's own Wi-Fi uncompressed."""
import gzip

from flask import Flask, Response, send_file

from app import compress


def _app():
    a = Flask("t")
    compress.init_app(a)
    a.add_url_rule("/big", "big", lambda: "<p>" + "hello fleet " * 500 + "</p>")
    a.add_url_rule("/small", "small", lambda: "<p>hi</p>")
    a.add_url_rule("/json", "json", lambda: {"x": ["value"] * 400})
    a.add_url_rule("/png", "png", lambda: Response(b"\x89PNG" + b"0" * 5000, mimetype="image/png"))
    a.add_url_rule("/stream", "stream", lambda: Response((c for c in ["<p>", "x" * 5000, "</p>"]), mimetype="text/html"))
    a.add_url_rule("/gone", "gone", lambda: ("<p>" + "missing " * 400 + "</p>", 404))
    a.add_url_rule("/file", "file", lambda: send_file(__file__, mimetype="text/plain"))
    return a


def test_a_big_page_is_gzipped_only_when_asked():
    c = _app().test_client()
    plain = c.get("/big")
    assert "Content-Encoding" not in plain.headers
    z = c.get("/big", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert z.headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in z.headers["Vary"]
    assert gzip.decompress(z.data) == plain.data and len(z.data) < len(plain.data) / 10
    assert int(z.headers["Content-Length"]) == len(z.data)
    j = c.get("/json", headers={"Accept-Encoding": "gzip"})
    assert j.headers["Content-Encoding"] == "gzip" and b"value" in gzip.decompress(j.data)


def test_what_is_left_alone():
    c = _app().test_client()
    h = {"Accept-Encoding": "gzip"}
    for url in ("/small", "/png", "/stream", "/gone", "/file"):
        assert "Content-Encoding" not in c.get(url, headers=h).headers, url
    assert "Content-Encoding" not in c.get("/big", headers={"Accept-Encoding": "gzip;q=0, br"}).headers
    assert "Content-Encoding" not in c.get("/big", headers={"Accept-Encoding": "identity"}).headers


def test_the_accept_encoding_reading():
    assert compress.wants_gzip("gzip") and compress.wants_gzip("br, GZIP;q=0.8") and compress.wants_gzip("*")
    assert not compress.wants_gzip("") and not compress.wants_gzip("br") and not compress.wants_gzip("gzip;q=0")
