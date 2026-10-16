#!/usr/bin/env python3
"""Keep PABLO's managed Railway endpoint complete and reachable.

3x-ui stores a share endpoint in the legacy externalProxy block, the v3.10
Hosts table, and the public subscription URI shown by the panel. Railway exposes
none of Xray's internal ports; every public config must use the actual request
host on port 443 with TLS, SNI, WebSocket Host and path all aligned.

Only an inbound on ``inbound_port`` that is identifiable by the ``brand``
remark (on the inbound or one of its Hosts) is managed. Custom Hosts are left
alone. The generated brand Host follows Railway domain changes only while it
still matches the exact automatic baseline; editing it in the panel transfers
ownership to the operator, so later panel/sub requests cannot undo the Save.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import secrets
import sqlite3
import string
import time
import urllib.parse
from dataclasses import dataclass


@dataclass
class SyncResult:
    host: str
    settings: int = 0
    inbounds: int = 0
    hosts: int = 0
    hosts_created: int = 0
    hosts_preserved: int = 0

    @property
    def changed(self) -> int:
        return self.settings + self.inbounds + self.hosts + self.hosts_created


# This marker records the exact values last written by the automatic Railway
# synchronizer. If the panel later stores different values, that difference is
# an operator edit and must win. Keeping the marker in SQLite makes the choice
# survive both panel requests and container restarts (when /etc/x-ui is a
# Railway Volume).
HOST_STATE_KEY = "pabloManagedHostStateV1"


def _load_host_state(con: sqlite3.Connection) -> dict:
    row = con.execute("SELECT value FROM settings WHERE key=?", (HOST_STATE_KEY,)).fetchone()
    if not row or not row[0]:
        return {"version": 1, "inbounds": {}}
    try:
        state = json.loads(row[0])
    except (TypeError, ValueError):
        return {"version": 1, "inbounds": {}}
    if not isinstance(state, dict) or not isinstance(state.get("inbounds"), dict):
        return {"version": 1, "inbounds": {}}
    return state


def _host_signature(row: tuple) -> dict:
    """Return only fields the Railway synchronizer is allowed to own."""
    return {
        "address": row[3] or "",
        "port": int(row[4] or 0),
        "security": row[5] or "",
        "sni": row[6] or "",
        "hostHeader": row[7] or "",
        "path": row[8] or "",
        "allowInsecure": bool(row[9]),
        "isDisabled": bool(row[10]),
        "isHidden": bool(row[11]),
        "overrideSniFromAddress": bool(row[12]),
        "keepSniBlank": bool(row[13]),
    }


def _desired_host_signature(host: str, port: int, security: str, path: str) -> dict:
    return {
        "address": host,
        "port": int(port),
        "security": security,
        "sni": host,
        "hostHeader": host,
        "path": path,
        "allowInsecure": False,
        "isDisabled": False,
        "isHidden": False,
        "overrideSniFromAddress": False,
        "keepSniBlank": False,
    }


def normalize_public_host(value: str | None) -> str | None:
    """Return a safe DNS hostname (without a port), or ``None``."""
    value = (value or "").strip()
    if not value or any(ch in value for ch in "/\\@?#"):
        return None
    try:
        host = urllib.parse.urlsplit("//" + value).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if host == "localhost" or "." not in host or len(host) > 253:
        return None
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        return None
    return host


def _group_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _upsert_setting(con: sqlite3.Connection, key: str, value: str) -> int:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row[0] == value:
        return 0
    if row:
        con.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
    else:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    return 1


def _managed_stream(raw: str, host: str, port: int, security: str,
                    brand: str, tunnel_path: str) -> tuple[str, bool]:
    """Update only PABLO's externalProxy entry in one streamSettings JSON."""
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return raw, False
    if not isinstance(obj, dict):
        return raw, False

    proxies = obj.get("externalProxy")
    if not isinstance(proxies, list):
        proxies = []

    managed = [
        i for i, item in enumerate(proxies)
        if isinstance(item, dict) and item.get("remark") == brand
    ]

    endpoint = {
        "forceTls": security,
        "dest": host,
        "port": port,
        "remark": brand,
        "sni": host,
    }
    if managed:
        # The brand label is reserved for the one PABLO-managed Railway
        # endpoint. Keep the oldest occurrence (including any extra fields an
        # upgrade added) and collapse stale duplicates so subscriptions do not
        # emit the same server more than once.
        keep = managed[0]
        updated = dict(proxies[keep])
        updated.update(endpoint)
        proxies[keep] = updated
        duplicate_indexes = set(managed[1:])
        proxies = [
            item for index, item in enumerate(proxies)
            if index not in duplicate_indexes
        ]
    else:
        proxies.append(endpoint)
    obj["externalProxy"] = proxies

    # This is also the transport data used by direct share links. Keeping it in
    # step prevents an old WS Host/path from surviving beside a correct Host row.
    ws = obj.get("wsSettings")
    if not isinstance(ws, dict):
        ws = {}
    ws["path"] = tunnel_path
    ws["host"] = host
    headers = ws.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    headers["Host"] = host
    ws["headers"] = headers
    obj["wsSettings"] = ws

    cooked = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return cooked, cooked != raw


