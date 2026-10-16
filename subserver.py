#!/usr/bin/env python3
"""PABLO subscription page and transparent 3x-ui feed forwarder.

A browser receives the PABLO PasarGuard subscription interface rendered with
3x-ui's own links and quota headers. VPN applications receive the original raw
3x-ui subscription byte-for-byte. This sidecar also owns two Railway edge rules:

* a real public request synchronizes only PABLO's still-automatic Host and public
  subscription URI; a Host edited in the panel is operator-owned and preserved;
* a meaningless total=0/expire=0 Subscription-Userinfo header is omitted from
  raw app responses because several clients divide by zero and reject the feed.
"""

from __future__ import annotations

import base64
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

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
TEMPLATE_DIR = os.environ.get(
    "SUB_TEMPLATE_DIR", str(Path(__file__).resolve().parent / "templates")
)
SYNC_PATH = "/__pablo_sync_host"
_SYNC_LOCK = threading.Lock()
_SYNCED_HOST = None
_SYNCED_AT = 0.0
SYNC_TTL = 5.0
_TEMPLATE = None

# Every token below is emitted by browsers, not merely by an operating system.
# Do not match bare android/iphone/ipad: native VPN clients include those words.
BROWSER_UA = [
    "mozilla", "chrome", "safari", "firefox", "edg/", "edge", "opera", "opr/",
    "samsungbrowser", "crios", "fxios", "chromium", "yaapp", "yabrowser",
    "ucbrowser", "miuibrowser", "huaweibrowser", "vivobrowser", "oppobrowser",
    "trident", "gecko", "webkit", "iemobile",
]

# BaseHTTPRequestHandler creates Date and Server itself. Everything else here is
# hop-by-hop or explicitly regenerated from the final response body.
HOP_BY_HOP = {
    "date", "server", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    "content-length", "cache-control",
}

APP_IMPORTS = (
    ("v2rayNG", "android", "v2rayng://install-sub?url={url}"),
    ("Hiddify", "android", "hiddify://import/{url}"),
    ("V2Box", "ios", "v2box://install-sub?url={url}"),
    ("Streisand", "ios", "streisand://import/{url}"),
    ("NekoBox", "android", "sn://subscription?url={url}"),
    ("Happ", "ios", "happ://add/{url}"),
)


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


def parse_userinfo(value):
    """Read a Subscription-Userinfo header into integer fields."""
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
    """Return true only when a client has something meaningful to chart."""
    return bool(ui) and (ui.get("total", 0) > 0 or ui.get("expire", 0) > 0)


def to_upstream(path):
    """Map stable public /sub paths onto 3x-ui's private random subPath."""
    public = PUBLIC_PATH if PUBLIC_PATH.endswith("/") else PUBLIC_PATH + "/"
    private = SUB_REAL_PATH if SUB_REAL_PATH.endswith("/") else SUB_REAL_PATH + "/"
    if path == public.rstrip("/") or path in ("", "/") or path == public:
        return private + DEFAULT_SUBID
    if path.startswith(public):
        return private + path[len(public):]
    return path


def subscription_id(path):
    """Extract the 3x-ui subId represented by a public path."""
    public = PUBLIC_PATH if PUBLIC_PATH.endswith("/") else PUBLIC_PATH + "/"
    if path == public.rstrip("/") or path in ("", "/") or path == public:
        return DEFAULT_SUBID
    if path.startswith(public):
        value = urllib.parse.unquote(path[len(public):].strip("/"))
        return value or DEFAULT_SUBID
    return DEFAULT_SUBID


def is_browser_request(user_agent, accept, query=""):
    """Distinguish the human page from native subscription clients."""
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    if params.get("html") == ["1"] or any(
        str(v).lower() == "html" for v in params.get("view", [])
    ):
        return True

    accept = (accept or "").lower()
    if "text/html" in accept or "application/xhtml+xml" in accept:
        return True

    media = [part.split(";", 1)[0].strip() for part in accept.split(",") if part.strip()]
    vague = not media or all(item in ("*/*", "") for item in media)
    if not vague:
        return False
    ua = (user_agent or "").lower()
    return any(token in ua for token in BROWSER_UA)


def _raw_query(query):
    """Drop only selectors that would make the upstream return its HTML app."""
    pairs = urllib.parse.parse_qsl(query or "", keep_blank_values=True)
    pairs = [
        (key, value) for key, value in pairs
        if key.lower() not in ("html", "view")
    ]
    return urllib.parse.urlencode(pairs)


