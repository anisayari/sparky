"""Web search parsing, page text extraction, SSRF guard (no network except the optional live test)."""
import base64, io, os, socket, ssl
from unittest.mock import Mock

import pytest

import web

DDG = """<html><body>
<div class="result results_links results_links_deep web-result">
 <h2 class="result__title"><a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fa%3D1%26b%3D2&amp;rut=abc">Example <b>Page</b></a></h2>
 <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">The <b>best</b> example   page.</a>
</div>
<div class="result"><h2><a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ads.com">Ad</a></h2></div>
<div class="result"><h2><a class="result__a" href="https://second.org/">Second &amp; more</a></h2>
 <div class="result__snippet">Snippet two</div></div>
</body></html>"""

BING_TARGET = "https://bing-result.net/a?b=1"
BING = f"""<ol id="b_results">
<li class="b_algo"><div class="b_tpcn"><a href="https://bing-result.net/" class="tilk"></a></div>
 <h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=xyz&amp;u=a1{base64.urlsafe_b64encode(BING_TARGET.encode()).decode().rstrip('=')}&amp;ntb=1">Bing title</a></h2>
 <div class="b_caption"><p>Bing <strong>snippet</strong></p></div></li>
<li class="b_algo"><h2><a href="https://other.io/x">Other</a></h2><ul><li>deep</li></ul><p>Other snippet</p></li>
</ol>"""

PAGE = """<!doctype html><html><head><title> My Article </title><style>.x{color:red}</style>
<script>var secret = 1;</script></head><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<header>Site banner</header>
<h1>Main title</h1><p>First   paragraph
 with <a href="#">a link</a>.</p>
<svg><title>icon</title></svg>
<h2>Section</h2><ul><li>one</li><li>two</li></ul>
<footer>Copyright footer</footer></body></html>"""


def test_ddg_parsing_and_redirect_decoding():
    r = web.parse_results(DDG, "ddg")
    assert r == [{"title": "Example Page", "url": "https://example.com/page?a=1&b=2", "snippet": "The best example page."},
                 {"title": "Second & more", "url": "https://second.org/", "snippet": "Snippet two"}]


def test_bing_parsing():
    r = web.parse_results(BING, "bing")
    assert r[0]["url"] == BING_TARGET and r[0]["snippet"] == "Bing snippet"
    assert r[1] == {"title": "Other", "url": "https://other.io/x", "snippet": "Other snippet"}


def test_decode_link():
    assert web.decode_link("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2F%C3%A9t%C3%A9&rut=1") == "https://a.com/été"
    assert web.decode_link("https://plain.com/x") == "https://plain.com/x"


def test_html_text():
    title, text = web.html_text(PAGE)
    assert title == "My Article"
    assert text == "# Main title\n\nFirst paragraph with a link.\n\n## Section\n\n- one\n- two"
    for gone in ("secret", "color", "Home", "banner", "footer", "icon"):
        assert gone not in text


def test_html_text_prefers_main():
    body = "<main><p>" + "real content " * 60 + "</p></main><div>sidebar junk</div>"
    assert "sidebar" not in web.html_text(body)[1]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/", "http://localhost:7860/api", "http://10.0.0.5/",
                                 "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                                 "http://0.0.0.0/", "file:///C:/Windows/win.ini", "ftp://example.com/"])
def test_ssrf_guard(url):
    with pytest.raises(ValueError):
        web.check_url(url)
    with pytest.raises(ValueError):
        web.fetch(url)


def transport(monkeypatch, addresses, response):
    """Exercise http.client's request and response paths without any real network I/O."""
    answers = iter(addresses)
    dns = Mock(side_effect=lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
                                                    for ip in next(answers)])
    stream = Mock()
    stream.makefile.return_value = io.BytesIO(response)
    tls = Mock()
    tls.wrap_socket.return_value = stream
    monkeypatch.setattr(web.socket, "getaddrinfo", dns)
    monkeypatch.setattr(web.socket, "socket", Mock(return_value=stream))
    monkeypatch.setattr(web.ssl, "create_default_context", Mock(return_value=tls))
    return dns, stream, tls


