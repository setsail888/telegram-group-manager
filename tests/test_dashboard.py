"""Live local HTTP tests; no Telegram API or public network required."""
import http.client
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.dashboard import create_server, dashboard_db, cookie_value
from app.store import Store

TOKEN = "0123456789abcdef" * 4


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "readonly.sqlite")
        self.store = Store(self.path)
        self.store.ensure_chat(-100, '<img src=x onerror="alert(1)">测试群')
        self.store.stat(-100, "messages", 10)
        self.store.set(-100, "points_enabled", True)
        self.store.change_points(-100, 777, 20, 1)
        self.server = create_server(
            "127.0.0.1", 0, TOKEN, self.path, ZoneInfo("Asia/Shanghai"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, *, cookie=None, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if host:
            headers["Host"] = host
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        result = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return result

    def login(self):
        status, headers, _ = self.request(
            "POST", "/login", body=urlencode({"token": TOKEN}))
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_default_requires_authentication(self):
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login")
        status, _, _ = self.request("GET", "/group?id=-100")
        self.assertEqual(status, 303)

    def test_login_rejects_wrong_token(self):
        status, _, body = self.request(
            "POST", "/login", body=urlencode({"token": "not-the-key"}))
        self.assertEqual(status, 401)
        self.assertIn("密钥无效", body)
        self.assertNotIn(TOKEN, body)

    def test_login_has_basic_bruteforce_limit(self):
        for _ in range(5):
            self.assertEqual(self.request(
                "POST", "/login", body=urlencode({"token": "wrong"}))[0], 401)
        self.assertEqual(self.request(
            "POST", "/login", body=urlencode({"token": TOKEN}))[0], 429)

    def test_overview_and_group_escape_untrusted_titles(self):
        cookie = self.login()
        status, headers, html = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", html)
        self.assertNotIn('<img src=x onerror="alert(1)">', html)
        self.assertNotIn(TOKEN, html)
        status, _, group = self.request(
            "GET", "/group?id=-100", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("10", group)
        self.assertIn("积分排行榜", group)
        self.assertIn("777", group)
        self.assertIn("积分商城", group)
        self.assertIn("免费抽奖", group)

    def test_no_mutation_routes(self):
        cookie = self.login()
        status, _, _ = self.request(
            "POST", "/group?id=-100", cookie=cookie, body="enabled=0")
        self.assertEqual(status, 405)
        self.assertEqual(self.store.point_balance(-100, 777)["balance"], 20)
        self.assertTrue(self.store.settings(-100)["points_enabled"])

    def test_no_sql_injection_through_query_string(self):
        cookie = self.login()
        status, _, _ = self.request(
            "GET", "/group?id=-100%20OR%201=1", cookie=cookie)
        self.assertEqual(status, 400)

    def test_forbids_unexpected_host(self):
        status, _, _ = self.request(
            "GET", "/login", host="attacker.example")
        self.assertEqual(status, 403)

    def test_logout_invalidates_cookie_in_browser(self):
        cookie = self.login()
        status, headers, _ = self.request(
            "POST", "/logout", cookie=cookie, body="")
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_db_is_query_only(self):
        db = dashboard_db(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM chats")
        finally:
            db.close()
        self.assertIsNotNone(self.store.chat(-100))

    def test_missing_database_does_not_leak_path(self):
        self.server.db_path = "/nonexistent/private/location.sqlite"
        status, _, content = self.request("GET", "/", cookie=self.login())
        self.assertEqual(status, 503)
        self.assertNotIn("/nonexistent", content)

    def test_short_token_rejected_before_binding(self):
        with self.assertRaises(ValueError):
            create_server("127.0.0.1", 0, "unsafe", self.path, ZoneInfo("Asia/Shanghai"))

    def test_signed_cookie_enforces_server_side_expiration(self):
        expired = "groupguard_session=" + cookie_value(TOKEN, int(time.time()) - 60)
        future = "groupguard_session=" + cookie_value(TOKEN, int(time.time()) + 86400)
        for candidate in (expired, future, "groupguard_session=untrusted"):
            with self.subTest(candidate=candidate[:25]):
                status, _, _ = self.request("GET", "/", cookie=candidate)
                self.assertEqual(status, 303)


if __name__ == "__main__":
    unittest.main()
