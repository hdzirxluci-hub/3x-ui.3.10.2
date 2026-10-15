#!/usr/bin/env bash
#
# Container entrypoint. Order matters here:
#
#   1. Configure the panel through its CLI *before* it ever starts. The CLI
#      writes straight to /etc/x-ui/x-ui.db, so there is no reload to wait for.
#   2. Start the panel. It spawns Xray from bin/config.json.
#   3. Create the inbound through the HTTP API. This writes to the database but
#      does NOT reliably regenerate bin/config.json, so Xray keeps running the
#      config it booted with.
#   4. Therefore: if step 3 created or changed anything, restart the panel once
#      so Xray is relaunched with the inbound present. Bootstrap is idempotent,
#      so the second pass finds the inbound, changes nothing and stops looping.
#   5. Render nginx and supervise both processes.
#
# If either process dies the container exits and Railway restarts it.

set -euo pipefail

XUI_DIR=/usr/local/x-ui
XUI_BIN="${XUI_DIR}/x-ui"
DB_FILE=/etc/x-ui/x-ui.db

# Scratch space for the rendered nginx config and the bootstrap handoff.
# /run is correct inside the container (it runs as root); fall back so the
# scripts are also runnable outside it, e.g. in CI.
RUNTIME_DIR="${RUNTIME_DIR:-}"
if [ -z "$RUNTIME_DIR" ]; then
  if mkdir -p /run/xui-railway 2>/dev/null && [ -w /run/xui-railway ]; then
    RUNTIME_DIR=/run/xui-railway
  else
    RUNTIME_DIR="$(mktemp -d)"
  fi
fi
export RUNTIME_DIR

# --- configuration -----------------------------------------------------------
# Fixed values, not settings. Railway Variables is not a control panel for this
# service: the only thing read from there is the admin username and password
# (see the credentials block further down, and note that it only applies them
# when you actually set them). Brand, subscription page wording, ports, paths
# and transport all live here, so a stray variable cannot rename the brand, blank
# the subscription page or repoint the panel.

# The panel renders dates in whatever zone the container is in, and Railway
# containers come up on UTC. A date three and a half hours off is enough for a
# client to look expired when it is not.
export TZ=Asia/Tehran

HTTP_PORT="${PORT:-8080}"   # Railway injects PORT; the edge routes to it
PANEL_PORT=2053
SUB_PORT=2096
INBOUND_PORT=10085
PANEL_PATH=/panel/
SUB_PATH=/sub/
TUNNEL_PATH=/ws
TRANSPORT=ws
CLIENT_EMAIL=user1
CLIENT_SUBID=PABLO
SUBPAGE_PORT=2097
BRAND=PABLO
# The name every config carries in the client app, in the bold face the brand
# uses. {{EMAIL}} is the name typed in the panel for that client, so one person's
# configs read 𝗣𝗔𝗕𝗟𝗢 | <their name> and the next person's read 𝗣𝗔𝗕𝗟𝗢 | <theirs>.
# Held as a literal because the bold face is characters, not a font.
BOLD_BRAND="𝗣𝗔𝗕𝗟𝗢"
REMARK_TEMPLATE="${BOLD_BRAND} | {{EMAIL}}"
# What this project named configs before the separator changed, so a deployment
# made with an earlier release is renamed too instead of keeping the old one.
REMARK_LEGACY="${BRAND} • {{EMAIL}}"
# The built-in subscription page has two slots of its own: a title rendered at
# the top of the card, and an "announce" box rendered below it. Both are plain
# panel settings, so the branding lives there rather than in a custom page.
SUB_TITLE="$BOLD_BRAND"
SUB_ANNOUNCE="این پنل توسط Pablo ساخته شده"
# subSupportUrl is the one field the built-in page puts in its <footer>, so it
# is the slot at the very bottom of the subscription page.
SUB_SUPPORT_URL="t.me/poll_o0"

export PANEL_PATH SUB_PATH TUNNEL_PATH HTTP_PORT PANEL_PORT SUB_PORT INBOUND_PORT
export TRANSPORT CLIENT_EMAIL CLIENT_SUBID
# BRAND is read inside bootstrap.sh (the inbound gets named after it), and
# bootstrap.sh runs under `set -u`, so it has to be exported -- an unexported
# variable there is an "unbound variable" error, not a silent empty string.
export BRAND BOLD_BRAND REMARK_TEMPLATE

# The public hostname is what every generated link contains. It comes from the
# platform and nowhere else: Railway injects RAILWAY_PUBLIC_DOMAIN once a domain
# exists, and that value cannot be wrong. Reading it from a variable someone
# typed is how links end up pointing at an address that never answers.
PUBLIC_HOST="${RAILWAY_PUBLIC_DOMAIN:-}"
PUBLIC_PORT=443
PUBLIC_TLS=tls
export PUBLIC_HOST PUBLIC_PORT PUBLIC_TLS

