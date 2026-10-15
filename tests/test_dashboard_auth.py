import json
import unittest
from unittest import mock

import aiohttp

from tests.dashboard_support import SECRET, DashboardTestCase, FakeClock

from src.utils.dashboard import auth
from src.utils.dashboard.auth import LoginThrottle


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.throttle = LoginThrottle(clock=self.clock)

    def fail(self, n, key="1.2.3.4"):
        results = [self.throttle.record_failure(key) for _ in range(n)]
        return results

    def test_first_four_failures_report_remaining_attempts(self):
        self.assertEqual(self.fail(4), [(4, 0), (3, 0), (2, 0), (1, 0)])
        self.assertEqual(self.throttle.retry_after("1.2.3.4"), 0)

    def test_fifth_failure_locks_for_90_seconds(self):
        results = self.fail(5)
        self.assertEqual(results[-1], (0, 90))
        self.assertEqual(self.throttle.retry_after("1.2.3.4"), 90)
        self.clock.advance(30)
        self.assertEqual(self.throttle.retry_after("1.2.3.4"), 60)
        self.clock.advance(60)
        self.assertEqual(self.throttle.retry_after("1.2.3.4"), 0)

    def test_lockouts_double_and_each_cycle_allows_five_attempts(self):
        durations = []
        for _ in range(4):
            results = self.fail(5)
            durations.append(results[-1][1])
            self.clock.advance(durations[-1])
            self.assertEqual(self.throttle.retry_after("1.2.3.4"), 0)
        self.assertEqual(durations, [90, 180, 360, 720])

    def test_lockout_is_capped_at_one_day(self):
        last = 0
        for _ in range(14):
            last = self.fail(5)[-1][1]
            self.clock.advance(last)
        self.assertEqual(last, 86400)

    def test_success_clears_state(self):
        self.fail(5)
        self.clock.advance(90)
        self.fail(3)
        self.throttle.record_success("1.2.3.4")
        self.assertEqual(self.fail(5)[-1], (0, 90))

    def test_clients_are_tracked_independently(self):
        self.fail(5, key="a")
        self.assertEqual(self.throttle.retry_after("b"), 0)
        self.assertEqual(self.throttle.record_failure("b"), (4, 0))

    def test_state_is_forgotten_after_a_day_without_failures(self):
        self.fail(5)
        self.clock.advance(90)
        self.assertEqual(self.fail(5)[-1], (0, 180))
        self.clock.advance(180 + 86400)
        self.assertEqual(self.fail(5)[-1], (0, 90))

    def test_stale_entries_are_pruned(self):
        for i in range(50):
            self.throttle.record_failure(f"10.0.0.{i}")
        self.assertEqual(len(self.throttle), 50)
        self.clock.advance(86400 + 120)
        self.throttle.retry_after("other")
        self.assertEqual(len(self.throttle), 0)


