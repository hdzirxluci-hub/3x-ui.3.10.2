import base64
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import subserver  # noqa: E402


class SubscriptionPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "x-ui.db")
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT
            );
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY,
                email TEXT,
                sub_id TEXT,
                total_gb INTEGER,
                expiry_time INTEGER,
                enable NUMERIC
            );
            CREATE TABLE client_traffics (
                id INTEGER PRIMARY KEY,
                email TEXT,
                up INTEGER,
                down INTEGER
            );
        """)
        con.execute(
            "INSERT INTO settings(key,value) VALUES('subAnnounce',?)",
            ("این پنل توسط Pablo ساخته شده",),
        )
        con.execute(
            "INSERT INTO clients(id,email,sub_id,total_gb,expiry_time,enable) "
            "VALUES(1,'zahra','PABLO',10737418240,1893456000000,1)"
        )
        con.execute(
            "INSERT INTO client_traffics(id,email,up,down) "
            "VALUES(1,'zahra',1073741824,2147483648)"
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_browser_detection_does_not_mistake_native_apps(self):
        self.assertTrue(subserver.is_browser_request(
            "Mozilla/5.0 Chrome/130", "text/html,application/xhtml+xml"
        ))
        self.assertTrue(subserver.is_browser_request("Mozilla/5.0", "*/*"))
        self.assertTrue(subserver.is_browser_request("curl/8", "*/*", "html=1"))
        self.assertFalse(subserver.is_browser_request(
            "Streisand/1.6.43 (iOS 18)", "*/*"
        ))
        self.assertFalse(subserver.is_browser_request(
            "v2rayNG/1.9", "application/json"
        ))

    def test_raw_feed_decoder_accepts_base64_and_plain_links(self):
        links = [
            "vless://uuid@manual.edge.example:8443?security=tls#PABLO",
            "trojan://secret@example.net:443?security=tls#two",
        ]
        payload = base64.b64encode(("\n".join(links) + "\n").encode())
        self.assertEqual(subserver.decode_subscription_links(payload), links)
        self.assertEqual(subserver.decode_subscription_links("\n".join(links).encode()), links)
        self.assertEqual(subserver.decode_subscription_links(b"not a feed"), [])

    def test_user_contract_uses_header_quota_and_client_identity(self):
        now = datetime.fromtimestamp(1800000000, timezone.utc)
        info = {"upload": 1024, "download": 2048, "total": 4096, "expire": 1893456000}
        user = subserver.subscription_user(self.db, "PABLO", info, now=now)
        self.assertEqual(user.username, "zahra")
        self.assertEqual(user.used_traffic, 3072)
        self.assertEqual(user.data_limit, 4096)
        self.assertEqual(user.expire.timestamp(), 1893456000)
        self.assertEqual(user.status.value, "active")
        info["download"] = 4096
        user = subserver.subscription_user(self.db, "PABLO", info, now=now)
        self.assertEqual(user.status.value, "limited")

    def test_negative_expiry_maps_to_pasarguard_on_hold_state(self):
        con = sqlite3.connect(self.db)
        con.execute("UPDATE clients SET expiry_time=-604800000 WHERE id=1")
        con.commit(); con.close()
        user = subserver.subscription_user(
            self.db, "PABLO",
            {"upload": 0, "download": 0, "total": 0, "expire": 1893456000},
        )
        self.assertEqual(user.status.value, "on_hold")
        self.assertEqual(user.on_hold_expire_duration, 604800)
        self.assertIsNone(user.expire)

    def test_pasarguard_template_renders_live_3xui_data_and_keeps_host(self):
        link = (
            "vless://uuid@manual.edge.example:8443?encryption=none&security=tls"
            "&type=ws&host=manual.host.example&sni=manual.sni.example"
            "&path=%2Fmanual#%F0%9D%97%A3%F0%9D%97%94%F0%9D%97%95%F0%9D%97%9F%F0%9D%97%A2%20%7C%20zahra"
        )
        payload = base64.b64encode((link + "\n").encode())
        headers = {
            "Subscription-Userinfo": "upload=1073741824; download=2147483648; total=10737418240; expire=1893456000",
            "Announce": "base64:" + base64.b64encode("این پنل توسط Pablo ساخته شده".encode()).decode(),
        }
        html = subserver.render_subscription(
            self.db, "/sub/PABLO", "pablo-panel.up.railway.app", headers, payload
        )
        self.assertIn("<title>𝗣𝗔𝗕𝗟𝗢 — اشتراک اختصاصی</title>", html)
        self.assertIn("zahra", html)
        self.assertIn("manual.edge.example:8443", html)
        self.assertIn("manual.host.example", html)
        self.assertIn("این پنل توسط Pablo ساخته شده", html)
        self.assertIn("v2rayng://install-sub?url=https%3A%2F%2Fpablo-panel.up.railway.app%2Fsub%2FPABLO", html)
        self.assertNotIn("head.replace(/@", html)
        self.assertNotIn("CHANGE-ME.up.railway.app", html)
        self.assertNotIn("{%", html)
        self.assertNotIn("{{", html)

    def test_public_and_private_path_mapping(self):
        self.assertEqual(subserver.to_upstream("/sub/PABLO"), "/subs/PABLO")
        self.assertEqual(subserver.to_upstream("/sub"), "/subs/PABLO")
        self.assertEqual(subserver.subscription_id("/sub/zahra%2D1"), "zahra-1")
        self.assertEqual(subserver._raw_query("html=1&view=html&_=42"), "_=42")


if __name__ == "__main__":
    unittest.main()
