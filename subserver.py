#!/usr/bin/env python3
"""A thin forwarder in front of 3x-ui's own subscription server.

The page a browser gets is 3x-ui's built-in subpage.html -- its own title, layout
and assets. The forwarder adds the blue accent and owns two edge-only policies
that the panel cannot know by itself:

* nginx supplies the hostname of a real public request to an internal endpoint.
  It is persisted as https://<host>/sub/ and as the managed inbound's :443/TLS
  subscription address, so 3x-ui never publishes its loopback :2096 or :10085.
* 3x-ui always emits a Subscription-Userinfo header, filling 0 for any limit the
  client does not have. Apps such as v2box divide usage by total, hit zero and
  report an error even though the configs arrived, so the header is forwarded
  only when it carries something meaningful to chart.

Everything else -- status, content type, body, and every other header such as
Profile-Title and Profile-Update-Interval -- passes through as-is.
"""

import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from public_endpoint import normalize_public_host, sync_public_endpoint

PORT = int(os.environ.get("SUBPAGE_PORT", "2097"))
SUB_PORT = int(os.environ.get("SUB_PORT", "2096"))
SUB_REAL_PATH = os.environ.get("SUB_REAL_PATH", "/subs/")
PUBLIC_PATH = os.environ.get("SUB_PATH", "/sub/")
DEFAULT_SUBID = os.environ.get("CLIENT_SUBID", "PABLO")
DB_FILE = os.environ.get("DB_FILE", "/etc/x-ui/x-ui.db")
INBOUND_PORT = int(os.environ.get("INBOUND_PORT", "10085"))
PUBLIC_PORT = int(os.environ.get("PUBLIC_PORT", "443"))
PUBLIC_TLS = os.environ.get("PUBLIC_TLS", "tls")
BRAND = os.environ.get("BRAND", "PABLO")
TUNNEL_PATH = os.environ.get("TUNNEL_PATH", "/ws")
SYNC_PATH = "/__pablo_sync_host"
_SYNC_LOCK = threading.Lock()
_SYNCED_HOST = None
_SYNCED_AT = 0.0
SYNC_TTL = 5.0


def sync_public_host(host):
    """Synchronize PABLO's automatic endpoint without reclaiming manual Hosts."""
    global _SYNCED_HOST, _SYNCED_AT
    now = time.monotonic()
    if not host or (_SYNCED_HOST == host and now - _SYNCED_AT < SYNC_TTL):
        return False

    with _SYNC_LOCK:
        now = time.monotonic()
        if _SYNCED_HOST == host and now - _SYNCED_AT < SYNC_TTL:
            return False

        result = sync_public_endpoint(
            DB_FILE,
            host,
            inbound_port=INBOUND_PORT,
            public_port=PUBLIC_PORT,
            security=PUBLIC_TLS,
            brand=BRAND,
            sub_path=PUBLIC_PATH,
            tunnel_path=TUNNEL_PATH,
        )
        _SYNCED_HOST = host
        _SYNCED_AT = time.monotonic()
        if result.changed:
            sys.stderr.write(
                "[subfwd] PABLO public endpoint synchronized to "
                "https://%s:443 (%d database change(s); %d manual Host(s) "
                "preserved)\n"
                % (host, result.changed, result.hosts_preserved)
            )
        return bool(result.changed)


# User agents that are plainly a browser. Used only to decide whether to ask
# for the HTML page when Accept is too vague to tell.
# Deliberately no bare "android", "iphone" or "ipad" here: an app's user agent
# routinely carries the platform ("Streisand/1.6.43 (com.kavakebi.streisand;
# build 1; iOS 17.5.1) Alamofire/5.10.2"), and matching on the platform alone
# would hand a VPN client an HTML page instead of its feed. Every entry below
# is something only a browser says.
BROWSER_UA = [
    "mozilla", "chrome", "safari", "firefox", "edg/", "edge", "opera", "opr/",
    "samsungbrowser", "crios", "fxios", "chromium", "yaapp", "yabrowser",
    "ucbrowser", "miuibrowser", "huaweibrowser", "vivobrowser", "oppobrowser",
    "trident", "gecko", "webkit", "iemobile",
]