class LoginEndpointTests(DashboardTestCase):
    async def test_login_success_sets_cookie_without_exposing_token(self):
        resp = await self.client.post("/api/auth/login", json={"secret": SECRET})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body, {"status": "ok"})
        cookie = resp.cookies["session_token"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertFalse(cookie["secure"])
        self.assertNotIn(cookie.value, json.dumps(body))
        self.assertGreaterEqual(len(cookie.value), 40)

    async def test_session_tokens_are_random_and_unrelated_to_secret(self):
        first = (await self.login()).cookies["session_token"].value
        second = (await self.login()).cookies["session_token"].value
        self.assertNotEqual(first, second)
        self.assertNotIn(SECRET, first)

    async def test_login_failure_reports_remaining_attempts(self):
        resp = await self.client.post("/api/auth/login", json={"secret": "wrong"})
        self.assertEqual(resp.status, 401)
        self.assertEqual(await resp.json(), {"error": "Invalid secret", "attempts_remaining": 4})
        self.assertNotIn("session_token", resp.cookies)

    async def test_login_rejects_bad_bodies(self):
        resp = await self.client.post("/api/auth/login", data="not json")
        self.assertEqual(resp.status, 400)
        resp = await self.client.post("/api/auth/login", json={"secret": 123})
        self.assertEqual(resp.status, 401)

    async def test_lockout_over_http_does_not_check_secret_or_extend_lock(self):
        clock = FakeClock()
        self.server.login_throttle = LoginThrottle(clock=clock)

        for expected in (4, 3, 2, 1):
            resp = await self.client.post("/api/auth/login", json={"secret": "bad"})
            self.assertEqual((await resp.json())["attempts_remaining"], expected)

        resp = await self.client.post("/api/auth/login", json={"secret": "bad"})
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers["Retry-After"], "90")
        self.assertEqual(await resp.json(), {"error": "Too many failed attempts", "retry_after": 90})

        clock.advance(30)
        with mock.patch("src.utils.dashboard.server.verify_secret") as verify:
            resp = await self.client.post("/api/auth/login", json={"secret": SECRET})
            verify.assert_not_called()
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers["Retry-After"], "60")

        clock.advance(60)
        resp = await self.client.post("/api/auth/login", json={"secret": SECRET})
        self.assertEqual(resp.status, 200)

    async def test_second_lockout_doubles_and_success_resets(self):
        clock = FakeClock()
        self.server.login_throttle = LoginThrottle(clock=clock)

        for _ in range(5):
            resp = await self.client.post("/api/auth/login", json={"secret": "bad"})
        clock.advance(90)
        for _ in range(4):
            resp = await self.client.post("/api/auth/login", json={"secret": "bad"})
            self.assertEqual(resp.status, 401)
        resp = await self.client.post("/api/auth/login", json={"secret": "bad"})
        self.assertEqual((resp.status, (await resp.json())["retry_after"]), (429, 180))

        clock.advance(180)
        await self.login()
        for _ in range(4):
            await self.client.post("/api/auth/login", json={"secret": "bad"})
        resp = await self.client.post("/api/auth/login", json={"secret": "bad"})
        self.assertEqual((await resp.json())["retry_after"], 90)


class SessionTests(DashboardTestCase):
    async def test_api_requires_session(self):
        for method, path in (("get", "/api/overview"), ("get", "/api/settings"),
                             ("post", "/api/diagnostics/prune"), ("get", "/api/search?q=ab"),
                             ("post", "/api/commands/sync"), ("get", "/api/logs/errors")):
            resp = await getattr(self.client, method)(path)
            self.assertEqual(resp.status, 401, path)
            self.assertEqual(await resp.json(), {"error": "Unauthorized"})

    async def test_authorization_header_is_not_accepted(self):
        token = (await self.login()).cookies["session_token"].value
        self.client.session.cookie_jar.clear()
        resp = await self.client.get("/api/overview", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status, 401)

    async def test_auth_check_never_returns_token(self):
        resp = await self.client.get("/api/auth/check")
        self.assertEqual(await resp.json(), {"authenticated": False})
        await self.login()
        resp = await self.client.get("/api/auth/check")
        self.assertEqual(await resp.json(), {"authenticated": True})

    async def test_session_expires(self):
        await self.login()
        resp = await self.client.get("/api/settings")
        self.assertEqual(resp.status, 200)

        real_now = auth._now()
        with mock.patch.object(auth, "_now", lambda: real_now + auth.SESSION_DURATION + 1):
            resp = await self.client.get("/api/settings")
            self.assertEqual(resp.status, 401)
            resp = await self.client.get("/api/auth/check")
            self.assertEqual(await resp.json(), {"authenticated": False})

    async def test_expired_sessions_are_cleaned_up_on_login(self):
        auth._active_sessions["stale"] = auth._now() - 5
        await self.login()
        self.assertNotIn("stale", auth._active_sessions)

    async def test_logout_invalidates_session(self):
        await self.login()
        resp = await self.client.post("/api/auth/logout")
        self.assertEqual(resp.status, 200)
        self.assertEqual(auth._active_sessions, {})
        resp = await self.client.get("/api/settings")
        self.assertEqual(resp.status, 401)