log() { printf '[entrypoint] %s\n' "$*"; }

# Reads the live panel username out of the database, so a credential changed
# from the panel UI survives a reboot instead of being overwritten by the
# defaults. The column name cannot be a bound parameter (SQLite would treat it
# as a string literal), so it is matched against a whitelist instead.
read_credential() {
  case "$1" in
    username) COL=username ;;
    password) COL=password ;;
    *) return 1 ;;
  esac
  python3 - "$COL" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    row = con.execute(f"SELECT {sys.argv[1]} FROM users ORDER BY id LIMIT 1").fetchone()
    print(row[0] if row else "")
except Exception:
    print("")
PY
}

# --- embedded bootstrap -----------------------------------------------------
# Kept in this one file so the repository stays small enough to create from
# a phone. It is written out verbatim at boot (quoted heredoc: no expansion)
# so it stays one reviewable bash script rather than a quoting maze.
RUNTIME_DIR_BOOT="${RUNTIME_DIR}"
write_bootstrap() {
  cat > "${RUNTIME_DIR_BOOT}/bootstrap.sh" <<'BOOTSTRAP_EOF'
#
# Bootstraps the panel and one ready-to-use inbound, talking to the panel's own
# HTTP API. Runs once per boot; it is idempotent, so a second boot is a no-op.
#
# Everything here was derived by probing 3x-ui v3.9.0 directly, because two of
# its behaviours are not what the existing templates assume:
#
#   1. The API base is "<webBasePath>panel/api/". With PANEL_PATH=/panel/ that
#      reads /panel/panel/api/... and looks like a typo. With /abc/ it is
#      /abc/panel/api/. Guessing "<webBasePath>api/" gives 404 on every call.
#   2. Unsafe requests need an X-CSRF-Token header. Without it POST /login
#      answers 403 with an empty body, which reads like bad credentials.
#
# Usage: bootstrap.sh   (reads configuration from the environment)

set -euo pipefail

PANEL_PATH=/panel/
PANEL_PORT=2053
INBOUND_PORT=10085
PUBLIC_HOST="${PUBLIC_HOST:-}"
PUBLIC_PORT=443
PUBLIC_TLS=tls            # tls | none  (Railway's edge terminates TLS -> the link must say tls)
TRANSPORT=ws              # ws | xhttp  (ws works in every client)
TUNNEL_PATH=/ws           # public path nginx exposes; also the internal path
CLIENT_EMAIL=user1
CLIENT_SUBID=PABLO
INBOUND_REMARK="$BRAND"
# Exported by the entrypoint: on the first boot this is the seeded admin/admin,
# which is the only boot on which this script can log in at all -- once the
# database exists the stored password is a bcrypt hash and there is nothing to
# authenticate with.
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"

BOOTSTRAP_TIMEOUT=90

BASE="http://127.0.0.1:${PANEL_PORT}${PANEL_PATH}"
API="${BASE}panel/api"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

log()  { printf '[bootstrap] %s\n' "$*"; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; return 1; }

# APPLY_CREDS=0 means the entrypoint deliberately left the stored credentials
# alone, so what we hold is a bcrypt hash rather than a password and there is
# nothing to log in with. The inbound already exists from the first boot, so
# leaving it untouched is the correct no-op.
if [ "${APPLY_CREDS:-1}" = "0" ]; then
  log "credentials live in the panel database; skipping the inbound bootstrap"
  log "the public address is handled separately, without logging in"
  exit 0
fi

# --- wait for the panel ------------------------------------------------------
deadline=$(( $(date +%s) + BOOTSTRAP_TIMEOUT ))
until curl -fsS -o /dev/null --max-time 5 "${BASE}"; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    die "panel did not answer on ${BASE} within ${BOOTSTRAP_TIMEOUT}s"
  fi
  sleep 1
done
log "panel is up at ${BASE}"

# --- session -----------------------------------------------------------------
# The CSRF token is bound to the session cookie, so fetch it with the same jar
# that the login POST uses.
csrf_token() {
  curl -fsS -b "$JAR" -c "$JAR" --max-time 10 "${BASE}csrf-token" \
    | jq -er '.obj'
}

CSRF="$(csrf_token)" || die "could not mint a CSRF token"
LOGIN_BODY=$(curl -sS -b "$JAR" -c "$JAR" --max-time 10 \
  -X POST "${BASE}login" \
  -H "X-CSRF-Token: ${CSRF}" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=${ADMIN_USERNAME}" \
  --data-urlencode "password=${ADMIN_PASSWORD}")
echo "$LOGIN_BODY" | jq -e '.success == true' >/dev/null \
  || die "panel login failed: ${LOGIN_BODY}"
log "logged in as ${ADMIN_USERNAME}"

api_get() {
  curl -fsS -b "$JAR" --max-time 20 "${API}$1"
}
api_post() {
  curl -fsS -b "$JAR" --max-time 30 -X POST "${API}$1" \
    -H "X-CSRF-Token: ${CSRF}" -H 'Content-Type: application/json' --data @"$2"
}

# --- inbound -----------------------------------------------------------------
EXISTING="$(api_get /inbounds/list | jq -c '[.obj[]? | select(.port == '"${INBOUND_PORT}"')]')"
COUNT="$(echo "$EXISTING" | jq 'length')"

UUID="$(cat /proc/sys/kernel/random/uuid)"
if [ "$COUNT" -gt 0 ]; then
  UUID="$(echo "$EXISTING" | jq -r '.[0].settings.clients[0].id // empty')"
  [ -n "$UUID" ] || UUID="$(cat /proc/sys/kernel/random/uuid)"
  log "inbound on port ${INBOUND_PORT} already exists; reusing uuid ${UUID}"
fi

# Build the payload in Python so the nested JSON strings stay valid without a
# quoting maze. 3x-ui stores settings/streamSettings as JSON *strings*.
PAYLOAD="$(mktemp)"
CLIENT_UUID="$UUID" \
INBOUND_PORT="$INBOUND_PORT" \
TRANSPORT="$TRANSPORT" \
TUNNEL_PATH="$TUNNEL_PATH" \
PUBLIC_HOST="$PUBLIC_HOST" \
PUBLIC_PORT="$PUBLIC_PORT" \
PUBLIC_TLS="$PUBLIC_TLS" \
CLIENT_EMAIL="$CLIENT_EMAIL" \
CLIENT_SUBID="$CLIENT_SUBID" \
INBOUND_REMARK="$INBOUND_REMARK" \
python3 - > "$PAYLOAD" <<'PY'
import json, os

transport  = os.environ["TRANSPORT"]
path       = os.environ["TUNNEL_PATH"]
host       = os.environ["PUBLIC_HOST"]
port       = int(os.environ["PUBLIC_PORT"])
force_tls  = os.environ["PUBLIC_TLS"]
in_port    = int(os.environ["INBOUND_PORT"])

stream = {"network": transport, "security": "none"}

# externalProxy is what rewrites the address in generated links and in the
# subscription. Without it the link says localhost:<internal port>.
if host:
    stream["externalProxy"] = [{
        "forceTls": force_tls, "dest": host, "port": port,
        "remark": os.environ["INBOUND_REMARK"], "sni": host,
    }]

if transport == "ws":
    stream["wsSettings"] = {
        "acceptProxyProtocol": False, "path": path, "host": host, "headers": {},
    }
else:
    stream["xhttpSettings"] = {
        "path": path, "host": host, "mode": "auto", "extra": {},
        "maxConnections": 0, "maxConnectionSecs": 0,
        "scMaxEachPostBytes": 0, "scMinPostsIntervalMs": 0,
        "scKeepAliveIntervalSecs": 0,
        "xPaddingBytes": "100-1000", "noGRPCHeader": False,
        "grpcFirstPacketWait": 0,
        "xmux": {
            "maxConcurrency": "16-32", "maxConnections": 0,
            "cMaxRequestsSec": 0, "cMaxRequestSize": 0,
            "minVersion": "2", "maxVersion": "3",
            "hMaxRequestSecs": 30, "hKeepAlivePeriod": 20,
        },
    }

settings = {
    "clients": [{
        "id": os.environ["CLIENT_UUID"], "flow": "",
        "email": os.environ["CLIENT_EMAIL"], "limitIp": 0, "totalGB": 0,
        "expiryTime": 0, "enable": True, "tgId": "",
        "subId": os.environ["CLIENT_SUBID"], "reset": 0,
    }],
    "decryption": "none", "fallbacks": [],
}

print(json.dumps({
    "up": 0, "down": 0, "total": 0,
    "remark": os.environ["INBOUND_REMARK"],
    "enable": True, "expiryTime": 0,
    "listen": "", "port": in_port, "protocol": "vless",
    "settings": json.dumps(settings),
    "streamSettings": json.dumps(stream),
    "tag": f"in-{in_port}-tcp",
    "sniffing": json.dumps({
        "enabled": True,
        "destOverride": ["http", "tls", "quic", "fakedns"],
        "metadataOnly": False, "routeOnly": False,
    }),
}))
PY

if [ "$COUNT" -gt 0 ]; then
  ID="$(echo "$EXISTING" | jq -r '.[0].id')"
  RESP="$(api_post "/inbounds/update/${ID}" "$PAYLOAD")"
  VERB="updated"
else
  RESP="$(api_post /inbounds/add "$PAYLOAD")"
  VERB="created"
fi
echo "$RESP" | jq -e '.success == true' >/dev/null || die "inbound ${VERB} failed: ${RESP}"
rm -f "$PAYLOAD"
log "inbound ${VERB}: vless/${TRANSPORT} on internal port ${INBOUND_PORT}, path ${TUNNEL_PATH}"

# --- subscription path -------------------------------------------------------
# The panel generates a random subPath and serves it on its own port. nginx
# needs it to map the stable public /sub/ onto it, so hand it back to the
# caller on stdout as a single line the entrypoint can read.
SUB_REAL_PATH="$(python3 - <<'PY'
import sqlite3, os
db = "/etc/x-ui/x-ui.db"
try:
    con = sqlite3.connect(db)
    row = con.execute("SELECT value FROM settings WHERE key='subPath'").fetchone()
    print(row[0] if row and row[0] else "/subs/")
except Exception:
    print("/subs/")
PY
)"
log "panel subscription path is ${SUB_REAL_PATH}"

# --- subURI ------------------------------------------------------------------
# Left empty, the panel builds the subscription link it shows on the Clients
# page from subDomain and subPort, which gives
# http://<domain>:2096<subPath><subId>. Railway exposes exactly one public port
# over HTTPS, so that link can never be reached from a browser or an app.
# subURI overrides it. Only fill in the broken defaults -- a custom domain the
# admin typed by hand is left alone.
if [ -n "$PUBLIC_HOST" ]; then
  python3 - "$PUBLIC_HOST" "$SUB_PATH" <<'PY'
import sqlite3, sys
host, sub_path = sys.argv[1], sys.argv[2]
want = f"https://{host}{sub_path}"
db = "/etc/x-ui/x-ui.db"
try:
    con = sqlite3.connect(db)
    row = con.execute("SELECT value FROM settings WHERE key='subURI'").fetchone()
    cur = row[0] if row else ""
    if cur and ":2096" not in cur and not cur.startswith("http://"):
        print(f"[bootstrap] subURI already set to {cur}; leaving it alone")
    else:
        if row:
            con.execute("UPDATE settings SET value=? WHERE key='subURI'", (want,))
        else:
            con.execute("INSERT INTO settings(key,value) VALUES('subURI',?)", (want,))
        con.commit()
        print(f"[bootstrap] subURI set to {want}")
except Exception as e:
    print(f"[bootstrap] WARNING: could not set subURI: {e}")
PY
else
  log "PUBLIC_HOST is empty; not setting subURI, so the panel will show a :2096 link"
  log "set PUBLIC_HOST in Railway Variables (or Generate Domain then Redeploy)"
fi

# --- report ------------------------------------------------------------------
# The entrypoint sources this file, so every value has to be shell-quoted.
# A vless:// link ends in "#remark", and an unquoted '#' starts a comment:
# CLIENT_LINK=vless://...#remark silently assigns the part before the '#'.
shell_quote() {
  # Wrap in single quotes, escaping any single quote inside the value.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

LINKS="$(api_get /inbounds/allLinks | jq -r '.obj[]?' | head -1 || true)"
{
  printf 'PANEL_API_OK=1\n'
  printf 'SUB_REAL_PATH=%s\n'  "$(shell_quote "$SUB_REAL_PATH")"
  printf 'CLIENT_UUID=%s\n'    "$(shell_quote "$UUID")"
  printf 'INBOUND_VERB=%s\n'   "$(shell_quote "$VERB")"
  if [ -n "$LINKS" ]; then
    printf 'CLIENT_LINK=%s\n'  "$(shell_quote "$LINKS")"
  fi
} > "${RUNTIME_DIR}/bootstrap.env"

log "wrote ${RUNTIME_DIR}/bootstrap.env"
BOOTSTRAP_EOF
  chmod +x "${RUNTIME_DIR_BOOT}/bootstrap.sh"
}

# --- 1. panel settings -------------------------------------------------------
mkdir -p /etc/x-ui /var/log/x-ui

# Credentials. The trap here is that writing them on every boot would undo a
# password you changed from the panel UI -- the CLI overwrites the database
# unconditionally. So:
#
#   * no database yet (first boot, empty Volume): seed admin/admin, so the whole
#     service comes up with nothing configured anywhere.
#   * database exists: leave the credentials alone, so the password you set in
#     Panel Settings survives every restart and redeploy.
#
# There is deliberately no way in through the environment. Nothing about this
# panel is configurable from outside the panel, which also means there is no
# variable anyone could set to take it over.
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
if [ -f "$DB_FILE" ]; then
  APPLY_CREDS=0
  ADMIN_USERNAME="$(read_credential username)"
  ADMIN_PASSWORD="$(read_credential password)"
  CREDS_NOTE="kept from the panel database"
else
  APPLY_CREDS=1
  DEFAULT_CREDS=1
  CREDS_NOTE="seeded on first boot"
fi
export ADMIN_USERNAME ADMIN_PASSWORD APPLY_CREDS
"$XUI_BIN" setting -port "$PANEL_PORT"          >/dev/null
"$XUI_BIN" setting -webBasePath "$PANEL_PATH"   >/dev/null
"$XUI_BIN" setting -listenIP 127.0.0.1          >/dev/null
if [ "$APPLY_CREDS" = "1" ]; then
  "$XUI_BIN" setting -username "$ADMIN_USERNAME" -password "$ADMIN_PASSWORD" >/dev/null
fi

# remarkTemplate has no CLI flag, so it goes straight into the settings table.
# It is what every share link and subscription entry is named after, which is
# where the branding the clients actually see comes from. Written before the
# panel starts so the first render already uses it. Only overwrite when the
# stored value is still the upstream default or empty, so a template someone
# tuned by hand in Panel Settings is not clobbered on every restart.
REMARK_DEFAULT='{{INBOUND}}-{{EMAIL}}|📊{{TRAFFIC_LEFT}}|⏳{{DAYS_LEFT}}D'
python3 - "$REMARK_TEMPLATE" "$REMARK_DEFAULT" "$REMARK_LEGACY" <<'PY'
import sqlite3, sys
want, upstream, legacy = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    row = con.execute("SELECT value FROM settings WHERE key='remarkTemplate'").fetchone()
    cur = row[0] if row else ""
    if cur and cur not in (upstream, legacy):
        print(f"[entrypoint] remarkTemplate already customised; leaving {cur!r}")
    else:
        if row:
            con.execute("UPDATE settings SET value=? WHERE key='remarkTemplate'", (want,))
        else:
            con.execute("INSERT INTO settings(key,value) VALUES('remarkTemplate',?)", (want,))
        con.commit()
        print(f"[entrypoint] remarkTemplate set to {want!r}")
except Exception as e:
    print(f"[entrypoint] WARNING: could not set remarkTemplate: {e}")
PY

# subTitle and subAnnounce are the two slots on 3x-ui's own subscription page:
# the title at the top of the card and the announce box under it. Same rule as
# remarkTemplate -- write them when unset or still empty, never over something
# you typed into Panel Settings yourself.
python3 - "$SUB_TITLE" "$SUB_ANNOUNCE" "$SUB_SUPPORT_URL" "$BRAND" <<'PY'
import sqlite3, sys
values = {"subTitle": sys.argv[1], "subAnnounce": sys.argv[2],
          "subSupportUrl": sys.argv[3]}
# The plain name this project wrote before is ours to replace; anything else in
# the slot was typed in Panel Settings and is left alone.
legacy = {"subTitle": {sys.argv[4]}}
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    for key, want in values.items():
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        cur = row[0] if row else ""
        if cur and cur not in legacy.get(key, ()):
            print(f"[entrypoint] {key} already set; leaving {cur!r}")
            continue
        if row:
            con.execute("UPDATE settings SET value=? WHERE key=?", (want, key))
        else:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, want))
        print(f"[entrypoint] {key} set to {want!r}")
    con.commit()