def decode_subscription_links(payload):
    """Decode 3x-ui's raw base64 feed into individual share links."""
    raw = (payload or b"").strip()
    if not raw:
        return []

    def links_from(text):
        return [
            line.strip() for line in text.replace("\r", "\n").split("\n")
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", line.strip())
        ]

    try:
        plain = raw.decode("utf-8")
    except UnicodeDecodeError:
        plain = ""
    links = links_from(plain)
    if links:
        return links

    compact = b"".join(raw.split())
    compact += b"=" * (-len(compact) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(compact).decode("utf-8")
        except Exception:
            continue
        links = links_from(decoded)
        if links:
            return links
    return []


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _subscription_rows(db_path, sub_id):
    try:
        con = sqlite3.connect(db_path, timeout=5)
        try:
            return con.execute(
                "SELECT email,total_gb,expiry_time,enable FROM clients "
                "WHERE sub_id=? ORDER BY id",
                (sub_id,),
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # page still works when the DB is briefly locked
        sys.stderr.write("[subfwd] could not read subscription clients: %s\n" % exc)
        return []


def _traffic_fallback(db_path, emails):
    if not emails:
        return 0
    try:
        con = sqlite3.connect(db_path, timeout=5)
        try:
            marks = ",".join("?" for _ in emails)
            row = con.execute(
                "SELECT COALESCE(SUM(up),0),COALESCE(SUM(down),0) "
                f"FROM client_traffics WHERE email IN ({marks})",
                emails,
            ).fetchone()
            return int(row[0] or 0) + int(row[1] or 0)
        finally:
            con.close()
    except Exception:
        return 0


def subscription_user(db_path, sub_id, userinfo=None, now=None):
    """Map 3x-ui client/header data onto PasarGuard's template contract."""
    now = now or datetime.now(timezone.utc)
    rows = _subscription_rows(db_path, sub_id)
    emails = [str(row[0]) for row in rows if row[0]]
    username = emails[0] if len(emails) == 1 else (sub_id or BRAND)

    ui = userinfo or {}
    used = int(ui.get("upload", 0) or 0) + int(ui.get("download", 0) or 0)
    if not userinfo:
        used = _traffic_fallback(db_path, emails)

    total = int(ui.get("total", 0) or 0)
    if not userinfo and rows:
        limits = [int(row[1] or 0) for row in rows]
        total = 0 if any(limit == 0 for limit in limits) else sum(limits)

    expire_seconds = int(ui.get("expire", 0) or 0)
    positive_expiries = [int(row[2] or 0) for row in rows if int(row[2] or 0) > 0]
    held_durations = [-int(row[2]) for row in rows if int(row[2] or 0) < 0]
    if expire_seconds > 100_000_000_000:  # tolerate a millisecond header
        expire_seconds //= 1000
    if not expire_seconds and len(positive_expiries) == 1:
        expire_seconds = positive_expiries[0] // 1000

    enabled = [_truthy(row[3]) for row in rows]
    on_hold = bool(rows and held_durations and len(held_durations) == len(rows))
    if enabled and not any(enabled):
        status = "disabled"
    elif on_hold:
        status = "on_hold"
    elif expire_seconds and expire_seconds <= int(now.timestamp()):
        status = "expired"
    elif total > 0 and used >= total:
        status = "limited"
    else:
        status = "active"

    expire = None
    if expire_seconds and not on_hold:
        try:
            expire = datetime.fromtimestamp(expire_seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            expire = None

    return SimpleNamespace(
        username=username,
        status=SimpleNamespace(value=status),
        used_traffic=used,
        data_limit=total,
        expire=expire,
        on_hold_expire_duration=(min(held_durations) / 1000 if on_hold else None),
    )


def _decode_base64_header(value):
    value = (value or "").strip()
    if not value:
        return ""
    if not value.lower().startswith("base64:"):
        return value
    try:
        return base64.b64decode(value.split(":", 1)[1]).decode("utf-8")
    except Exception:
        return ""


def _setting(db_path, key, default=""):
    try:
        con = sqlite3.connect(db_path, timeout=5)
        try:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row and row[0] is not None else default
        finally:
            con.close()
    except Exception:
        return default


def _bytesformat(number, *args, **kwargs):
    value = float(number or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def _datetimeformat(value, fmt="%Y-%m-%d %H:%M:%S"):
    return value.strftime(fmt) if value is not None else ""


def _get_template():
    global _TEMPLATE
    if _TEMPLATE is None:
        env = Environment(
            loader=FileSystemLoader([TEMPLATE_DIR]),
            autoescape=False,
            auto_reload=False,
        )
        env.filters["bytesformat"] = _bytesformat
        env.filters["datetime"] = _datetimeformat
        env.globals["now"] = lambda: datetime.now(timezone.utc)
        _TEMPLATE = env.get_template("subscription/index.html")
    return _TEMPLATE


def _apps(subscription_url):
    encoded = urllib.parse.quote(subscription_url, safe="")
    return [
        SimpleNamespace(
            name=name,
            platform=platform,
            icon_url="",
            import_url=pattern.format(url=encoded),
        )
        for name, platform, pattern in APP_IMPORTS
    ]


def render_subscription(db_path, request_path, host_header, headers, payload):
    """Render the copied PasarGuard page from 3x-ui's live subscription data."""
    sub_id = subscription_id(request_path)
    ui = parse_userinfo(headers.get("Subscription-Userinfo"))
    user = subscription_user(db_path, sub_id, ui)
    links = decode_subscription_links(payload)
    announce = _decode_base64_header(headers.get("Announce"))
    if not announce:
        announce = _setting(db_path, "subAnnounce", "")

    public_host = normalize_public_host(host_header) or "localhost"
    path = request_path or (PUBLIC_PATH + sub_id)
    subscription_url = "https://%s%s" % (public_host, path)
    return _get_template().render(
        user=user,
        links=links,
        announce=announce,
        announce_url="",
        apps=_apps(subscription_url),
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "pablo-subfwd"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if urllib.parse.urlparse(self.path).path == SYNC_PATH:
            return
        sys.stderr.write("[subfwd] %s\n" % (fmt % args))

    def _sync_host(self):
        # nginx marks this route internal. A transient SQLite lock must never
        # make the panel unavailable, so fail open and retry on the next request.
        host = normalize_public_host(self.headers.get("X-Public-Host"))
        try:
            if host:
                sync_public_host(host)
        except Exception as exc:  # noqa: BLE001 - log and retry next request
            sys.stderr.write("[subfwd] public endpoint sync failed: %s\n" % exc)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _target(self, parsed, query=None):
        query = parsed.query if query is None else query
        suffix = urllib.parse.quote(to_upstream(parsed.path), safe="/:%")
        if query:
            suffix += "?" + query
        return "http://127.0.0.1:%d%s" % (SUB_PORT, suffix)

    def _forward_headers(self):
        forwarded = {}
        for key in (
            "User-Agent", "Accept", "Accept-Language", "Cookie",
            "If-None-Match", "If-Modified-Since",
        ):
            value = self.headers.get(key)
            if value:
                forwarded[key] = value
        forwarded["Host"] = self.headers.get("Host") or "127.0.0.1:%d" % SUB_PORT
        return forwarded

    @staticmethod
    def _fetch(target, method, forwarded, body=None):
        request = urllib.request.Request(
            target, data=body, headers=forwarded, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def _bad_gateway(self, exc):
        sys.stderr.write("[subfwd] upstream failed: %s\n" % exc)
        payload = b"sub server unreachable"
        self.send_response(502)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_custom(self, html, method):
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Language", "fa")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def _send_upstream(self, status, headers, payload, method):
        ui = parse_userinfo(headers.get("Subscription-Userinfo"))
        self.send_response(status)
        for key, value in headers.items():
            low = key.lower()
            if low in HOP_BY_HOP:
                continue
            if low == "subscription-userinfo":
                if has_quota(ui):
                    self.send_header(
                        "Subscription-Userinfo",
                        "upload=%d; download=%d; total=%d; expire=%d" % (
                            ui.get("upload", 0), ui.get("download", 0),
                            ui.get("total", 0), ui.get("expire", 0),
                        ),
                    )
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def _relay(self, method):
        parsed = urllib.parse.urlparse(self.path)
        forwarded = self._forward_headers()
        wants_page = is_browser_request(
            forwarded.get("User-Agent"), forwarded.get("Accept"), parsed.query
        )

        length = self.headers.get("Content-Length")
        body = self.rfile.read(int(length)) if length and length.isdigit() else None

        if wants_page:
            raw_headers = {
                "Host": forwarded["Host"],
                "User-Agent": "PABLO-subpage/1.0",
                "Accept": "text/plain",
            }
            if forwarded.get("Accept-Language"):
                raw_headers["Accept-Language"] = forwarded["Accept-Language"]
            try:
                status, headers, payload = self._fetch(
                    self._target(parsed, _raw_query(parsed.query)),
                    "GET",  # a HEAD still needs data to compute the page length
                    raw_headers,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as a 502
                self._bad_gateway(exc)
                return

            if status == 200:
                try:
                    html = render_subscription(
                        DB_FILE,
                        parsed.path,
                        forwarded["Host"],
                        headers,
                        payload,
                    )
                    self._send_custom(html, method)
                    return
                except Exception as exc:  # fail back to 3x-ui's own page
                    sys.stderr.write(
                        "[subfwd] custom subscription render failed: %s\n" % exc
                    )
                    fallback = dict(forwarded)
                    fallback["Accept"] = "text/html,application/xhtml+xml,*/*;q=0.8"
                    try:
                        status, headers, payload = self._fetch(
                            self._target(parsed), method, fallback, body
                        )
                    except Exception as fallback_exc:  # noqa: BLE001
                        self._bad_gateway(fallback_exc)
                        return
            self._send_upstream(status, headers, payload, method)
            return

        try:
            status, headers, payload = self._fetch(
                self._target(parsed), method, forwarded, body
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a 502
            self._bad_gateway(exc)
            return
        self._send_upstream(status, headers, payload, method)

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