class WebSocketTests(DashboardTestCase):
    async def test_rejected_without_session_cookie(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
            await self.client.ws_connect("/ws", headers={"Origin": self.origin})
        self.assertEqual(ctx.exception.status, 401)

    async def test_rejected_with_wrong_or_missing_origin(self):
        await self.login()
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {}):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
                await self.client.ws_connect("/ws", headers=headers)
            self.assertEqual(ctx.exception.status, 403, headers)

    async def test_accepted_with_cookie_and_matching_origin(self):
        await self.login()
        ws = await self.client.ws_connect("/ws", headers={"Origin": self.origin})
        msg = await ws.receive_json()
        self.assertEqual(msg["type"], "connected")
        self.assertEqual(self.server.ws_client_count, 1)

        await ws.send_json({"type": "ping"})
        self.assertEqual((await ws.receive_json())["type"], "pong")

        resp = await self.client.put("/api/settings", json={"max_channel_history": 20})
        self.assertEqual(resp.status, 200)
        event = await ws.receive_json()
        self.assertEqual((event["type"], event["fields"]), ("settings_updated", ["max_channel_history"]))
        await ws.close()

    async def test_no_token_message_protocol(self):
        await self.login()
        ws = await self.client.ws_connect("/ws", headers={"Origin": self.origin})
        await ws.receive_json()
        await ws.send_json({"token": "anything"})
        await ws.send_json({"type": "ping"})
        self.assertEqual((await ws.receive_json())["type"], "pong")
        await ws.close()

    async def test_socket_closed_with_4401_when_session_expires(self):
        await self.login()
        ws = await self.client.ws_connect("/ws", headers={"Origin": self.origin})
        await ws.receive_json()

        real_now = auth._now()
        with mock.patch.object(auth, "_now", lambda: real_now + auth.SESSION_DURATION + 1):
            await ws.send_json({"type": "ping"})
            msg = await ws.receive()
        self.assertEqual(msg.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(msg.data, 4401)

    async def test_logout_closes_sockets_with_4401(self):
        await self.login()
        ws = await self.client.ws_connect("/ws", headers={"Origin": self.origin})
        await ws.receive_json()
        await self.client.post("/api/auth/logout")
        msg = await ws.receive()
        self.assertEqual(msg.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(msg.data, 4401)


class SecurityHeaderTests(DashboardTestCase):
    def assert_common_headers(self, resp):
        host = f"{self.client.server.host}:{self.client.server.port}"
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn(f"connect-src 'self' ws://{host} wss://{host}", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Strict-Transport-Security", resp.headers)

    async def test_headers_on_page_and_api_responses(self):
        resp = await self.client.get("/")
        self.assert_common_headers(resp)
        self.assertNotIn("no-store", resp.headers.get("Cache-Control", ""))

        resp = await self.client.get("/api/auth/check")
        self.assert_common_headers(resp)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

    async def test_headers_on_error_responses(self):
        resp = await self.client.get("/api/overview")
        self.assertEqual(resp.status, 401)
        self.assert_common_headers(resp)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

        resp = await self.client.get("/definitely-missing")
        self.assertEqual(resp.status, 404)
        self.assert_common_headers(resp)

    async def test_headers_on_streamed_download(self):
        await self.login()
        resp = await self.client.get("/api/backup/database")
        self.assertEqual(resp.status, 200)
        await resp.read()
        self.assert_common_headers(resp)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

    async def test_unsafe_host_header_is_not_reflected_into_csp(self):
        resp = await self.client.get("/", headers={"Host": "evil.example; script-src *"})
        self.assertNotIn("evil", resp.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