except Exception as e:
    print(f"[entrypoint] WARNING: could not set the subscription page text: {e}")
PY

# The address every generated config points at. This is the one setting that has
# to be right or nothing connects at all: left alone, links carry the *internal*
# port of the inbound and no TLS, which is unreachable from outside.
#
# Two ways it gets written. The bootstrap below sets it through the panel API,
# but the bootstrap can only log in when the credentials are known, and on a
# redeploy where they come from the database it steps aside. That is exactly the
# boot on which the address becomes known, because Railway injects
# RAILWAY_PUBLIC_DOMAIN only *after* a domain exists -- so the deploy that gives
# the panel its name is the one that would otherwise leave the inbound pointing
# at an address nothing answers on. Written straight into the database instead,
# which needs no login.
#
# Idempotent, and it only touches its own entry: an address configured by hand in
# the panel (a different domain, a CDN) is left exactly as it is.
if [ -n "$PUBLIC_HOST" ]; then
  python3 - "$PUBLIC_HOST" "$PUBLIC_PORT" "$PUBLIC_TLS" "$INBOUND_PORT" \
           "$BRAND" "$SUB_PATH" <<'PUBADDR_PY'
import json, sqlite3, sys
host, port, force_tls = sys.argv[1], int(sys.argv[2]), sys.argv[3]
in_port, remark, sub_path = int(sys.argv[4]), sys.argv[5], sys.argv[6]

