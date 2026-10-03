import unittest
from io import BytesIO
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from muth.main import create_app
from muth.storage import Base, SessionRow
from tests.test_sessions import settings


def preview(size=(800, 600), color="white"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def consent(**changes):
    return {
        "accepted": True,
        "purpose": "onboarding",
        "policy_version": "capture-privacy-v1",
        "camera_frames_opt_in": True,
        "camera_policy_version": "capture-camera-v1",
        **changes,
    }


class LiveCameraApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings(document_ocr_enabled=False))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, **changes):
        response = self.client.post("/capture-api/sessions", json={"consent": consent(**changes)})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def assess(self, session, target="document_front", *, files=None, headers=None):
        return self.client.post(
            f"/capture-api/sessions/{session['session_id']}/camera-assessment",
            params={"target": target},
            headers={"Authorization": f"Bearer {session['capture_token']}", **(headers or {})},
            files=files
            if files is not None
            else {"frame": ("preview.png", preview(), "image/png")},
        )

    def snapshot(self):
        with self.app.state.database.transaction() as db:
            return {
                table.name: [tuple(row) for row in db.execute(select(table))]
                for table in Base.metadata.sorted_tables
            }

    def test_opted_in_frames_are_ephemeral_and_never_run_verification_or_ocr(self):
        session = self.create()
        self.assertTrue(session["camera_frames_enabled"])
        before = self.snapshot()
        with (
            patch.object(self.app.state.document_ocr, "extract") as ocr,
            patch.object(self.app.state.verify_service, "verify") as verifier,
        ):
            for target in ("document_front", "document_back", "selfie"):
                response = self.assess(session, target)
                self.assertEqual(response.status_code, 200, response.text)
                report = response.json()
                self.assertEqual(report["target"], target)
                self.assertEqual(report["version"], "camera-assessment-v1")
                self.assertFalse(report["ready"])
                self.assertFalse(report["authenticity_confirmed"])
                self.assertIsNone(report["appearance_signature"])
                self.assertNotIn("identity_score", report)
            ocr.assert_not_called()
            verifier.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        with self.app.state.database.transaction() as db:
            row = db.get(SessionRow, session["session_id"])
            self.assertNotIn("camera_frames_opt_in", row.payload)
            self.assertNotIn(session["capture_token"], row.payload)
            payload = self.app.state.store._decrypt(row.payload)
            self.assertIn('"camera_frames_opt_in":true', payload)
            self.assertIn('"camera_policy_version":"capture-camera-v1"', payload)
        self.assertEqual(self.app.state.capacity.requests, 0)
        self.assertEqual(self.app.state.capacity.workers, 0)

    def test_legacy_session_has_no_camera_permission_and_consent_is_strict(self):
        session = self.create(camera_frames_opt_in=False, camera_policy_version=None)
        self.assertFalse(session["camera_frames_enabled"])
        denied = self.assess(session)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "camera_consent_required")
        for value in ("true", 1, None):
            with self.subTest(value=value):
                response = self.client.post(
                    "/capture-api/sessions", json={"consent": consent(camera_frames_opt_in=value)}
                )
                self.assertEqual(response.status_code, 422)
        for version in (None, "old-camera-policy"):
            response = self.client.post(
                "/capture-api/sessions", json={"consent": consent(camera_policy_version=version)}
            )
            self.assertEqual(response.status_code, 422)

    def test_wrong_origin_cross_session_and_backend_key_cannot_access_frames(self):
        one, two = self.create(), self.create()
        self.assertEqual(
            self.assess(one, headers={"Origin": "https://other.example"}).status_code, 403
        )
        self.assertEqual(
            self.assess(one, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403
        )
        for authorization in ("Bearer invalid", f"Bearer {two['capture_token']}", ""):
            response = self.assess(
                one, headers={"Authorization": authorization, "X-API-Key": "alpha-token"}
            )
            self.assertEqual(response.status_code, 401)

    def test_disabled_camera_flags_and_valid_old_consent_remain_usable(self):
        with TestClient(create_app(settings(live_camera_enabled=False))) as client:
            config = client.get("/capture-api/config").json()
            self.assertFalse(config["live_camera_enabled"])
            self.assertFalse(config["live_document_detector_ready"])
            self.assertFalse(config["live_face_detector_ready"])
            self.assertEqual(
                client.post("/capture-api/sessions", json={"consent": consent()}).status_code, 422
            )
            created = client.post(
                "/capture-api/sessions",
                json={"consent": consent(camera_frames_opt_in=False, camera_policy_version=None)},
            )
            self.assertEqual(created.status_code, 201)
            session = created.json()
            response = client.post(
                f"/capture-api/sessions/{session['session_id']}/camera-assessment?target=selfie",
                headers={"Authorization": f"Bearer {session['capture_token']}"},
                files={"frame": ("frame.png", preview(), "image/png")},
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "live_camera_disabled")

    def test_target_is_canonical_single_and_multipart_has_one_frame(self):
        session = self.create()
        for target in ("docfront", "front", "", "document_front&target=selfie"):
            self.assertEqual(self.assess(session, target).status_code, 422)
        path = f"/capture-api/sessions/{session['session_id']}/camera-assessment"
        auth = {"Authorization": f"Bearer {session['capture_token']}"}
        self.assertEqual(self.client.post(path, headers=auth, json={}).status_code, 422)
        self.assertEqual(
            self.client.post(path + "?target=selfie", headers=auth, json={}).status_code, 415
        )
        response = self.client.post(
            path + "?target=document_front&target=selfie",
            headers=auth,
            files={"frame": preview()},
        )
        self.assertEqual(response.status_code, 422)
        for files, expected in (
            ({"other": ("x.png", preview(), "image/png")}, 422),
            ({"frame": ("x.png", preview()), "other": ("y.png", preview())}, 400),
            ([("frame", ("x.png", preview())), ("frame", ("y.png", preview()))], 400),
            ({"frame": (None, "text")}, 400),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(self.assess(session, files=files).status_code, expected)

    def test_size_dimension_and_image_structure_are_bounded(self):
        session = self.create()
        for frame, expected in (
            (b"", 422),
            (b"not an image", 422),
            (b"x" * (512 * 1024 + 1), 413),
            (preview((1281, 400)), 413),
            (preview((1280, 1600)), 413),
        ):
            response = self.assess(session, files={"frame": ("frame.png", frame, "image/png")})
            self.assertEqual(response.status_code, expected, response.text)
            self.assertEqual(self.app.state.capacity.requests, 0)
            self.assertEqual(self.app.state.capacity.workers, 0)
        self.assertEqual(
            self.assess(session, files={"frame": ("frame.png", preview((1280, 800)))}).status_code,
            200,
        )

    def test_detector_failure_is_sanitized_and_capacity_reusable(self):
        session = self.create()
        with patch("muth.capture_api.assess_camera", side_effect=RuntimeError("private-frame")):
            response = self.assess(session)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "camera_detector_failure")
        self.assertNotIn("private-frame", response.text)
        self.assertEqual(self.app.state.capacity.requests, 0)
        self.assertEqual(self.app.state.capacity.workers, 0)
        self.assertEqual(self.assess(session).status_code, 200)

    def test_camera_route_uses_mounted_capabilities_and_origin(self):
        outer = FastAPI()
        outer.mount("/muth", create_app(settings(document_ocr_enabled=False)))
        with TestClient(outer) as client:
            session = client.post("/muth/capture-api/sessions", json={"consent": consent()}).json()
            response = client.post(
                f"/muth/capture-api/sessions/{session['session_id']}/camera-assessment?target=selfie",
                headers={
                    "Authorization": f"Bearer {session['capture_token']}",
                    "Origin": "http://testserver",
                },
                files={"frame": preview()},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["detector_ready"])


if __name__ == "__main__":
    unittest.main()