def test_redirect_to_private_address_is_refused(monkeypatch):
    dns, stream, _ = transport(monkeypatch, [["1.1.1.1"], ["127.0.0.1"]],
                               b"HTTP/1.1 302 Found\r\nLocation: /second\r\nContent-Length: 0\r\n\r\n")
    with pytest.raises(ValueError, match="private"):
        web.fetch("https://public.example/")
    assert dns.call_count == 2  # including a relative redirect to the same host
    stream.connect.assert_called_once_with(("1.1.1.1", 443))


def test_pinned_https_keeps_original_tls_and_http_identity(monkeypatch):
    dns, stream, tls = transport(monkeypatch, [["1.1.1.1"], ["127.0.0.1"]],
                                 b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello")
    monkeypatch.setenv("https_proxy", "http://untrusted.example:1234")
    assert web.fetch("https://public.example:8443/test?q=ok#fragment")["text"] == "hello"
    assert dns.call_count == 1
    stream.connect.assert_called_once_with(("1.1.1.1", 8443))
    tls.wrap_socket.assert_called_once_with(stream, server_hostname="public.example")
    raw = stream.sendall.call_args.args[0]
    assert b"Host: public.example:8443\r\n" in raw and raw.startswith(b"GET /test?q=ok HTTP/1.1\r\n")


def test_mixed_addresses_are_rejected_before_connecting(monkeypatch):
    _, stream, _ = transport(monkeypatch, [["1.1.1.1", "10.0.0.1"]], b"")
    with pytest.raises(ValueError, match="private"):
        web.fetch("http://mixed.example")
    stream.connect.assert_not_called()


@pytest.mark.parametrize("ip", ["::ffff:127.0.0.1", "64:ff9b::7f00:1", "fec0::1", "2002:7f00:1::1"])
def test_rejects_local_and_translated_local_ipv6(monkeypatch, ip):
    _, stream, _ = transport(monkeypatch, [[ip]], b"")
    with pytest.raises(ValueError, match="private"):
        web.fetch("http://mixed.example")
    stream.connect.assert_not_called()


def test_pinned_cert_failure_never_sends_request(monkeypatch):
    _, stream, tls = transport(monkeypatch, [["1.1.1.1"]], b"")
    tls.wrap_socket.side_effect = ssl.SSLCertVerificationError("test invalid certificate")
    with pytest.raises(ssl.SSLCertVerificationError):
        web.fetch("https://public.example")
    stream.sendall.assert_not_called()
    stream.close.assert_called_once()


def test_configured_local_search_engine_cannot_redirect_to_other_private_origin(monkeypatch):
    _, stream, _ = transport(monkeypatch, [["127.0.0.1"], ["127.0.0.1"]],
                             b"HTTP/1.1 302 Found\r\nLocation: http://other.example/\r\nContent-Length: 0\r\n\r\n")
    with pytest.raises(ValueError, match="private"):
        web._searx("hello", 1, "http://search.example:8888")
    stream.connect.assert_called_once_with(("127.0.0.1", 8888))


def test_results_block():
    res = [{"title": "A", "url": "https://a.com", "snippet": "snip a"}, {"title": "B", "url": "https://b.com", "snippet": "snip b"}]
    pages = [{"url": "https://b.com", "title": "B", "text": "full text of b"}, {"url": "https://c.com", "title": "C", "text": "c text"}]
    out = web.results_block("q", res, pages)
    assert "[1] A — https://a.com\nsnip a" in out
    assert "[2] B — https://b.com\nfull text of b" in out and "snip b" not in out
    assert "[3] C — https://c.com\nc text" in out
    assert "[n]" in out
    assert "no results" in web.results_block("q", [], [])


@pytest.mark.skipif(os.environ.get("SPARKY_LIVE") != "1", reason="network test: set SPARKY_LIVE=1")
def test_live_search_and_fetch():
    r = web.search("python programming language", 5)
    assert r and all(x["url"].startswith("http") for x in r)
    page = web.fetch("https://www.python.org/")
    assert "Python" in page["title"] and len(page["text"]) > 200
