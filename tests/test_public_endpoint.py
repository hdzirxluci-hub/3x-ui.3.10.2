import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from public_endpoint import HOST_STATE_KEY, sync_public_endpoint  # noqa: E402


HOST_COLUMNS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT,
    inbound_id INTEGER NOT NULL,
    sort_order INTEGER DEFAULT 0,
    remark TEXT,
    server_description TEXT,
    is_disabled NUMERIC DEFAULT 0,
    is_hidden NUMERIC DEFAULT 0,
    tags TEXT,
    address TEXT,
    port INTEGER DEFAULT 0,
    security TEXT DEFAULT 'same',
    sni TEXT,
    host_header TEXT,
    path TEXT,
    alpn TEXT,
    fingerprint TEXT,
    cipher_suites TEXT,
    override_sni_from_address NUMERIC DEFAULT 0,
    keep_sni_blank NUMERIC DEFAULT 0,
    pinned_peer_cert_sha256 TEXT,
    verify_peer_cert_by_name TEXT,
    allow_insecure NUMERIC DEFAULT 0,
    ech_config_list TEXT,
    mux_params TEXT,
    sockopt_params TEXT,
    final_mask TEXT,
    vless_route TEXT,
    exclude_from_sub_types TEXT,
    mihomo_ip_version TEXT,
    mihomo_x25519 NUMERIC,
    shuffle_host NUMERIC,
    node_guids TEXT,
    created_at INTEGER,
    updated_at INTEGER