# Headers that must not be echoed back: hop-by-hop ones, plus the ones this
# process sets itself.
# 3x-ui 3.10 brought its own subscription page, and it is already blue:
# a clean light/dark card driven by the theme tokens. So this block is no
# longer a repair -- it is only the accent, set to the lighter blue the
# rest of the brand uses, and it stays out of the way of everything else.
BLUE_CSS = """
/* ---- PABLO: blue accent ---- */
.subscription-page{
  --bg-page:#e8f1fb;--bg-card:#ffffff;
  --ant-color-primary:#0ea5e9!important;
  --ant-color-primary-hover:#38bdf8!important;
  --ant-color-primary-active:#0284c7!important;
  --ant-color-primary-border:#7dd3fc!important;
  --ant-color-primary-bg:rgba(14,165,233,.12)!important;
  --ant-color-link:#0284c7!important;
  --ant-color-info:#0ea5e9!important;
}
.subscription-page.is-dark{
  --bg-page:#0f1722;--bg-card:#161f2c;
  --ant-color-primary:#38bdf8!important;
  --ant-color-primary-hover:#7dd3fc!important;
  --ant-color-primary-active:#0ea5e9!important;
  --ant-color-primary-border:#0ea5e9!important;
  --ant-color-primary-bg:rgba(56,189,248,.16)!important;
  --ant-color-link:#7dd3fc!important;
  --ant-color-info:#38bdf8!important;
}
.subscription-page.is-dark.is-ultra{
  --bg-page:#04080d;--bg-card:#0d141d;
  --ant-color-primary:#38bdf8!important;
  --ant-color-primary-hover:#7dd3fc!important;
  --ant-color-link:#7dd3fc!important;
}
/* antd keeps a second set of variables per component, computed from the
   theme's own primary -- overriding --ant-color-primary alone leaves the
   active tab and the progress rail behind. */
.subscription-page,.subscription-page *{
  --ant-tabs-item-selected-color:#0284c7!important;
  --ant-tabs-ink-bar-color:#0284c7!important;
  --ant-tabs-item-hover-color:#0ea5e9!important;
  --ant-tabs-item-active-color:#0369a1!important;
  --ant-progress-remaining-color:rgba(14,165,233,.16)!important;
}
.subscription-page.is-dark,.subscription-page.is-dark *{
  --ant-tabs-item-selected-color:#38bdf8!important;
  --ant-tabs-ink-bar-color:#38bdf8!important;
  --ant-tabs-item-hover-color:#7dd3fc!important;
  --ant-tabs-item-active-color:#0ea5e9!important;
  --ant-progress-remaining-color:rgba(56,189,248,.2)!important;
}
"""

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}


def parse_userinfo(value):
    """Read the Subscription-Userinfo header into numbers, or None."""
    out = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        try:
            out[key.strip().lower()] = int(val.strip())
        except ValueError:
            pass
    return out or None


def has_quota(ui):
    """Is there anything a client could actually chart?"""
    return bool(ui) and (ui.get("total", 0) > 0 or ui.get("expire", 0) > 0)


def to_upstream(path):
    """Map the public /sub/... path onto the sub server's own random path.

    /sub/PABLO    -> /<subPath>PABLO
    /sub or /sub/    -> /<subPath><default id>
    /sub/whatever/x  -> /<subPath>whatever/x

    The bare path has no sub id in it, and the sub server has no index to
    answer with, so it falls back to the panel's default client. That keeps
    /sub and /sub/ working instead of 404ing.
    """
    base = SUB_REAL_PATH if SUB_REAL_PATH.endswith("/") else SUB_REAL_PATH + "/"
    if path == PUBLIC_PATH.rstrip("/") or path in ("", "/") or path == PUBLIC_PATH:
        return base + DEFAULT_SUBID
    if path.startswith(PUBLIC_PATH):
        return base + path[len(PUBLIC_PATH):]
    return path



def recolour(html):
    """Put the blue palette into the panel's own subscription page."""
    if b"PABLO: blue" in html:
        return html
    marker = b"</body>"
    style = ("<style>%s</style>" % BLUE_CSS).encode("utf-8")
    at = html.rfind(marker)
    if at == -1:
        return html + style
    return html[:at] + style + html[at:]

