"""Shutdown must drain synchronous SQLite workers before disposing their pool."""

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from muth.config import Settings
from muth.engines.bundle import EngineBundle
from muth.main import _background_call, create_app


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for_event(self, event):
        async def poll():
            while not event.is_set():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(poll(), timeout=3)

    async def test_shutdown_waits_for_each_retention_worker_before_dispose(self):
        for service_name in ("capture_store", "capture_learning", "identities"):
            with self.subTest(service=service_name):
                app = create_app(
                    Settings(_env_file=None, engine_mode="demo", document_ocr_enabled=False)
                )
                service = getattr(app.state, service_name)
                original = service.purge
                started, release, finished = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )

                def blocked(started=started, release=release, original=original, finished=finished):
                    started.set()
                    try:
                        if not release.wait(timeout=5):
                            raise RuntimeError("Worker was never released")
                        return original()
                    finally:
                        finished.set()

                async def application_lifetime(app=app, started=started):
                    async with app.router.lifespan_context(app):
                        await self.wait_for_event(started)

                with (
                    patch.object(service, "purge", blocked),
                    patch.object(app.state.database.engine, "dispose") as dispose,
                ):
                    lifetime = asyncio.create_task(application_lifetime())
                    try:
                        await self.wait_for_event(started)
                        await asyncio.sleep(0.03)
                        self.assertFalse(lifetime.done())
                        dispose.assert_not_called()
                    finally:
                        release.set()
                        await asyncio.wait_for(lifetime, timeout=3)
                    self.assertTrue(finished.is_set())
                    dispose.assert_called_once_with()
                app.state.database.engine.dispose()

    async def test_shutdown_waits_for_refinement_worker_before_dispose(self):
        settings = Settings(
            _env_file=None,
            engine_mode="biometric",
            biometric_manifest="unused-test-manifest.json",
            document_ocr_enabled=False,
        )
        settings.learning_interval_seconds = 0.01
        runtime = SimpleNamespace(bundle=EngineBundle.demo)
        with patch("muth.main.BiometricRuntime", return_value=runtime):
            app = create_app(settings)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked(tenant):
            started.set()
            try:
                if not release.wait(timeout=5):
                    raise RuntimeError("Worker was never released")
                with app.state.database.transaction():
                    return {"status": "collecting"}
            finally:
                finished.set()

        async def application_lifetime():
            async with app.router.lifespan_context(app):
                await self.wait_for_event(started)

        with (
            patch.object(app.state.learning, "run", blocked),
            patch.object(app.state.database.engine, "dispose") as dispose,
        ):
            lifetime = asyncio.create_task(application_lifetime())
            try:
                await self.wait_for_event(started)
                await asyncio.sleep(0.03)
                self.assertFalse(lifetime.done())
                dispose.assert_not_called()
            finally:
                release.set()
                await asyncio.wait_for(lifetime, timeout=3)
            self.assertTrue(finished.is_set())
            dispose.assert_called_once_with()
        app.state.database.engine.dispose()

    async def test_repeated_cancellation_still_waits_for_worker(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def blocked():
            started.set()
            try:
                if not release.wait(timeout=5):
                    raise RuntimeError("Worker was never released")
            finally:
                finished.set()

        worker = asyncio.create_task(_background_call(blocked))
        try:
            await self.wait_for_event(started)
            worker.cancel()
            await asyncio.sleep(0.01)
            worker.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(worker.done())
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=3)
        self.assertTrue(finished.is_set())