# externalProxy is the row that rewrites the address, port and TLS in every
# generated link and in the subscription.
entry = {"forceTls": force_tls, "dest": host, "port": port,
         "remark": remark, "sni": host}
# subURI is what the panel builds the link on its Clients page from. Left empty it
# reads http://<domain>:2096/..., a port Railway does not expose.
sub_uri = f"https://{host}{sub_path}"

try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    for iid, ss in con.execute(
            "SELECT id, stream_settings FROM inbounds WHERE port=?", (in_port,)):
        try:
            obj = json.loads(ss)
        except Exception:
            continue
        proxies = obj.get("externalProxy") or []
        if proxies and not any(isinstance(p, dict) and p.get("remark") == remark
                               for p in proxies):
            print(f"[entrypoint] inbound {iid} has an address set by hand; "
                  f"leaving it alone")
            continue
        if proxies == [entry]:
            continue
        obj["externalProxy"] = [entry]
        con.execute("UPDATE inbounds SET stream_settings=? WHERE id=?",
                    (json.dumps(obj, ensure_ascii=False), iid))
        print(f"[entrypoint] inbound {iid} now answers on {host}:{port} ({force_tls})")

    # 3.10 keeps the public address in its own `hosts` table as well, and that
    # row is what the subscription actually builds links from once it is there.
    # A row left pointing at the previous domain keeps handing out an address
    # that no longer answers, so it moves with the domain instead of sitting
    # still. Only rows that already name an address are touched, and only for the
    # inbound this entrypoint manages -- anything else is someone's own work.
    ids = [r[0] for r in con.execute(
        "SELECT id FROM inbounds WHERE port=?", (in_port,))]
    moved = 0
    if ids:
        placeholders = ",".join("?" * len(ids))
        for hid, addr in con.execute(
                f"SELECT id, address FROM hosts WHERE inbound_id IN ({placeholders})",
                ids):
            if not addr or addr == host:
                continue
            con.execute("UPDATE hosts SET address=?, sni=?, port=?, security=? "
                        "WHERE id=?", (host, host, port, force_tls, hid))
            moved += 1
    if moved:
        print(f"[entrypoint] moved {moved} subscription address(es) to {host}")

    row = con.execute("SELECT value FROM settings WHERE key='subURI'").fetchone()
    cur = row[0] if row else ""
    if cur and ":2096" not in cur and not cur.startswith("http://"):
        print(f"[entrypoint] subURI already set to {cur}; leaving it alone")
    elif cur != sub_uri:
        if row:
            con.execute("UPDATE settings SET value=? WHERE key='subURI'", (sub_uri,))
        else:
            con.execute("INSERT INTO settings(key,value) VALUES('subURI',?)", (sub_uri,))
        print(f"[entrypoint] subURI set to {sub_uri}")
    con.commit()
