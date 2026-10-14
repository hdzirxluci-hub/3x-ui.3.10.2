#!/usr/bin/env python3
"""A thin forwarder in front of 3x-ui's own subscription server.

The page a browser gets is 3x-ui's built-in subpage.html, untouched -- its own
title, its own layout, its own assets. This process does not render anything.
It exists to apply exactly one policy that the sub server itself cannot:

3x-ui always emits a Subscription-Userinfo header, filling 0 for any limit the
client does not have (internal/sub/calendar_expire.go formats it straight from
the client_traffic row), so a client with no cap arrives as
"upload=0; download=0; total=0; expire=0". Apps such as v2box divide usage by
total, hit zero, and report an error even though the configs came through fine.
So the header is forwarded only when it carries something to report.

Everything else -- status, content type, body, and every other header such as
Profile-Title and Profile-Update-Interval -- passes through as-is.
"""

import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SUBPAGE_PORT", "2097"))
SUB_PORT = int(os.environ.get("SUB_PORT", "2096"))
SUB_REAL_PATH = os.environ.get("SUB_REAL_PATH", "/subs/")
PUBLIC_PATH = os.environ.get("SUB_PATH", "/sub/")
DEFAULT_SUBID = os.environ.get("CLIENT_SUBID", "PABLO")

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
# The subscription page shipped inside the panel is violet. This block is
# appended to its HTML so the page comes out blue; it is the whole page's
# palette and nothing else, and it is re-declared after the page's own
# stylesheet so it wins without !important on the layout itself.
BLUE_CSS = """
/* ---- PABLO: blue ---- */
.subscription-page{
  --sub-grad-from:#0ea5e9;--sub-grad-to:#06b6d4;
  --sub-ink-from:#0369a1;--sub-ink-to:#0e7490;
  --sub-accent:#0284c7;
  --bg-page:linear-gradient(135deg,#e0f2fe 0%,#dbeafe 52%,#e0f7ff 100%);
  --sub-card-shadow:0 1px 3px rgba(15,23,42,.05),0 20px 56px rgba(2,132,199,.16);
  --sub-card-sheen:linear-gradient(135deg,rgba(255,255,255,.75),rgba(255,255,255,0) 42%,rgba(2,132,199,.22) 88%);
  --sub-blob-1:rgba(14,165,233,.5);--sub-blob-2:rgba(6,182,212,.45);
  --sub-grid:rgba(2,132,199,.06);
  --sub-hairline:linear-gradient(90deg,rgba(14,165,233,.45),rgba(6,182,212,.45));
  --sub-tile-bg:rgba(2,132,199,.05);--sub-tile-border:rgba(2,132,199,.12);
  --sub-row-bg:rgba(2,132,199,.045);--sub-row-border:rgba(2,132,199,.11);
  --sub-row-bg-hover:rgba(2,132,199,.09);--sub-row-border-hover:rgba(2,132,199,.3);
  --sub-row-glow:rgba(2,132,199,.28);
}
.subscription-page.is-dark{
  --sub-grad-from:#60a5fa;--sub-grad-to:#22d3ee;
  --sub-ink-from:#93c5fd;--sub-ink-to:#67e8f9;
  --sub-accent:#60a5fa;
  --bg-page:radial-gradient(ellipse 120% 90% at 18% -10%,#0b2545 0%,#101820 52%,#0b0f14 100%);
  --sub-card-shadow:0 1px 3px rgba(0,0,0,.4),0 24px 64px rgba(2,132,199,.24);
  --sub-card-sheen:linear-gradient(135deg,rgba(255,255,255,.16),rgba(255,255,255,0) 42%,rgba(96,165,250,.4) 88%);
  --sub-blob-1:rgba(14,165,233,.34);--sub-blob-2:rgba(34,211,238,.26);
  --sub-grid:rgba(255,255,255,.035);
  --sub-hairline:linear-gradient(90deg,rgba(96,165,250,.45),rgba(34,211,238,.45));
  --sub-tile-bg:rgba(96,165,250,.07);--sub-tile-border:rgba(96,165,250,.14);
  --sub-row-bg:rgba(96,165,250,.06);--sub-row-border:rgba(96,165,250,.12);
  --sub-row-bg-hover:rgba(96,165,250,.12);--sub-row-border-hover:rgba(96,165,250,.35);
  --sub-row-glow:rgba(14,165,233,.45);
}
.subscription-page.is-dark.is-ultra{
  --bg-page:radial-gradient(ellipse 120% 90% at 18% -10%,#071a33 0%,#05070c 55%,#000 100%);
  --sub-card-shadow:0 1px 3px rgba(0,0,0,.6),0 24px 64px rgba(3,105,161,.3);
  --sub-blob-1:rgba(14,165,233,.2);--sub-blob-2:rgba(34,211,238,.14);
}
/* antd's own tokens, so buttons, tabs, links and progress read blue too */
.subscription-page,.subscription-page *{
  --ant-color-primary:#0ea5e9!important;
  --ant-color-primary-hover:#38bdf8!important;
  --ant-color-primary-active:#0284c7!important;
  --ant-color-primary-border:#7dd3fc!important;
  --ant-color-primary-bg:rgba(14,165,233,.12)!important;
  --ant-color-link:#0284c7!important;
  --ant-color-info:#0ea5e9!important;
}
.subscription-page.is-dark,.subscription-page.is-dark *{
  --ant-color-primary:#38bdf8!important;
  --ant-color-primary-hover:#7dd3fc!important;
  --ant-color-primary-active:#0ea5e9!important;
  --ant-color-primary-border:#0ea5e9!important;
  --ant-color-primary-bg:rgba(56,189,248,.16)!important;
  --ant-color-link:#7dd3fc!important;
  --ant-color-info:#38bdf8!important;
}
/* antd keeps a second, per-component set of variables for tabs and progress --
   they are computed from the theme's own primary, so overriding
   --ant-color-primary alone leaves the active tab violet. */
.subscription-page, .subscription-page *{
  --ant-tabs-item-selected-color:#0284c7!important;
  --ant-tabs-ink-bar-color:#0284c7!important;
  --ant-tabs-item-hover-color:#0ea5e9!important;
  --ant-tabs-item-active-color:#0369a1!important;
  --ant-progress-remaining-color:rgba(14,165,233,.16)!important;
}
.subscription-page.is-dark, .subscription-page.is-dark *{
  --ant-tabs-item-selected-color:#38bdf8!important;
  --ant-tabs-ink-bar-color:#38bdf8!important;
  --ant-tabs-item-hover-color:#7dd3fc!important;
  --ant-tabs-item-active-color:#0ea5e9!important;
  --ant-progress-remaining-color:rgba(56,189,248,.2)!important;
}
.subscription-page.is-dark.is-ultra,.subscription-page.is-dark.is-ultra *{
  --ant-color-primary:#38bdf8!important;
  --ant-color-primary-hover:#7dd3fc!important;
  --ant-color-link:#7dd3fc!important;
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
        self._relay("GET")

    def do_HEAD(self):  # noqa: N802
        self._relay("HEAD")


def main():
    if not re.match(r"^/[A-Za-z0-9_\-./]*/$", SUB_REAL_PATH):
        sys.stderr.write("[subfwd] suspicious SUB_REAL_PATH %r\n" % SUB_REAL_PATH)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