class Handler(BaseHTTPRequestHandler):
    server_version = "subfwd"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[subfwd] %s\n" % (fmt % args))

    def _sync_host(self):
        # nginx marks this route internal and supplies its normalized $host.
        # Fail open: a transient SQLite lock must never make the panel itself
        # unavailable; the next request retries because the cache was not set.
        host = normalize_public_host(self.headers.get("X-Public-Host"))
        try:
            if host:
                sync_public_host(host)
        except Exception as e:  # noqa: BLE001 - log and retry next request
            sys.stderr.write("[subfwd] public endpoint sync failed: %s\n" % e)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _relay(self, method):
        parsed = urllib.parse.urlparse(self.path)
        target = "http://127.0.0.1:%d%s" % (
            SUB_PORT,
            urllib.parse.quote(to_upstream(parsed.path), safe="/:%")
            + (("?" + parsed.query) if parsed.query else ""),
        )

        # Forward the client's own headers so the sub server can still tell a
        # browser from a VPN app -- that is what makes it serve its HTML page
        # to one and base64 to the other.
        fwd = {}
        for key in ("User-Agent", "Accept", "Accept-Language", "Cookie",
                    "If-None-Match", "If-Modified-Since"):
            value = self.headers.get(key)
            if value:
                fwd[key] = value
        fwd["Host"] = self.headers.get("Host") or "127.0.0.1:%d" % SUB_PORT

        # 3x-ui serves its HTML page only when Accept mentions text/html. Every
        # real browser says so, but an in-app webview or a browser behind a
        # header-stripping proxy can arrive with a bare */* and would silently
        # be handed base64 -- the same blank screen this whole thing exists to
        # prevent. If the user agent is plainly a browser and Accept says
        # nothing useful, ask for the page on its behalf.
        ua = (fwd.get("User-Agent") or "").lower()
        accept = (fwd.get("Accept") or "").lower()
        # If Accept already names a concrete type that is not HTML, the caller
        # has told us what it wants -- an app asking for application/json must
        # not be redirected to the page. Only a vague or empty Accept is a
        # case worth helping.
        vague = (not accept) or accept in ("*/*", "*/*;q=0.8")
        if vague and any(b in ua for b in BROWSER_UA):
            fwd["Accept"] = "text/html,application/xhtml+xml,*/*;q=0.8"

        length = self.headers.get("Content-Length")
        body = self.rfile.read(int(length)) if length and length.isdigit() else None

        req = urllib.request.Request(target, data=body, headers=fwd, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, headers, payload = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, headers, payload = e.code, e.headers, e.read()
        except Exception as e:  # noqa: BLE001 - surfaced as a 502
            sys.stderr.write("[subfwd] upstream failed: %s\n" % e)
            payload = b"sub server unreachable"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        ctype = (headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            payload = recolour(payload)

        ui = parse_userinfo(headers.get("Subscription-Userinfo"))

        self.send_response(status)
        for key, value in headers.items():
            low = key.lower()
            if low in HOP_BY_HOP:
                continue
            if low == "subscription-userinfo":
                # Only pass it on when it says something. total=0 means the
                # client has no cap, and apps that chart usage break on it.
                if has_quota(ui):
                    self.send_header(
                        "Subscription-Userinfo",
                        "upload=%d; download=%d; total=%d; expire=%d" % (
                            ui.get("upload", 0), ui.get("download", 0),
                            ui.get("total", 0), ui.get("expire", 0)))
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if urllib.parse.urlparse(self.path).path == SYNC_PATH:
            self._sync_host()
            return
        self._relay("GET")

    def do_HEAD(self):  # noqa: N802
        if urllib.parse.urlparse(self.path).path == SYNC_PATH:
            self._sync_host()
            return
        self._relay("HEAD")


def main():
    if not re.match(r"^/[A-Za-z0-9_\-./]*/$", SUB_REAL_PATH):
        sys.stderr.write("[subfwd] suspicious SUB_REAL_PATH %r\n" % SUB_REAL_PATH)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
