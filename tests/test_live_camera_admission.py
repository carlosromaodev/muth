import asyncio
import json
import threading
import unittest
from unittest.mock import patch

import httpx

from muth.capture_security import CaptureRateLimiter
from muth.errors import MuthError
from muth.main import create_app
from muth.media import decode_image
from tests.test_live_camera_api import consent, preview
from tests.test_sessions import settings


class LiveCameraAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = settings(
            max_inference_requests=1, request_body_timeout_seconds=0.02, document_ocr_enabled=False
        )
        self.app = create_app(self.settings)
        self.addCleanup(self.app.state.database.engine.dispose)
        self.now = self.app.state.store.clock()
        self.app.state.store.clock = lambda: self.now

    def scope(self, path, headers=(), query=b""):
        return {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "root_path": "",
            "query_string": query,
            "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver"), *headers],
            "server": ("testserver", 80),
            "client": ("198.51.100.55", 5000),
        }

    async def response(self, scope, receive):
        messages = []

        async def send(message):
            messages.append(message)

        await asyncio.wait_for(self.app(scope, receive, send), 2)
        status = next(message["status"] for message in messages if "status" in message)
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return status, json.loads(body)

    async def create(self, **changes):
        body = json.dumps({"consent": consent(**changes)}).encode()

        async def receive():
            return {"type": "http.request", "body": body}

        status, session = await self.response(
            self.scope("/capture-api/sessions", [(b"content-type", b"application/json")]), receive
        )
        self.assertEqual(status, 201)
        return session

    def camera_scope(self, session, *, extra_headers=(), suffix=""):
        return self.scope(
            f"/capture-api/sessions/{session['session_id']}/camera-assessment{suffix}",
            [
                (b"authorization", f"Bearer {session['capture_token']}".encode()),
                (b"content-type", b"multipart/form-data; boundary=camera"),
                *extra_headers,
            ],
            b"target=document_front",
        )

    def encoded_camera(self, session):
        request = httpx.Request(
            "POST",
            "http://testserver",
            files={"frame": ("preview.png", preview(), "image/png")},
        )
        body = request.read()
        scope = self.camera_scope(session)
        scope["headers"] = [pair for pair in scope["headers"] if pair[0] != b"content-type"] + [
            (b"content-type", request.headers["content-type"].encode())
        ]

        async def receive():
            return {"type": "http.request", "body": body}

        return scope, receive

    async def assert_unread(self, scope, status, code):
        async def receive():
            self.fail("Rejected camera request must not read any image bytes")

        with patch("muth.capture_api.assess_camera") as analyze:
            actual_status, response = await self.response(scope, receive)
        self.assertEqual(actual_status, status)
        self.assertEqual(response["error"]["code"], code)
        analyze.assert_not_called()
        self.assertEqual(self.app.state.capacity.requests, 0)

    async def test_token_origin_consent_and_status_rejected_before_body(self):
        session = await self.create()
        scope = self.camera_scope(session)
        scope["headers"] = [pair for pair in scope["headers"] if pair[0] != b"authorization"]
        await self.assert_unread(scope, 401, "invalid_capture_token")
        scope = self.camera_scope(session)
        scope["headers"] = [
            (key, b"https://attacker.test" if key == b"origin" else value)
            for key, value in scope["headers"]
        ]
        await self.assert_unread(scope, 403, "capture_origin_denied")
        legacy = await self.create(camera_frames_opt_in=False, camera_policy_version=None)
        await self.assert_unread(self.camera_scope(legacy), 403, "camera_consent_required")
        self.now += self.settings.session_ttl_seconds + 1
        await self.assert_unread(self.camera_scope(session), 410, "session_expired")

    async def test_stale_policy_and_closed_session_rejected_before_body(self):
        session = await self.create()
        self.app.state.settings.live_camera_policy_version = "capture-camera-v2"
        await self.assert_unread(self.camera_scope(session), 403, "camera_consent_required")
        self.app.state.settings.live_camera_policy_version = "capture-camera-v1"
        from muth.storage import SessionRow

        with self.app.state.database.transaction() as db:
            db.get(SessionRow, session["session_id"]).status = "processing"
        await self.assert_unread(self.camera_scope(session), 409, "camera_session_inactive")

    async def test_oversized_declared_body_including_slash_rejected_before_read(self):
        session = await self.create()
        for suffix in ("", "/"):
            await self.assert_unread(
                self.camera_scope(
                    session,
                    suffix=suffix,
                    extra_headers=[(b"content-length", str(512 * 1024 + 8193).encode())],
                ),
                413,
                "request_too_large",
            )

    async def test_unbounded_length_body_is_limited_and_stalled_body_times_out(self):
        session = await self.create()

        async def oversized():
            return {"type": "http.request", "body": b"x" * (512 * 1024 + 8193)}

        status, result = await self.response(self.camera_scope(session), oversized)
        self.assertEqual(status, 413)
        self.assertEqual(result["error"]["code"], "request_too_large")

        async def stalled():
            self.assertEqual(self.app.state.capacity.requests, 1)
            await asyncio.Event().wait()

        status, result = await self.response(self.camera_scope(session), stalled)
        self.assertEqual(status, 408)
        self.assertEqual(result["error"]["code"], "request_body_timeout")
        self.assertEqual(self.app.state.capacity.requests, 0)
        self.assertEqual(self.app.state.capacity.workers, 0)

    async def test_expiry_during_upload_and_decode_stops_detector(self):
        session = await self.create()

        async def expiring_body():
            self.now += self.settings.session_ttl_seconds + 1
            return {"type": "http.request", "body": b"--camera--\r\n"}

        with patch("muth.capture_api.Request.form") as parse:
            status, result = await self.response(self.camera_scope(session), expiring_body)
        self.assertEqual(status, 410)
        self.assertEqual(result["error"]["code"], "session_expired")
        parse.assert_not_called()
        session = await self.create()
        scope, receive = self.encoded_camera(session)

        def expiring_decode(content, config):
            frame = decode_image(content, config)
            self.now += self.settings.session_ttl_seconds + 1
            return frame

        with (
            patch("muth.media.decode_image", side_effect=expiring_decode),
            patch("muth.capture_api.assess_camera") as analyze,
        ):
            status, result = await self.response(scope, receive)
        self.assertEqual(status, 410)
        self.assertEqual(result["error"]["code"], "session_expired")
        analyze.assert_not_called()
        self.assertEqual(self.app.state.capacity.requests, 0)
        self.assertEqual(self.app.state.capacity.workers, 0)

    async def test_capability_expiry_after_detection_discards_result(self):
        session = await self.create()
        scope, receive = self.encoded_camera(session)

        def expiring_detector(*args, **kwargs):
            self.now += self.settings.capture_retention_days * 86400 + 1
            from muth.live_camera import CameraAssessment

            return CameraAssessment(target="document_front", detector_ready=True, reasons=[])

        with patch("muth.capture_api.assess_camera", side_effect=expiring_detector):
            status, result = await self.response(scope, receive)
        self.assertEqual(status, 410)
        self.assertEqual(result["error"]["code"], "capture_token_expired")
        self.assertNotIn("detected", result)
        self.assertEqual(self.app.state.capacity.workers, 0)

    async def test_cancelled_detector_keeps_worker_admission_until_thread_finishes(self):
        session = await self.create()
        scope, receive = self.encoded_camera(session)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocking_detector(*args, **kwargs):
            started.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("test detector was not released")
            finally:
                finished.set()

        with patch("muth.capture_api.assess_camera", side_effect=blocking_detector):
            task = asyncio.create_task(self.response(scope, receive))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(self.app.state.capacity.requests, 0)
                self.assertEqual(self.app.state.capacity.workers, 1)
                await self.assert_unread(
                    self.camera_scope(session), 429, "inference_capacity_exceeded"
                )
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
        self.assertEqual(self.app.state.capacity.workers, 0)

    async def test_detection_deadline_discards_frame_but_releases_worker_only_after_completion(
        self,
    ):
        session = await self.create()
        scope, receive = self.encoded_camera(session)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_timeout = asyncio.timeout

        def blocking_detector(*args, **kwargs):
            started.set()
            try:
                if not release.wait(3):
                    raise RuntimeError("test detector was not released")
            finally:
                finished.set()

        with (
            patch("muth.capture_api.assess_camera", side_effect=blocking_detector),
            patch("muth.capture_api.asyncio.timeout", side_effect=lambda _: original_timeout(0.05)),
        ):
            try:
                status, result = await self.response(scope, receive)
                self.assertTrue(started.is_set())
                self.assertEqual(status, 408)
                self.assertEqual(result["error"]["code"], "camera_assessment_timeout")
                self.assertEqual(self.app.state.capacity.requests, 0)
                self.assertEqual(self.app.state.capacity.workers, 1)
                await self.assert_unread(
                    self.camera_scope(session), 429, "inference_capacity_exceeded"
                )
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 1))
        self.assertEqual(self.app.state.capacity.workers, 0)


