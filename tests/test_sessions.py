import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from muth.config import SCOPES, Settings, TenantKey
from muth.domain import CreateSession
from muth.errors import MuthError
from muth.main import create_app
from muth.media import decode_image
from muth.security import Principal
from muth.storage import Database, SessionRow, SessionStore
from tests.test_muth import png


def key(tenant, token, scopes=SCOPES):
    return TenantKey(
        tenant_id=tenant,
        key_id=f"{tenant}-key",
        key_sha256=hashlib.sha256(token.encode()).hexdigest(),
        scopes=scopes,
    )


def settings(**kwargs):
    return Settings(
        _env_file=None,
        engine_mode="demo",
        tenants=[
            key("alpha", "alpha-token"),
            key("beta", "beta-token"),
            key("reader", "reader-token", {"read"}),
        ],
        **kwargs,
    )


PAYLOAD = {
    "consent": {"accepted": True, "purpose": "onboarding", "policy_version": "privacy-v1"},
    "subject_reference": "customer-private-123",
}


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings())
        self.client = TestClient(self.app, headers={"X-API-Key": "alpha-token"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self):
        response = self.client.post("/v1/sessions", json=PAYLOAD)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["session_id"]

    def verify(self, sid, token="attempt-one", image=None):
        return self.client.post(
            f"/v1/sessions/{sid}/verify",
            headers={"Idempotency-Key": token},
            files={
                "document": ("bi.png", png() if image is None else image),
                "selfie": ("selfie.png", png()),
            },
        )

    def test_complete_lifecycle_and_encryption(self):
        sid = self.create()
        result = self.verify(sid)
        self.assertEqual(result.status_code, 200, result.text)
        session = self.client.get(f"/v1/sessions/{sid}").json()
        self.assertEqual(session["verification"], result.json())
        self.assertEqual(session["status"], "review")
        self.assertEqual(session["consent"], {**PAYLOAD["consent"], "learning_opt_in": False})
        with self.app.state.database.transaction() as db:
            row = db.get(SessionRow, sid)
            self.assertNotIn("customer-private-123", row.payload)
            self.assertNotIn("verification_id", row.result)
            self.assertNotIn("attempt-one", row.idempotency_hash)
        events = self.client.get(f"/v1/sessions/{sid}/events").json()
        self.assertEqual(
            [e["event_type"] for e in events],
            ["session_created", "verification_started", "verification_completed"],
        )
        self.assertNotIn("customer-private-123", json.dumps(events))
        self.assertTrue(all(e["request_id"] for e in events))

    def test_consent_is_strict_and_tenant_cannot_be_supplied(self):
        for accepted in [False, "true", 1, None]:
            payload = {**PAYLOAD, "consent": {**PAYLOAD["consent"], "accepted": accepted}}
            self.assertEqual(self.client.post("/v1/sessions", json=payload).status_code, 422)
        self.assertEqual(self.client.post("/v1/sessions", json={}).status_code, 422)
        response = self.client.post("/v1/sessions", json={**PAYLOAD, "tenant_id": "beta"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("customer-private-123", response.text)

    def test_idempotent_replay_and_payload_conflict(self):
        sid = self.create()
        first = self.verify(sid)
        replay = self.verify(sid)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(self.verify(sid, image=png((16, 16))).status_code, 409)
        self.assertEqual(self.verify(sid, token="attempt-two").status_code, 409)
        events = self.client.get(f"/v1/sessions/{sid}/events").json()
        self.assertEqual(len(events), 3)

    def test_cross_tenant_access_is_hidden_for_every_resource(self):
        sid = self.create()
        headers = {"X-API-Key": "beta-token"}
        for suffix in ["", "/events"]:
            self.assertEqual(
                self.client.get(f"/v1/sessions/{sid}{suffix}", headers=headers).status_code, 404
            )
        self.assertEqual(
            self.client.delete(f"/v1/sessions/{sid}", headers=headers).status_code, 404
        )
        response = self.client.post(
            f"/v1/sessions/{sid}/verify",
            headers={**headers, "Idempotency-Key": "attempt-one"},
            files={"document": png(), "selfie": png()},
        )
        self.assertEqual(response.status_code, 404)
        review = self.client.post(
            f"/v1/sessions/{sid}/review",
            headers=headers,
            json={"decision": "rejected", "reason_code": "suspected_fraud"},
        )
        self.assertEqual(review.status_code, 404)

    def test_reviewer_can_reject_but_never_approve_demo(self):
        sid = self.create()
        self.verify(sid)
        response = self.client.post(
            f"/v1/sessions/{sid}/review",
            json={"decision": "approved", "reason_code": "suspected_fraud"},
        )
        self.assertEqual(response.status_code, 422)
        response = self.client.post(
            f"/v1/sessions/{sid}/review",
            json={"decision": "rejected", "reason_code": "suspected_fraud"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["verification"]["status"], "rejected")
        self.assertEqual(self.verify(sid).json()["status"], "rejected")

    def test_delete_scrubs_payloads_and_preserves_minimal_audit(self):
        sid = self.create()
        self.verify(sid)
        self.assertEqual(self.client.delete(f"/v1/sessions/{sid}").status_code, 204)
        self.assertEqual(self.client.get(f"/v1/sessions/{sid}").status_code, 404)
        self.assertEqual(self.verify(sid).status_code, 404)
        with self.app.state.database.transaction() as db:
            row = db.get(SessionRow, sid)
            for attr in ["payload", "result", "fingerprint", "idempotency_hash", "attempt"]:
                self.assertIsNone(getattr(row, attr))
        events = self.client.get(f"/v1/sessions/{sid}/events").json()
        self.assertEqual(events[-1]["event_type"], "session_deleted")

    def test_expiration_and_retention(self):
        now = [1000.0]
        self.app.state.store.clock = lambda: now[0]
        sid = self.create()
        now[0] += 1801
        self.assertEqual(self.client.get(f"/v1/sessions/{sid}").json()["status"], "expired")
        self.assertEqual(self.verify(sid).status_code, 410)
        now[0] += 31 * 86400
        self.assertEqual(self.client.get(f"/v1/sessions/{sid}").status_code, 404)
        self.assertEqual(self.app.state.store.purge(), 1)
        self.assertEqual(self.app.state.store.purge(), 0)
        with self.app.state.database.transaction() as db:
            self.assertIsNone(db.get(SessionRow, sid).payload)

    def test_engine_failure_is_sanitized_and_retryable(self):
        sid = self.create()
        with patch.object(
            self.app.state.engines.liveness,
            "assess",
            side_effect=RuntimeError("sensitive-provider-detail"),
        ):
            response = self.verify(sid)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sensitive-provider-detail", response.text)
        self.assertEqual(
            self.client.get(f"/v1/sessions/{sid}").json()["status"], "awaiting_capture"
        )
        self.assertEqual(self.verify(sid).status_code, 200)

    def test_authentication_scopes_and_error_request_ids(self):
        for token, expected in [("", 401), ("bad", 401), ("reader-token", 403)]:
            response = self.client.post("/v1/sessions", json=PAYLOAD, headers={"X-API-Key": token})
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])
            self.assertNotIn("customer-private-123", response.text)
        response = self.client.get("/metrics", headers={"X-API-Key": "reader-token"})
        self.assertEqual(response.status_code, 403)

    def test_metrics_use_template_labels_and_no_customer_identifiers(self):
        sid = self.create()
        self.client.get(f"/v1/sessions/{sid}")
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("muth_http_requests_total", response.text)
        self.assertNotIn(sid, response.text)
        self.assertNotIn("customer-private-123", response.text)

    def test_missing_idempotency_key_is_rejected(self):
        sid = self.create()
        response = self.client.post(
            f"/v1/sessions/{sid}/verify", files={"document": png(), "selfie": png()}
        )
        self.assertEqual(response.status_code, 422)


class PersistenceAndClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = settings(
            database_url=f"sqlite:///{self.temp.name}/muth.db",
            data_key=SecretStr(Fernet.generate_key().decode()),
        )
        self.db = Database(self.config)
        self.addCleanup(self.db.engine.dispose)
        self.db.migrate()
        self.store = SessionStore(self.db, self.config)
        self.principal = Principal("alpha", "alpha-key", frozenset(SCOPES))
        self.payload = CreateSession.model_validate(PAYLOAD)

    def test_schema_up_down_up_and_restart(self):
        sid = self.store.create(self.principal, self.payload, "a" * 32).session_id
        other_db = Database(self.config)
        self.addCleanup(other_db.engine.dispose)
        other = SessionStore(other_db, self.config)
        self.assertTrue(other_db.ready())
        self.assertEqual(other.get(self.principal, sid).subject_reference, "customer-private-123")
        self.db.migrate("base", downgrade=True)
        self.assertFalse(self.db.ready())
        self.db.migrate()
        self.assertTrue(self.db.ready())

    def test_result_survives_api_restart(self):
        with TestClient(create_app(self.config), headers={"X-API-Key": "alpha-token"}) as client:
            sid = client.post("/v1/sessions", json=PAYLOAD).json()["session_id"]
            result = client.post(
                f"/v1/sessions/{sid}/verify",
                headers={"Idempotency-Key": "attempt-one"},
                files={"document": png(), "selfie": png()},
            ).json()
        with TestClient(create_app(self.config), headers={"X-API-Key": "alpha-token"}) as client:
            self.assertEqual(client.get(f"/v1/sessions/{sid}").json()["verification"], result)

    def test_two_database_connections_only_one_can_claim(self):
        sid = self.store.create(self.principal, self.payload, "a" * 32).session_id
        other_db = Database(self.config)
        self.addCleanup(other_db.engine.dispose)
        other = SessionStore(other_db, self.config)
        barrier = threading.Barrier(2)

        def claim(store):
            barrier.wait(timeout=5)
            try:
                store.claim(self.principal, sid, "attempt-one", "fingerprint", "b" * 32)
                return "claimed"
            except MuthError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [self.store, other]))
        self.assertCountEqual(results, ["claimed", "session_busy"])

    def test_lease_recovery_blocks_old_completion_and_delete_blocks_new_completion(self):
        now = [1000.0]
        self.store.clock = lambda: now[0]
        sid = self.store.create(self.principal, self.payload, "a" * 32).session_id
        first = self.store.claim(self.principal, sid, "attempt-one", "fp", "b" * 32)
        now[0] += 121
        second = self.store.claim(self.principal, sid, "attempt-one", "fp", "c" * 32)
        app = create_app(settings())
        self.addCleanup(app.state.database.engine.dispose)
        image = decode_image(png(), self.config)
        result = app.state.verify_service.verify(image, image)
        with self.assertRaises(MuthError) as error:
            self.store.complete(self.principal, sid, first.attempt, result, "d" * 32)
        self.assertEqual(error.exception.code, "attempt_invalidated")
        self.store.delete(self.principal, sid, "e" * 32)
        with self.assertRaises(MuthError):
            self.store.complete(self.principal, sid, second.attempt, result, "f" * 32)
        with self.db.transaction() as db:
            row = db.scalar(select(SessionRow).where(SessionRow.session_id == sid))
            self.assertEqual(row.status, "deleted")
            self.assertIsNone(row.result)

    def test_persistence_requires_stable_encryption_key(self):
        with self.assertRaises(ValueError):
            settings(database_url=f"sqlite:///{Path(self.temp.name) / 'bad.db'}")