except Exception as e:
    print(f"[entrypoint] WARNING: could not set the public address: {e}")
PUBADDR_PY
fi

# Existing deployments carry the old default name. The bootstrap below can only
# log in on the very first boot, so a panel that has been running for a while
# would keep the old label for ever. So rename it here instead, right in the
# database, unconditionally and idempotently, before the panel reads the table.
#
# Only a stored value that is exactly the old default is touched. A name you
# typed yourself in the panel is yours and survives every restart.
python3 - "$BRAND" <<'PY'
import json, sqlite3, sys
brand = sys.argv[1]
OLD = "railway"
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    rows = con.execute("SELECT id, remark, stream_settings FROM inbounds").fetchall()
    renamed = 0
    for iid, remark, ss in rows:
        if remark == OLD:
            con.execute("UPDATE inbounds SET remark=? WHERE id=?", (brand, iid))
            renamed += 1
        # The same old label is also stored inside streamSettings, under
        # externalProxy -- that is the row the panel shows in its Hosts view.
        if ss:
            try:
                obj = json.loads(ss)
            except Exception:
                continue
            proxies = obj.get("externalProxy")
            if isinstance(proxies, list):
                dirty = False
                for p in proxies:
                    if isinstance(p, dict) and p.get("remark") == OLD:
                        p["remark"] = brand
                        dirty = True
                if dirty:
                    con.execute("UPDATE inbounds SET stream_settings=? WHERE id=?",
                                (json.dumps(obj, ensure_ascii=False), iid))
                    renamed += 1
    if renamed:
        con.commit()
        print(f"[entrypoint] renamed the old '{OLD}' label to '{brand}' "
              f"in {renamed} place(s)")