class LiveCameraRateLimitTests(unittest.TestCase):
    def test_camera_does_not_spend_creation_or_final_verification_quota(self):
        limiter = CaptureRateLimiter(
            settings(
                live_camera_rate_limit_per_minute=2,
                capture_rate_limit_per_minute=1,
                capture_creation_limit_per_minute=1,
            ),
            b"camera-quota-test-key",
            clock=lambda: 0,
        )
        limiter.check("192.0.2.20", camera=True)
        limiter.check("192.0.2.20", camera=True)
        with self.assertRaises(MuthError):
            limiter.check("192.0.2.20", camera=True)
        limiter.check("192.0.2.20")
        limiter.check("192.0.2.20", creation=True)
        self.assertNotIn("192.0.2.20", repr(limiter.windows))

    def test_global_camera_budget_and_bounded_addresses_roll_over(self):
        now = 0
        limiter = CaptureRateLimiter(
            settings(live_camera_rate_limit_per_minute=120),
            b"global-camera-quota-test-key",
            clock=lambda: now,
        )
        for index in range(480):
            limiter.check(f"198.51.100.{index}", camera=True)
        with self.assertRaises(MuthError) as denied:
            limiter.check("192.0.2.200", camera=True)
        self.assertEqual(denied.exception.status, 429)
        self.assertLessEqual(len(limiter.windows), 2048)
        now = 60
        limiter.check("192.0.2.200", camera=True)
        self.assertEqual(len(limiter.windows), 2)


if __name__ == "__main__":
    unittest.main()
