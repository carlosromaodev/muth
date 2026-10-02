import unittest
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

from muth.main import create_app
from muth.storage import LearningSampleRow, SessionRow
from tests.test_sessions import settings


def png(*, color="white"):
    content = BytesIO()
    Image.new("RGB", (640, 480), color).save(content, format="PNG")
    return content.getvalue()


class CaptureApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings())
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, **changes):
        payload = {
            "consent": {
                "accepted": True,
                "purpose": "onboarding",
                "policy_version": "capture-privacy-v1",
            },
            "device_group": "web",
        }
        return self.client.post("/capture-api/sessions", json=payload | changes)

    def verify(self, session, *, back=None, headers=None, extra=None):
        files = {
            "document_front": ("front.png", png(color="red"), "image/png"),
            "document_back": ("back.png", back or png(color="blue"), "image/png"),
            "selfie": ("selfie.png", png(color="green"), "image/png"),
        }
        return self.client.post(
            f"/capture-api/sessions/{session['session_id']}/verify",
            headers={
                "Authorization": f"Bearer {session['capture_token']}",
                "Idempotency-Key": "capture-001",
                **(headers or {}),
            },
            files=files | (extra or {}),
        )

    def test_flow_replay_encrypted_report_deletion_and_no_learning(self):
        created = self.create()
        self.assertEqual(created.status_code, 201)
        session = created.json()
        response = self.verify(session)
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertIsNone(report["score"]["value"])
        self.assertFalse(report["score"]["authenticity_confirmed"])
        self.assertNotEqual(report["status"], "approved")
        self.assertEqual(self.verify(session).json(), report)
        self.assertEqual(self.verify(session, back=png(color="yellow")).status_code, 409)
        headers = {"Authorization": f"Bearer {session['capture_token']}"}
        self.assertEqual(
            self.client.get(
                f"/capture-api/sessions/{session['session_id']}", headers=headers
            ).json(),
            report,
        )
        with self.app.state.database.transaction() as db:
            row = db.get(SessionRow, session["session_id"])
            self.assertEqual(row.tenant_id, "capture-portal")
            self.assertNotIn(report["verification_id"], row.capture_result)
            self.assertEqual(list(db.scalars(select(LearningSampleRow))), [])
        self.assertEqual(
            self.client.delete(
                f"/capture-api/sessions/{session['session_id']}", headers=headers
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.get(
                f"/capture-api/sessions/{session['session_id']}", headers=headers
            ).status_code,
            404,
        )
        with self.app.state.database.transaction() as db:
            row = db.get(SessionRow, session["session_id"])
            self.assertIsNone(row.capture_result)
            self.assertIsNone(row.result)

    def test_consent_scope_cross_session_and_b2b_isolation(self):
        self.assertEqual(self.create(tenant_id="alpha").status_code, 422)
        self.assertEqual(
            self.create(
                consent={"accepted": True, "purpose": "onboarding", "policy_version": "old"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.create(
                consent={
                    "accepted": True,
                    "purpose": "onboarding",
                    "policy_version": "capture-privacy-v1",
                    "learning_opt_in": True,
                }
            ).status_code,
            422,
        )
        one, two = self.create().json(), self.create().json()
        headers = {"Authorization": f"Bearer {one['capture_token']}"}
        self.assertEqual(
            self.client.get(
                f"/capture-api/sessions/{two['session_id']}", headers=headers
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(f"/v1/sessions/{one['session_id']}", headers=headers).status_code, 401
        )
        self.assertEqual(
            self.client.get(
                f"/v1/sessions/{one['session_id']}", headers={"X-API-Key": "alpha-token"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/capture-api/sessions/{one['session_id']}/feedback", headers=headers, json={}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.verify(one, headers={"Authorization": "Bearer invalid"}).status_code, 401
        )

    def test_origin_json_only_portal_disabled_and_reserved_tenant(self):
        for origin in ("https://attacker.test", "null", "http://["):
            response = self.client.post(
                "/capture-api/sessions", headers={"Origin": origin}, json={}
            )
            self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.post("/capture-api/sessions", data={"accepted": "true"}).status_code, 415
        )
        self.assertEqual(
            self.client.post(
                "/capture-api/sessions", json={}, headers={"Sec-Fetch-Site": "cross-site"}
            ).status_code,
            403,
        )
        for tenant in ("alpha", "local"):
            with self.assertRaises(ValidationError):
                settings(capture_tenant_id=tenant)
        with TestClient(create_app(settings(capture_portal_enabled=False))) as client:
            self.assertFalse(client.get("/capture-api/config").json()["enabled"])
            self.assertEqual(client.post("/capture-api/sessions", json={}).status_code, 503)

    def test_exact_multipart_and_expired_token(self):
        session = self.create().json()
        self.assertEqual(
            self.verify(session, extra={"other": ("x.png", png(), "image/png")}).status_code, 400
        )
        self.app.state.store.clock = lambda: 10**12
        self.assertEqual(self.verify(session).status_code, 410)

    def test_quota_abandoned_capture_cleanup_and_retained_result_scrub(self):
        with TestClient(create_app(settings(capture_max_sessions=1))) as client:
            payload = {
                "consent": {
                    "accepted": True,
                    "purpose": "onboarding",
                    "policy_version": "capture-privacy-v1",
                }
            }
            first = client.post("/capture-api/sessions", json=payload).json()
            self.assertEqual(client.post("/capture-api/sessions", json=payload).status_code, 429)
            original_clock = client.app.state.store.clock
            future = original_clock() + 2000
            client.app.state.store.clock = lambda: future
            self.assertEqual(client.post("/capture-api/sessions", json=payload).status_code, 201)
            with client.app.state.database.transaction() as db:
                self.assertIsNone(db.get(SessionRow, first["session_id"]).payload)
        session = self.create().json()
        self.assertEqual(self.verify(session).status_code, 200)
        self.app.state.store.clock = lambda: 10**12
        self.assertGreaterEqual(self.app.state.capture_store.purge(), 1)
        with self.app.state.database.transaction() as db:
            self.assertIsNone(db.get(SessionRow, session["session_id"]).capture_result)

    def test_html_and_asset_html_keep_identical_security_headers(self):
        for path in ("/", "/capture", "/assets/index.html"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_mounted_portal_guard_cannot_be_bypassed(self):
        outer = FastAPI()
        outer.mount("/muth", create_app(settings(capture_portal_enabled=False)))
        with TestClient(outer) as client:
            response = client.post("/muth/capture-api/sessions", json={})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(client.get("/muth/v1/sessions/unknown").status_code, 401)


class CaptureAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_token_and_unknown_route_rejected_before_upload_read(self):
        app = create_app(settings())
        self.addCleanup(app.state.database.engine.dispose)

        async def receive():
            self.fail("Unauthorized capture upload must remain unread")

        for path, expected in [
            ("/capture-api/sessions/mth_ses_" + "a" * 32 + "/verify", 401),
            ("/capture-api/unknown", 404),
        ]:
            messages = []

            async def send(message, target=messages):
                target.append(message)

            await app(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": path,
                    "root_path": "",
                    "query_string": b"",
                    "headers": [],
                    "server": ("testserver", 80),
                    "client": ("testclient", 5000),
                },
                receive,
                send,
            )
            self.assertEqual(messages[0]["status"], expected)