except Exception as e:
    print(f"[entrypoint] WARNING: could not rename the old inbound label: {e}")
PY

# The shared subscription id used to default to "mobile", which is also what the
# link on everyone's phone says (/sub/mobile). Renaming the default alone would
# strand every client created before this change: their sub_id would still read
# "mobile" while the link handed out said "PABLO", so the page would come up
# empty and the app would report no servers.
#
# So move the existing clients across too. Only a sub_id that is exactly the old
# default is touched -- anyone who put their own subId on a client keeps it.
python3 - "$CLIENT_SUBID" <<'PY'
import sqlite3, sys
want = sys.argv[1]
OLD = "mobile"
if want == OLD:
    sys.exit(0)
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    rows = con.execute("SELECT email FROM clients WHERE sub_id=?", (OLD,)).fetchall()
    if rows:
        con.execute("UPDATE clients SET sub_id=? WHERE sub_id=?", (want, OLD))
        con.commit()
        names = ", ".join(r[0] for r in rows)
        print(f"[entrypoint] moved {len(rows)} client(s) from subId '{OLD}' to "
              f"'{want}': {names}. The subscription link is now /sub/{want}; "
              f"the old /sub/{OLD} no longer resolves, so re-add the link on "
              f"any phone still using it.")
except Exception as e:
    print(f"[entrypoint] WARNING: could not move the old subId: {e}")
