import asyncio
import json
import threading
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from muth.api import invoke_engine
from muth.capacity import InferenceCapacity
from muth.main import create_app
from muth.media import decode_image, read_image
from muth.middleware import Metrics, PlatformMiddleware
from tests.test_muth import png
from tests.test_sessions import settings


class MiddlewareAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = settings(max_inference_requests=1)
        self.app = create_app(self.config)
        self.addCleanup(self.app.state.database.engine.dispose)
        self.metrics = Metrics()

        async def application(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        self.middleware = PlatformMiddleware(
            application, settings=self.config, metrics=self.metrics
        )

    def scope(self, path="/v1/liveness", headers=()):
        return {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": [(b"x-api-key", b"alpha-token"), *headers],
            "app": self.app,
        }

    async def response(self, scope, receive):
        messages = []

        async def send(message):
            messages.append(message)

        await self.middleware(scope, receive, send)
        return messages

    async def test_busy_request_rejected_before_read_and_released_after_cancel(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def stalled_receive():
            started.set()
            await release.wait()
            return {"type": "http.request", "body": b"{}"}

        first = asyncio.create_task(self.response(self.scope(), stalled_receive))
        await started.wait()

        async def unread_receive():
            self.fail("Busy request body must remain unread")

        for path in ["/v1/liveness", "/v1/faces/compare", "/v1/sessions/opaque-session/verify"]:
            result = await self.response(self.scope(path), unread_receive)
            self.assertEqual(result[0]["status"], 429)
            self.assertEqual(dict(result[0]["headers"])[b"retry-after"], b"1")
            self.assertEqual(
                json.loads(result[1]["body"])["error"]["code"], "inference_capacity_exceeded"
            )
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(self.middleware.capacity.requests, 0)
        release.set()
        self.assertEqual((await self.response(self.scope(), stalled_receive))[0]["status"], 200)

    async def test_body_limits_and_ambiguous_length(self):
        async def unread_receive():
            self.fail("Declared oversized/ambiguous bodies must remain unread")

        for headers, expected in [
            ([(b"content-length", b"100000")], 413),
            ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
            ([(b"content-length", b"1"), (b"transfer-encoding", b"chunked")], 400),
        ]:
            result = await self.response(self.scope("/health", headers), unread_receive)
            self.assertEqual(result[0]["status"], expected)

        async def receive():
            return {"type": "http.request", "body": b"123"}

        result = await self.response(self.scope(headers=[(b"content-length", b"2")]), receive)
        self.assertEqual(result[0]["status"], 400)
        self.assertEqual(self.middleware.capacity.requests, 0)

    async def test_disconnect_releases_admission(self):
        async def receive():
            return {"type": "http.disconnect"}

        self.assertEqual(await self.response(self.scope(), receive), [])
        self.assertEqual(self.middleware.capacity.requests, 0)

    async def test_worker_remains_bounded_after_http_task_cancelled(self):
        capacity = InferenceCapacity(1, self.metrics)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(capacity=capacity)),
            state=SimpleNamespace(request_id="opaque-test-request"),
        )
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def operation():
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test worker timed out")
            return "finished"

        def worker():
            try:
                return invoke_engine(request, operation)
            finally:
                finished.set()

        self.assertTrue(capacity.acquire_request())
        task = asyncio.create_task(run_in_threadpool(worker))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            capacity.release_request()
            self.assertFalse(capacity.acquire_request())
            self.assertEqual(capacity.workers, 1)
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertTrue(capacity.acquire_request())
        capacity.release_request()
        self.assertEqual(capacity.workers, 0)

    async def test_image_decode_retains_capacity_after_http_cancellation(self):
        capacity = InferenceCapacity(1, self.metrics)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def stalled_decode(content, config):
            started.set()
            try:
                if not release.wait(timeout=5):
                    raise RuntimeError("Test decoder timed out")
                return decode_image(content, config)
            finally:
                finished.set()

        async def application(scope, receive, send):
            await receive()
            await read_image(UploadFile(BytesIO(png())), self.config, capacity=capacity)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        self.middleware = PlatformMiddleware(
            application, settings=self.config, metrics=self.metrics, capacity=capacity
        )

        async def receive():
            return {"type": "http.request", "body": b"{}"}

        async def unread_receive():
            self.fail("A decoder that survived cancellation must still occupy capacity")

        with patch("muth.media.decode_image", side_effect=stalled_decode):
            task = asyncio.create_task(self.response(self.scope(), receive))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(capacity.requests, 0)
                self.assertEqual(capacity.workers, 1)
                self.assertEqual(
                    (await self.response(self.scope(), unread_receive))[0]["status"], 429
                )
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(capacity.workers, 0)
        self.assertEqual((await self.response(self.scope(), receive))[0]["status"], 200)


class EngineFailureTests(unittest.TestCase):
    def test_standalone_failure_sanitized_capacity_released_and_retry_succeeds(self):
        app = create_app(settings(max_inference_requests=1))
        with TestClient(app, headers={"X-API-Key": "alpha-token"}) as client:
            with patch.object(
                app.state.engines.liveness, "assess", side_effect=RuntimeError("private-debug")
            ):
                response = client.post("/v1/liveness", files={"selfie": png()})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private-debug", response.text)
            self.assertEqual(response.json()["error"]["code"], "engine_failure")
            self.assertEqual(app.state.capacity.requests, 0)
            self.assertEqual(app.state.capacity.workers, 0)
            self.assertEqual(client.post("/v1/liveness", files={"selfie": png()}).status_code, 200)
            metrics = client.get("/metrics").text
            self.assertIn("muth_inference_workers_active 0.0", metrics)
            self.assertNotIn("alpha-token", metrics)