def sync_public_endpoint(
    db_path: str,
    public_host: str,
    inbound_port: int = 10085,
    public_port: int = 443,
    security: str = "tls",
    brand: str = "PABLO",
    sub_path: str = "/sub/",
    tunnel_path: str = "/ws",
) -> SyncResult:
    """Synchronize PABLO's managed endpoint in one SQLite transaction."""
    host = normalize_public_host(public_host)
    if not host:
        raise ValueError("invalid public hostname")
    if public_port != 443 or security != "tls":
        raise ValueError("Railway's public endpoint must be port 443 with TLS")
    public_paths = {"subURI": sub_path}
    if not sub_path.startswith("/") or not sub_path.endswith("/"):
        raise ValueError("subscription path must start and end with '/'")
    if not tunnel_path.startswith("/"):
        raise ValueError("tunnel_path must start with '/'")

    result = SyncResult(host=host)
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("BEGIN IMMEDIATE")
        for key, path in public_paths.items():
            result.settings += _upsert_setting(
                con, key, f"https://{host}{path}"
            )

        rows = con.execute(
            "SELECT i.id, i.stream_settings FROM inbounds i "
            "WHERE i.port=? AND (i.remark=? OR EXISTS ("
            "SELECT 1 FROM hosts h WHERE h.inbound_id=i.id AND h.remark=?))",
            (int(inbound_port), brand, brand),
        ).fetchall()
        host_state = _load_host_state(con)
        inbound_states = host_state.setdefault("inbounds", {})
        now_ms = int(time.time() * 1000)
        desired_signature = _desired_host_signature(
            host, public_port, security, tunnel_path
        )

        for inbound_id, raw in rows:
            host_rows = con.execute(
                "SELECT id, group_id, remark, address, port, security, sni, "
                "host_header, path, allow_insecure, is_disabled, is_hidden, "
                "override_sni_from_address, keep_sni_blank "
                "FROM hosts WHERE inbound_id=? ORDER BY id",
                (inbound_id,),
            ).fetchall()
            managed_rows = [row for row in host_rows if row[2] == brand]
            state_key = str(inbound_id)
            saved = inbound_states.get(state_key)
            automatic = False

            if managed_rows:
                # Exactly one row owns the reserved brand. Retain the oldest
                # one and remove only same-brand duplicates; custom rows are
                # never candidates for deletion.
                row = managed_rows[0]
                group_id = row[1]
                current_signature = _host_signature(row)

                # A row is automatic only while it still equals the exact
                # baseline last written by us. A successful Hosts-page edit
                # changes that signature, so it becomes operator-owned and is
                # never replaced by the next panel/sub request.
                if (
                    isinstance(saved, dict)
                    and saved.get("groupId") == group_id
                ):
                    automatic = (
                        saved.get("mode") == "auto"
                        and saved.get("signature") == current_signature
                    )
                elif saved is None:
                    # Upgrade path from releases that did not persist a marker:
                    # the reserved PABLO row was always wrapper-owned in
                    # those versions, so repair it once and establish a precise
                    # baseline before an operator can open the Hosts page.
                    automatic = True

                if automatic:
                    if current_signature != desired_signature:
                        con.execute(
                            "UPDATE hosts SET address=?, port=?, security=?, sni=?, "
                            "host_header=?, path=?, allow_insecure=0, "
                            "is_disabled=0, is_hidden=0, "
                            "override_sni_from_address=0, keep_sni_blank=0, "
                            "updated_at=? WHERE id=?",
                            (
                                host, public_port, security, host, host,
                                tunnel_path, now_ms, row[0],
                            ),
                        )
                        result.hosts += 1
                    inbound_states[state_key] = {
                        "groupId": group_id,
                        "mode": "auto",
                        "signature": desired_signature,
                    }
                else:
                    inbound_states[state_key] = {
                        "groupId": group_id,
                        "mode": "manual",
                        "signature": current_signature,
                    }
                    result.hosts_preserved += 1

                duplicate_ids = [duplicate[0] for duplicate in managed_rows[1:]]
                if duplicate_ids:
                    marks = ",".join("?" for _ in duplicate_ids)
                    con.execute(
                        f"DELETE FROM hosts WHERE id IN ({marks})",
                        duplicate_ids,
                    )
                    result.hosts += len(duplicate_ids)
            else:
                # A fresh API-created inbound normally gets a Host
                # automatically. If it is missing, add one without changing
                # any custom Host rows.
                group_id = _group_id()
                con.execute(
                    "INSERT INTO hosts (group_id,inbound_id,sort_order,remark,"
                    "is_disabled,is_hidden,address,port,security,sni,host_header,"
                    "path,override_sni_from_address,keep_sni_blank,allow_insecure,"
                    "created_at,updated_at) VALUES (?,?,0,?,0,0,?,?,?,?,?,? ,0,0,0,?,?)",
                    (
                        group_id, inbound_id, brand, host, public_port,
                        security, host, host, tunnel_path, now_ms, now_ms,
                    ),
                )
                result.hosts_created += 1
                automatic = True
                inbound_states[state_key] = {
                    "groupId": group_id,
                    "mode": "auto",
                    "signature": desired_signature,
                }

            # Keep the legacy direct-link projection synchronized only while
            # the PABLO Host remains automatic. Once the operator edits that
            # Host, both the normalized row and the legacy projection are left
            # under panel control.
            if automatic:
                cooked, changed = _managed_stream(
                    raw, host, public_port, security, brand, tunnel_path
                )
                if changed:
                    con.execute(
                        "UPDATE inbounds SET stream_settings=? WHERE id=?",
                        (cooked, inbound_id),
                    )
                    result.inbounds += 1

        if rows:
            result.settings += _upsert_setting(
                con,
                HOST_STATE_KEY,
                json.dumps(host_state, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":")),
            )

        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--db", default="/etc/x-ui/x-ui.db")
    parser.add_argument("--inbound-port", type=int, default=10085)
    parser.add_argument("--brand", default="PABLO")
    parser.add_argument("--sub-path", default="/sub/")
    parser.add_argument("--tunnel-path", default="/ws")
    args = parser.parse_args()
    result = sync_public_endpoint(
        args.db,
        args.host,
        inbound_port=args.inbound_port,
        brand=args.brand,
        sub_path=args.sub_path,
        tunnel_path=args.tunnel_path,
    )
    print(
        "[public-endpoint] https://%s:443 synchronized "
        "(settings=%d, inbounds=%d, hosts=%d, hosts_created=%d, "
        "hosts_preserved=%d)"
        % (
            result.host,
            result.settings,
            result.inbounds,
            result.hosts,
            result.hosts_created,
            result.hosts_preserved,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