PY

# A client whose expiryTime is stored in seconds rather than milliseconds reads
# as a date in 1970, so it shows as expired the moment it is created. The panel
# form sends milliseconds, so this should never happen -- but if it ever does,
# say so in the log rather than leaving a client that silently looks dead.
python3 - <<'PYCHK'
import sqlite3, time
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    for email, sub_id, exp in con.execute(
            "SELECT email, sub_id, expiry_time FROM clients"):
        # expiry_time is milliseconds. Anything positive but far too small to be
        # a millisecond timestamp is a date entered as seconds, which the panel
        # reads as January 1970 and therefore shows as long expired.
        if exp and 0 < exp < 10**11:
            print("[entrypoint] WARNING: client %r (subId %r) has expiry_time=%s."
                  " That is a date in seconds, but this field is milliseconds,"
                  " so the panel reads it as %s and shows the client as expired."
                  " Open the client in the panel and set the expiry again, or"
                  " set it to 0 for unlimited."
                  % (email, sub_id, exp,
                     time.strftime("%Y-%m-%d", time.gmtime(exp / 1000.0))))
except Exception as e:
    print("[entrypoint] WARNING: could not check client expiry values: %s" % e)
PYCHK

log "panel configured: port ${PANEL_PORT}, base path ${PANEL_PATH}; credentials ${CREDS_NOTE}"

# --- 2/3/4. panel + inbound --------------------------------------------------
start_panel() {
  ( cd "$XUI_DIR" && exec "$XUI_BIN" run ) &
  PANEL_PID=$!
  log "panel started (pid ${PANEL_PID})"
}

stop_panel() {
  if [ -n "${PANEL_PID:-}" ] && kill -0 "$PANEL_PID" 2>/dev/null; then
    log "stopping panel so Xray picks up the inbound"
    kill -TERM "$PANEL_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$PANEL_PID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$PANEL_PID" 2>/dev/null || true
    wait "$PANEL_PID" 2>/dev/null || true
  fi
  PANEL_PID=""
}

start_panel
write_bootstrap
rm -f "${RUNTIME_DIR}/bootstrap.env"
"${RUNTIME_DIR}/bootstrap.sh" || log "WARNING: bootstrap did not complete; the panel is still usable"

if [ -s "${RUNTIME_DIR}/bootstrap.env" ]; then
  # shellcheck disable=SC1091
  . "${RUNTIME_DIR}/bootstrap.env"
  # A freshly created or updated inbound only reaches Xray on a relaunch.
  stop_panel
  start_panel
  sleep 3
fi

# --- 5. nginx ----------------------------------------------------------------
# Bootstrap is skipped on later boots (no stored password to log in with), so
# read the panel's own subPath from the database rather than assuming /subs/.
if [ -z "${SUB_REAL_PATH:-}" ]; then
  SUB_REAL_PATH="$(python3 - <<'PY'
import sqlite3
try:
    con = sqlite3.connect("/etc/x-ui/x-ui.db")
    row = con.execute("SELECT value FROM settings WHERE key='subPath'").fetchone()
    print(row[0] if row and row[0] else "/subs/")
except Exception:
    print("/subs/")
PY
)"
fi
SUB_REAL_PATH="${SUB_REAL_PATH:-/subs/}"
# The viewer page is served at the bare /sub (no trailing slash), while the
# prefix location below keeps the slash form the proxy rewrite expects.
SUB_PATH_NOSLASH="${SUB_PATH%/}"
[ -n "$SUB_PATH_NOSLASH" ] || SUB_PATH_NOSLASH="/"
RENDERED="${RUNTIME_DIR}/nginx.conf"
sed -e "s|@@HTTP_PORT@@|${HTTP_PORT}|g" \
    -e "s|@@PANEL_PORT@@|${PANEL_PORT}|g" \
    -e "s|@@PANEL_PATH@@|${PANEL_PATH}|g" \
    -e "s|@@SUB_PORT@@|${SUB_PORT}|g" \
    -e "s|@@SUB_PATH@@|${SUB_PATH}|g" \
    -e "s|@@SUB_PATH_NOSLASH@@|${SUB_PATH_NOSLASH}|g" \
    -e "s|@@SUB_REAL_PATH@@|${SUB_REAL_PATH}|g" \
    -e "s|@@INBOUND_PORT@@|${INBOUND_PORT}|g" \
    -e "s|@@TUNNEL_PATH@@|${TUNNEL_PATH}|g" \
    -e "s|@@SUBPAGE_PORT@@|${SUBPAGE_PORT}|g" \
    -e "s|@@BRAND@@|${BRAND}|g" \
    /opt/nginx.conf.template > "$RENDERED"

