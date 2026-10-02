import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from sqlalchemy import select

from muth.storage import AuditRow, Database
from tests.test_sessions import settings


class DatabaseReadinessTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(settings())
        self.addCleanup(self.db.engine.dispose)

    def row(self, event_id="event-test"):
        return AuditRow(
            event_id=event_id,
            tenant_id="alpha",
            session_id="opaque-session",
            event_type="test",
            actor="test",
            created_at=1000,
            request_id="opaque-request",
        )

    def test_readiness_inside_transaction_does_not_rollback_pending_writes(self):
        with self.db.transaction() as db:
            db.add(self.row())
            db.flush()
            self.assertTrue(self.db.ready())
            self.assertIsNotNone(db.get(AuditRow, "event-test"))
        with self.db.transaction() as db:
            self.assertIsNotNone(db.get(AuditRow, "event-test"))

    def test_concurrent_readiness_waits_for_commit(self):
        started = threading.Event()

        def ready():
            started.set()
            return self.db.ready()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.db.transaction() as db:
                db.add(self.row())
                db.flush()
                result = executor.submit(ready)
                self.assertTrue(started.wait(timeout=2))
                with self.assertRaises(TimeoutError):
                    result.result(timeout=0.05)
            self.assertTrue(result.result(timeout=2))
        with self.db.transaction() as db:
            self.assertIsNotNone(db.get(AuditRow, "event-test"))

    def test_nested_transaction_rollback_preserves_outer_work(self):
        with self.db.transaction() as db:
            db.add(self.row("outer"))
            with self.assertRaises(ValueError):
                with self.db.transaction() as nested:
                    nested.add(self.row("inner"))
                    nested.flush()
                    raise ValueError("Rollback savepoint")
            self.assertTrue(self.db.ready())
        with self.db.transaction() as db:
            self.assertEqual(list(db.scalars(select(AuditRow.event_id))), ["outer"])

    def test_nested_commit_is_rolled_back_with_unwritten_outer_transaction(self):
        for read_first in (False, True):
            with self.subTest(read_first=read_first):
                with self.assertRaises(ValueError):
                    with self.db.transaction() as db:
                        if read_first:
                            self.assertTrue(self.db.ready())
                        with self.db.transaction() as nested:
                            nested.add(self.row("nested-commit"))
                            nested.flush()
                        raise ValueError("Rollback enclosing transaction")
                with self.db.transaction() as db:
                    self.assertIsNone(db.get(AuditRow, "nested-commit"))
