import asyncio
import threading
import unittest
from contextlib import suppress
from unittest.mock import patch

import httpx

from muth.main import create_app
from tests.test_muth import png
from tests.test_sessions import PAYLOAD, settings


class SessionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(settings(max_inference_requests=1))
        self.addCleanup(self.app.state.database.engine.dispose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://muth-test",
            headers={"X-API-Key": "alpha-token"},
        )
        self.addAsyncCleanup(self.client.aclose)
        created = await self.client.post("/v1/sessions", json=PAYLOAD)
        self.assertEqual(created.status_code, 201)
        self.session_path = f"/v1/sessions/{created.json()['session_id']}"
        self.image = png()

    async def verify(self):
        return await self.client.post(
            f"{self.session_path}/verify",
            headers={"Idempotency-Key": "cancelled-attempt-123"},
            files={"document": self.image, "selfie": self.image},
        )

    async def wait_event(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 2), "Worker did not reach test barrier")

    async def wait_workers_idle(self):
        for _ in range(200):
            if self.app.state.capacity.workers == 0:
                return
            await asyncio.sleep(0.01)
        self.fail("Cancelled worker did not release capacity after finishing")

    async def assert_released_session(self):
        response = await self.client.get(self.session_path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "awaiting_capture")

    async def test_cancel_during_engine_releases_claim_but_retains_native_worker_capacity(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.app.state.engines.liveness.assess

        def blocked_assessment(image):
            started.set()
            try:
                if not release.wait(timeout=5):
                    raise RuntimeError("Test assessment barrier timed out")
                return original(image)
            finally:
                finished.set()

        task = None
        try:
            with patch.object(
                self.app.state.engines.liveness, "assess", side_effect=blocked_assessment
            ) as assess:
                task = asyncio.create_task(self.verify())
                await self.wait_event(started)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.assert_released_session()
                self.assertEqual(self.app.state.capacity.requests, 0)
                self.assertEqual(self.app.state.capacity.workers, 1)

                # The orphaned native worker still limits admission, despite its released claim.
                busy = await self.verify()
                self.assertEqual(busy.status_code, 429)
                self.assertEqual(busy.json()["error"]["code"], "inference_capacity_exceeded")
                self.assertEqual(assess.call_count, 1)

                release.set()
                await self.wait_event(finished)
                await self.wait_workers_idle()
                retry = await self.verify()
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(assess.call_count, 2)
                replay = await self.verify()
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json(), retry.json())
                self.assertEqual(assess.call_count, 2)
                self.assertEqual(self.app.state.capacity.requests, 0)
                self.assertEqual(self.app.state.capacity.workers, 0)
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self.wait_workers_idle()

    async def test_cancel_while_claim_is_blocked_recovers_attempt_and_allows_retry(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        original_claim = self.app.state.store.claim
        original_assess = self.app.state.engines.liveness.assess

        def blocked_claim(*args):
            started.set()
            try:
                if not release.wait(timeout=5):
                    raise RuntimeError("Test claim barrier timed out")
                return original_claim(*args)
            finally:
                finished.set()

        task = None
        try:
            with (
                patch.object(self.app.state.store, "claim", side_effect=blocked_claim),
                patch.object(
                    self.app.state.engines.liveness, "assess", wraps=original_assess
                ) as assess,
            ):
                task = asyncio.create_task(self.verify())
                await self.wait_event(started)
                task.cancel()
                # Cancellation cleanup must wait for the preserved claim task to yield its token.
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.wait_event(finished)
                await self.assert_released_session()
                assess.assert_not_called()
                self.assertEqual(self.app.state.capacity.requests, 0)
                self.assertEqual(self.app.state.capacity.workers, 0)

                retry = await self.verify()
                self.assertEqual(retry.status_code, 200)
                self.assertEqual(assess.call_count, 1)
                replay = await self.verify()
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json(), retry.json())
                self.assertEqual(assess.call_count, 1)
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self.wait_workers_idle()