# --- 4b. subscription page ----------------------------------------------------
# Renders /sub. Needs the panel's real subPath to reach the sub server, so it
# starts after bootstrap has handed it back.
SUBPAGE_PID=""
if [ -f /opt/subserver.py ]; then
  SUBPAGE_PORT="$SUBPAGE_PORT" \
  SUB_PORT="$SUB_PORT" \
  SUB_REAL_PATH="$SUB_REAL_PATH" \
  SUB_PATH="$SUB_PATH" \
  CLIENT_SUBID="$CLIENT_SUBID" \
    python3 /opt/subserver.py &
  SUBPAGE_PID=$!
  log "subscription page started (pid ${SUBPAGE_PID}) on :${SUBPAGE_PORT}"
fi

nginx -t -c "$RENDERED" || { log "FATAL: rendered nginx config is invalid"; exit 1; }
nginx -c "$RENDERED" || { log "FATAL: nginx would not start on :${HTTP_PORT}"; stop_panel; exit 1; }
# nginx daemonises; give it a moment and confirm it is really serving before
# announcing success, so a bind failure surfaces here instead of as a 502.
for _ in $(seq 1 10); do
  curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:${HTTP_PORT}/healthz" && break
  sleep 1
done
log "nginx listening on :${HTTP_PORT} inside the container"
log "the panel is reached at https://${PUBLIC_HOST:-<your-domain>}/panel/ -- no port on the end"

# --- report ------------------------------------------------------------------
HOST_FOR_LINKS="${PUBLIC_HOST:-<your-domain>}"
PANEL_URL="https://${HOST_FOR_LINKS}${PANEL_PATH}"
SUB_URL="https://${HOST_FOR_LINKS}${SUB_PATH}${CLIENT_SUBID}"

echo
echo "=============================================================="
echo "  3x-ui on Railway — ready"
echo "=============================================================="
echo "  Panel          ${PANEL_URL}"
echo "  Username       ${ADMIN_USERNAME}"
if [ "$APPLY_CREDS" = "1" ]; then
  echo "  Password       ${ADMIN_PASSWORD}   (${CREDS_NOTE})"
fi
if [ -n "${DEFAULT_CREDS:-}" ]; then
  echo
  echo "  !! Still on admin/admin. Change it in Panel Settings -> the change"
  echo "     sticks across restarts, nothing here overwrites it."
fi
echo "  Subscription   ${SUB_URL}"
echo "                 ^ Open it in a browser: 3x-ui's own subscription page."
echo "                   Paste it into a VPN app: the same address hands the"
echo "                   app the raw feed. The panel decides by who is asking."
echo "                   Title: ${SUB_TITLE}"
echo "                 ^ give every new client subId \"${CLIENT_SUBID}\" and it"
echo "                   appears in this same link automatically. No new link,"
echo "                   no new inbound, nothing to hand out per person."
if [ -n "${CLIENT_LINK:-}" ]; then
  echo
  echo "  Client config:"
  echo "  ${CLIENT_LINK}"
fi
echo
echo "  Transport      vless + ${TRANSPORT} on path ${TUNNEL_PATH}"
echo "  Internal       panel :${PANEL_PORT}, sub :${SUB_PORT}, inbound :${INBOUND_PORT}"
if [ -z "$PUBLIC_HOST" ]; then
  echo
  echo "  !! No public domain detected. Links above contain <your-domain>."
  echo "     Railway only injects RAILWAY_PUBLIC_DOMAIN after you press"
  echo "     'Generate Domain', and it appears on the NEXT deploy. Either"
  echo "     Generate Domain then Redeploy, or set PUBLIC_HOST explicitly."
fi
echo "=============================================================="
echo

# --- supervise ---------------------------------------------------------------
trap 'stop_panel; [ -n "$SUBPAGE_PID" ] && kill "$SUBPAGE_PID" 2>/dev/null; nginx -s quit 2>/dev/null || true; exit 0' TERM INT

while true; do
  if ! kill -0 "$PANEL_PID" 2>/dev/null; then
    log "panel exited; shutting down so Railway restarts the container"
    nginx -s quit 2>/dev/null || true
    exit 1
  fi
  if ! pgrep -x nginx >/dev/null 2>&1; then
    log "nginx exited; shutting down so Railway restarts the container"
    stop_panel
    exit 1
  fi
  # The subscription page is not fatal: without it the raw /sub/<id> endpoint
  # still works, so restart it rather than taking the whole service down.
  if [ -n "$SUBPAGE_PID" ] && ! kill -0 "$SUBPAGE_PID" 2>/dev/null; then
    log "subscription page exited; restarting it"
    SUBPAGE_PORT="$SUBPAGE_PORT" SUB_PORT="$SUB_PORT" \
    SUB_REAL_PATH="$SUB_REAL_PATH" SUB_PATH="$SUB_PATH" \
    CLIENT_SUBID="$CLIENT_SUBID" \
      python3 /opt/subserver.py &
    SUBPAGE_PID=$!
  fi
  sleep 5
done