"""


def stream(host="old.up.railway.app"):
    return json.dumps({
        "network": "ws",
        "security": "none",
        "externalProxy": [
            {"forceTls": "tls", "dest": host, "port": 443,
             "remark": "PABLO", "sni": host},
            {"forceTls": "tls", "dest": "cdn.example.com", "port": 8443,
             "remark": "MY-CDN", "sni": "cdn.example.com"},
        ],
        "wsSettings": {"path": "/ws", "host": host,
                       "headers": {"Host": host}},
    })


class PublicEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "x-ui.db")
        con = sqlite3.connect(self.db)
        con.executescript(f"""
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT
            );
            CREATE TABLE inbounds (
                id INTEGER PRIMARY KEY,
                port INTEGER,
                remark TEXT,
                stream_settings TEXT
            );
            CREATE TABLE hosts ({HOST_COLUMNS});
        """)
        con.execute(
            "INSERT INTO inbounds(id,port,remark,stream_settings) VALUES(1,10085,'PABLO',?)",
            (stream(),),
        )
        con.execute(
            "INSERT INTO hosts(group_id,inbound_id,sort_order,remark,address,port,security,"
            "sni,host_header,path,allow_insecure,is_disabled,is_hidden,"
            "override_sni_from_address,keep_sni_blank,created_at,updated_at) "
            "VALUES('pablo-group',1,0,'PABLO','old.up.railway.app',443,'tls',"
            "'old.up.railway.app','old.up.railway.app','/ws',0,0,0,0,0,1,1)"
        )
        con.execute(
            "INSERT INTO hosts(group_id,inbound_id,sort_order,remark,address,port,security,"
            "sni,host_header,path,allow_insecure,is_disabled,is_hidden,"
            "override_sni_from_address,keep_sni_blank,created_at,updated_at) "
            "VALUES('custom-group',1,1,'MY-CDN','cdn.example.com',8443,'tls',"
            "'sni.example.com','header.example.com','/custom',1,1,1,1,1,1,1)"
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, remark):
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT address,port,security,sni,host_header,path,allow_insecure,"
            "is_disabled,is_hidden,override_sni_from_address,keep_sni_blank "
            "FROM hosts WHERE inbound_id=1 AND remark=? ORDER BY id LIMIT 1",
            (remark,),
        ).fetchone()
        con.close()
        return row

    def test_automatic_host_tracks_domain_and_custom_host_is_untouched(self):
        first = sync_public_endpoint(self.db, "first.up.railway.app")
        self.assertEqual(first.hosts, 1)
        self.assertEqual(
            self.row("PABLO"),
            ("first.up.railway.app", 443, "tls", "first.up.railway.app",
             "first.up.railway.app", "/ws", 0, 0, 0, 0, 0),
        )
        custom_before = self.row("MY-CDN")
        second = sync_public_endpoint(self.db, "second.up.railway.app")
        self.assertEqual(second.hosts, 1)
        self.assertEqual(self.row("PABLO")[0], "second.up.railway.app")
        self.assertEqual(self.row("MY-CDN"), custom_before)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT value FROM settings WHERE key='subURI'").fetchone()[0], "https://second.up.railway.app/sub/")
        obj = json.loads(con.execute("SELECT stream_settings FROM inbounds WHERE id=1").fetchone()[0])
        con.close()
        self.assertEqual([p for p in obj["externalProxy"] if p.get("remark") == "PABLO"][0]["dest"], "second.up.railway.app")
        self.assertEqual([p for p in obj["externalProxy"] if p.get("remark") == "MY-CDN"][0]["dest"], "cdn.example.com")

    def test_hosts_page_edit_survives_later_syncs_and_restart_state(self):
        sync_public_endpoint(self.db, "first.up.railway.app")
        manual = ("manual.edge.example", 8443, "tls", "manual.sni.example", "manual.host.example", "/manual", 1, 1, 1, 1, 1)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE hosts SET address=?,port=?,security=?,sni=?,host_header=?,path=?,allow_insecure=?,is_disabled=?,is_hidden=?,override_sni_from_address=?,keep_sni_blank=? WHERE remark='PABLO'", manual)
        con.commit(); con.close()
        result = sync_public_endpoint(self.db, "second.up.railway.app")
        self.assertEqual(result.hosts_preserved, 1)
        self.assertEqual(self.row("PABLO"), manual)
        result = sync_public_endpoint(self.db, "third.up.railway.app")
        self.assertEqual(result.hosts_preserved, 1)
        self.assertEqual(self.row("PABLO"), manual)
        con = sqlite3.connect(self.db)
        state = json.loads(con.execute("SELECT value FROM settings WHERE key=?", (HOST_STATE_KEY,)).fetchone()[0])
        external = json.loads(con.execute("SELECT stream_settings FROM inbounds WHERE id=1").fetchone()[0])["externalProxy"]
        con.close()
        self.assertEqual(state["inbounds"]["1"]["mode"], "manual")
        self.assertEqual([p for p in external if p.get("remark") == "PABLO"][0]["dest"], "first.up.railway.app")

    def test_deleting_reserved_row_resets_only_the_managed_host(self):
        sync_public_endpoint(self.db, "first.up.railway.app")
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM hosts WHERE inbound_id=1 AND remark='PABLO'")
        con.commit(); con.close()
        custom_before = self.row("MY-CDN")
        result = sync_public_endpoint(self.db, "reset.up.railway.app")
        self.assertEqual(result.hosts_created, 1)
        self.assertEqual(self.row("PABLO")[0], "reset.up.railway.app")
        self.assertEqual(self.row("MY-CDN"), custom_before)

    def test_legacy_no_marker_row_gets_one_automatic_baseline(self):
        result = sync_public_endpoint(self.db, "upgrade.up.railway.app")
        self.assertEqual(result.hosts, 1)
        self.assertEqual(self.row("PABLO")[0], "upgrade.up.railway.app")
        con = sqlite3.connect(self.db)
        state = json.loads(con.execute("SELECT value FROM settings WHERE key=?", (HOST_STATE_KEY,)).fetchone()[0])
        con.close()
        self.assertEqual(state["inbounds"]["1"]["mode"], "auto")

    def test_unrelated_inbound_on_same_port_is_not_claimed(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO inbounds(id,port,remark,stream_settings) VALUES(2,10085,'OTHER',?)", (stream("other.example.com").replace('PABLO', 'OTHER'),))
        con.execute("INSERT INTO hosts(group_id,inbound_id,remark,address,port,security,sni,host_header,path) VALUES('other-group',2,'OTHER','other.example.com',9443,'tls','other-sni.example.com','other-host.example.com','/other')")
        con.commit()
        before = con.execute("SELECT stream_settings FROM inbounds WHERE id=2").fetchone()[0]
        con.close()
        sync_public_endpoint(self.db, "public.up.railway.app")
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT address,port,sni,host_header,path FROM hosts WHERE inbound_id=2").fetchone()
        after = con.execute("SELECT stream_settings FROM inbounds WHERE id=2").fetchone()[0]
        con.close()
        self.assertEqual(row, ("other.example.com", 9443, "other-sni.example.com", "other-host.example.com", "/other"))
        self.assertEqual(after, before)

    def test_invalid_or_non_edge_endpoint_is_rejected(self):
        for host in ("localhost", "127.0.0.1", "bad host", "https://example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                sync_public_endpoint(self.db, host)
        with self.assertRaises(ValueError):
            sync_public_endpoint(self.db, "valid.example.com", public_port=80)
        with self.assertRaises(ValueError):
            sync_public_endpoint(self.db, "valid.example.com", security="none")


if __name__ == "__main__":
    unittest.main()
