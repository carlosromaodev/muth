import asyncio
import json
import unittest
from unittest.mock import patch

from muth.capture_security import CaptureRateLimiter
from muth.config import Settings
from muth.errors import MuthError
from muth.main import create_app


class CaptureUploadDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(
            _env_file=None,
            engine_mode="demo",
            max_inference_requests=1,
            request_body_timeout_seconds=0.02,
        )
        self.app = create_app(self.settings)
        self.addCleanup(self.app.state.database.engine.dispose)
        self.now = self.app.state.store.clock()
        self.app.state.store.clock = lambda: self.now

    def scope(self, path, headers):
        return {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"origin", b"http://testserver"),
                (b"sec-fetch-site", b"same-origin"),
                *headers,
            ],
            "server": ("testserver", 80),
            "client": ("198.51.100.17", 5000),
        }

    async def response(self, scope, receive):
        messages = []

        async def send(message):
            messages.append(message)

        # A missing application deadline must fail the test instead of hanging the suite.
        await asyncio.wait_for(self.app(scope, receive, send), timeout=2)
        status = next(message["status"] for message in messages if "status" in message)
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return status, json.loads(body)

    async def create_session(self):
        body = json.dumps(
            {
                "consent": {
                    "accepted": True,
                    "purpose": "onboarding",
                    "policy_version": self.settings.capture_policy_version,
                },
                "device_group": "web",
            }
        ).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        status, session = await self.response(
            self.scope(
                "/capture-api/sessions",
                [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            ),
            receive,
        )
        self.assertEqual(status, 201)
        return session

    def verify_scope(self, session):
        return self.scope(
            f"/capture-api/sessions/{session['session_id']}/verify",
            [
                (b"authorization", f"Bearer {session['capture_token']}".encode()),
                (b"idempotency-key", b"capture-deadline-001"),
                (b"content-type", b"multipart/form-data; boundary=capture-test"),
            ],
        )

    def assert_capacity_released(self):
        capacity, registry = self.app.state.capacity, self.app.state.metrics.registry
        self.assertEqual(capacity.requests, 0)
        self.assertEqual(capacity.workers, 0)
        self.assertEqual(registry.get_sample_value("muth_inference_requests_inflight"), 0)
        self.assertEqual(registry.get_sample_value("muth_inference_workers_active"), 0)
        self.assertTrue(capacity.acquire_request())
        capacity.release_request()

    async def test_stalled_authenticated_upload_times_out_and_releases_admission(self):
        session = await self.create_session()
        cancelled = asyncio.Event()

        async def stalled_receive():
            self.assertEqual(self.app.state.capacity.requests, 1)
            self.assertEqual(self.app.state.capacity.workers, 0)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        status, response = await self.response(self.verify_scope(session), stalled_receive)
        self.assertEqual(status, 408)
        self.assertEqual(response["error"]["code"], "request_body_timeout")
        self.assertTrue(cancelled.is_set())
        self.assert_capacity_released()

    async def test_capture_expiring_during_upload_is_rejected_before_multipart_parsing(self):
        session = await self.create_session()

        async def receive():
            self.assertEqual(self.app.state.capacity.requests, 1)
            self.now += self.settings.session_ttl_seconds + 1
            return {
                "type": "http.request",
                "body": b"--capture-test--\r\n",
                "more_body": False,
            }

        with patch("muth.capture_api.Request.form") as parser:
            status, response = await self.response(self.verify_scope(session), receive)
        self.assertEqual(status, 410)
        self.assertEqual(response["error"]["code"], "session_expired")
        parser.assert_not_called()
        self.assert_capacity_released()

    async def test_capability_expiring_during_upload_is_rejected_before_multipart_parsing(self):
        session = await self.create_session()

        async def receive():
            self.now += self.settings.capture_retention_days * 86400 + 1
            return {"type": "http.request", "body": b"--capture-test--\r\n", "more_body": False}

        with patch("muth.capture_api.Request.form") as parser:
            status, response = await self.response(self.verify_scope(session), receive)
        self.assertEqual(status, 410)
        self.assertEqual(response["error"]["code"], "capture_token_expired")
        parser.assert_not_called()
        self.assert_capacity_released()


class CaptureRateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0

    def limiter(self, **options):
        settings = Settings(_env_file=None, **options)
        return CaptureRateLimiter(settings, b"independent-test-purpose-key", clock=lambda: self.now)

    def assert_rate_limited(self, limiter, address, **options):
        with self.assertRaises(MuthError) as rejected:
            limiter.check(address, **options)
        self.assertEqual(rejected.exception.status, 429)
        self.assertEqual(rejected.exception.code, "rate_limit_exceeded")

    def test_creation_global_ceiling_cannot_be_bypassed_by_rotating_addresses(self):
        limiter = self.limiter(capture_creation_limit_per_minute=1)
        for suffix in range(4):
            limiter.check(f"192.0.2.{suffix}", creation=True)
        self.assert_rate_limited(limiter, "192.0.2.99", creation=True)
        self.now = 60
        limiter.check("192.0.2.99", creation=True)
        self.assertNotIn("192.0.2.", repr(limiter.windows))

    def test_expired_windows_are_pruned_and_the_same_address_can_retry(self):
        limiter = self.limiter(capture_rate_limit_per_minute=1)
        limiter.check("203.0.113.10")
        first_window_keys = set(limiter.windows)
        self.assert_rate_limited(limiter, "203.0.113.10")
        self.now = 60
        limiter.check("203.0.113.10")
        self.assertNotIn("203.0.113.10", repr(limiter.windows))
        self.now = 120
        limiter.check("203.0.113.20")
        self.assertEqual(len(limiter.windows), 2)
        self.assertEqual(first_window_keys & set(limiter.windows), {"global:request"})

    def test_many_distinct_addresses_cannot_grow_rate_state_without_bound(self):
        limiter = self.limiter(capture_rate_limit_per_minute=1000)
        accepted, rejected = 0, 0
        for number in range(4096):
            address = f"198.51.{number // 256}.{number % 256}"
            try:
                limiter.check(address)
                accepted += 1
            except MuthError as error:
                self.assertEqual(error.status, 429)
                rejected += 1
        self.assertGreater(accepted, 0)
        self.assertGreater(rejected, 0)
        self.assertLessEqual(len(limiter.windows), 2048)
        self.assertNotIn("198.51.", repr(limiter.windows))
        self.now = 60
        limiter.check("198.51.100.250")
        self.assertEqual(len(limiter.windows), 2)
