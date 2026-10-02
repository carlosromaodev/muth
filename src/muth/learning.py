"""Consented score calibration, independent labels, immutable versions and revocation.

No image retention, no self-labels, no online updates to neural network weights.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import delete, select, text
from starlette.concurrency import run_in_threadpool

from muth.domain import CreateSession, LearningFeedback
from muth.engines.biometric import Calibration
from muth.errors import MuthError
from muth.security import Principal
from muth.storage import (
    ActiveCalibrationRow,
    CalibrationMemberRow,
    CalibrationRow,
    LearningSampleRow,
)


def subject_hash(store, tenant, subject):
    key = hmac.digest(store.fingerprint_key, b"learning-subject:v1", "sha256")
    return hmac.new(key, json.dumps([tenant, subject]).encode(), "sha256").hexdigest()


def cohort(subject):
    bucket = int(subject[:8], 16) % 10
    return "calibration" if bucket < 6 else "validation" if bucket < 8 else "test"


def collect_samples(store, db, session, result):
    if store.settings.engine_mode != "biometric" or not store.settings.learning_enabled:
        return
    payload = CreateSession.model_validate_json(store._decrypt(session.payload))
    if not payload.consent.learning_opt_in or not payload.subject_reference:
        return
    subject = subject_hash(store, session.tenant_id, payload.subject_reference)
    for role, check, kind in (
        ("face", result.checks.face_match, "cosine_similarity"),
        ("liveness", result.checks.liveness, "liveness_softmax"),
    ):
        if check.score is None or check.score_kind != kind or not check.model_fingerprint:
            continue
        excluded = "liveness_ensemble_disagreement" in check.reasons
        db.add(
            LearningSampleRow(
                sample_id=f"mth_smp_{uuid4().hex}",
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                role=role,
                model_fingerprint=check.model_fingerprint,
                subject_hash=subject,
                split=cohort(subject),
                state="excluded" if excluded else "unlabelled",
                payload=store._encrypt(
                    json.dumps(
                        {
                            "score": check.score,
                            "device_group": payload.device_group,
                            "score_kind": kind,
                            "label": None,
                            "pair": subject,
                        }
                    )
                ),
                created_at=store.clock(),
                retain_until=session.retain_until,
            )
        )


def erase_samples(db, session_ids):
    ids = list(
        db.scalars(
            select(LearningSampleRow.sample_id).where(LearningSampleRow.session_id.in_(session_ids))
        )
    )
    if not ids:
        return
    policies = set(
        db.scalars(
            select(CalibrationMemberRow.policy_id).where(CalibrationMemberRow.sample_id.in_(ids))
        )
    )
    if policies:
        for row in db.scalars(select(CalibrationRow).where(CalibrationRow.policy_id.in_(policies))):
            row.state = "revoked"
        for active in db.scalars(
            select(ActiveCalibrationRow).where(ActiveCalibrationRow.policy_id.in_(policies))
        ):
            active.policy_id = None
            active.generation += 1
        # Remove associations to erased subjects; reports contain aggregate counts only.
        db.execute(delete(CalibrationMemberRow).where(CalibrationMemberRow.policy_id.in_(policies)))
    db.execute(delete(LearningSampleRow).where(LearningSampleRow.sample_id.in_(ids)))


@dataclass(frozen=True)
class Gates:
    min_calibration_per_class: int = 50
    min_evaluation_per_class: int = 400
    max_false_accept_upper: float = 0.01
    max_false_reject: float = 0.10
    max_attempts_per_test_panel: int = 5


async def refinement_loop(service, interval, tenants, *, pause=None):
    pause = pause or asyncio.sleep
    while True:
        await pause(interval)
        if not service.store.db.ready():
            continue
        for tenant in tenants:
            try:
                await run_in_threadpool(service.run, tenant)
            except Exception as exc:
                logging.getLogger("muth.learning").warning(
                    "refinement_failed exception_type=%s", type(exc).__name__
                )


def wilson_upper(errors, total):
    if not total:
        return 1.0
    z = 1.959963984540054
    p = errors / total
    return (
        p + z * z / (2 * total) + z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    ) / (1 + z * z / total)


def rates(samples, threshold):
    positives = [s for s in samples if s["positive"]]
    negatives = [s for s in samples if not s["positive"]]
    fa = sum(s["score"] >= threshold for s in negatives)
    fr = sum(s["score"] < threshold for s in positives)
    return {
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "false_accept_count": fa,
        "false_reject_count": fr,
        "false_accept_rate": fa / len(negatives) if negatives else 1,
        "false_reject_rate": fr / len(positives) if positives else 1,
        "false_accept_upper_95": wilson_upper(fa, len(negatives)),
    }


def evaluation(samples, threshold, gates, role):
    metrics = {"overall": rates(samples, threshold)}
    failures = []
    for device in sorted({s["device_group"] for s in samples} - {"unknown"}):
        metrics[f"device:{device}"] = rates(
            [s for s in samples if s["device_group"] == device], threshold
        )
    if role == "liveness":
        for attack in sorted({s.get("attack_type") for s in samples if not s["positive"]}):
            # BPCER uses bona fide samples; APCER is evaluated separately per attack.
            metrics[f"attack:{attack}"] = rates(
                [s for s in samples if s["positive"] or s.get("attack_type") == attack], threshold
            )
    for group, result in metrics.items():
        if min(result["positive_count"], result["negative_count"]) < gates.min_evaluation_per_class:
            failures.append(f"{group}:insufficient_samples")
        if result["false_accept_upper_95"] > gates.max_false_accept_upper:
            failures.append(f"{group}:false_accept_bound")
        if result["false_reject_rate"] > gates.max_false_reject:
            failures.append(f"{group}:false_reject_rate")
    return metrics, failures


class LearningService:
    def __init__(self, store, runtime=None, *, gates=None):
        self.store, self.runtime, self.gates = store, runtime, gates or Gates()

    def feedback(self, principal, session_id, feedback: LearningFeedback, request_id):
        if not self.store.settings.learning_enabled:
            raise MuthError(409, "learning_disabled", "Aprendizagem desactivada.")
        if feedback.source != "human_review":
            raise MuthError(422, "source_not_integrated", "Fonte oficial ainda não integrada.")
        with self.store.db.transaction() as db:
            if not self.store.db.in_memory:
                db.execute(text("BEGIN IMMEDIATE"))
            session = self.store._row(db, principal, session_id)
            sample = db.scalar(
                select(LearningSampleRow).where(
                    LearningSampleRow.session_id == session_id,
                    LearningSampleRow.tenant_id == principal.tenant_id,
                    LearningSampleRow.role == feedback.role,
                )
            )
            if not sample:
                raise MuthError(409, "sample_unavailable", "Sem amostra biométrica autorizada.")
            data = json.loads(self.store._decrypt(sample.payload))
            if data["label"] is not None:
                if data["feedback"] == feedback.model_dump():
                    return {"sample_id": sample.sample_id, "state": sample.state}
                raise MuthError(409, "label_conflict", "Rótulo já confirmado; requer investigação.")
            payload = CreateSession.model_validate_json(self.store._decrypt(session.payload))
            capture_subject = feedback.capture_subject_reference or payload.subject_reference
            captured = subject_hash(self.store, principal.tenant_id, capture_subject)
            if feedback.role == "face":
                if (feedback.label == "genuine") != (captured == sample.subject_hash):
                    raise MuthError(
                        422, "subject_conflict", "Referências incompatíveis com o rótulo."
                    )
                data["pair"] = ":".join(sorted({sample.subject_hash, captured}))
            excluded = sample.state == "excluded" or cohort(captured) != sample.split
            # RGB pixels cannot validate camera injection attacks.
            excluded |= feedback.attack_type in {"injection", "other"}
            data.update(
                label=feedback.label,
                feedback=feedback.model_dump(),
                attack_type=feedback.attack_type,
            )
            sample.state = "excluded" if excluded else "labelled"
            sample.payload = self.store._encrypt(json.dumps(data))
            self.store._event(db, principal, session_id, "learning_label_confirmed", request_id)
            return {"sample_id": sample.sample_id, "state": sample.state, "split": sample.split}

    def withdraw(self, principal, session_id, request_id):
        with self.store.db.transaction() as db:
            session = self.store._row(db, principal, session_id)
            payload = CreateSession.model_validate_json(self.store._decrypt(session.payload))
            payload.consent.learning_opt_in = False
            session.payload = self.store._encrypt(payload.model_dump_json())
            erase_samples(db, [session_id])
            self.store._event(db, principal, session_id, "learning_consent_withdrawn", request_id)

    def _active(self, db, tenant, role, fingerprint):
        return db.get(ActiveCalibrationRow, (tenant, role, fingerprint))

    def _valid_policy(self, db, policy):
        if not policy or policy.state != "passed":
            return False
        members = list(
            db.scalars(
                select(CalibrationMemberRow.sample_id).where(
                    CalibrationMemberRow.policy_id == policy.policy_id
                )
            )
        )
        available = list(
            db.scalars(
                select(LearningSampleRow.sample_id).where(
                    LearningSampleRow.sample_id.in_(members),
                    LearningSampleRow.retain_until > self.store.clock(),
                )
            )
        )
        return bool(members) and len(available) == len(members)

    def calibration(self, tenant, role, engine, device_group="unknown"):
        if not self.store.settings.learning_enabled:
            return engine.calibration
        with self.store.db.transaction() as db:
            active = self._active(db, tenant, role, engine.fingerprint)
            policy = (
                db.get(CalibrationRow, active.policy_id) if active and active.policy_id else None
            )
            if self._valid_policy(db, policy):
                report = json.loads(self.store._decrypt(policy.report))
                if device_group != "unknown" and any(
                    f"device:{device_group}" not in report[split]
                    for split in ("validation", "test")
                ):
                    return engine.calibration
                return Calibration(policy.threshold, policy.policy_id, True)
        return engine.calibration

    def bundle(self, tenant, device_group="unknown"):
        if not self.runtime:
            return None
        return self.runtime.bundle(
            self.calibration(tenant, "face", self.runtime.face, device_group),
            self.calibration(tenant, "liveness", self.runtime.liveness, device_group),
        )

    def status(self, tenant):
        with self.store.db.transaction() as db:
            samples = db.scalars(
                select(LearningSampleRow).where(
                    LearningSampleRow.tenant_id == tenant,
                    LearningSampleRow.retain_until > self.store.clock(),
                )
            ).all()
            policies = db.scalars(
                select(CalibrationRow)
                .where(CalibrationRow.tenant_id == tenant)
                .order_by(CalibrationRow.created_at.desc())
                .limit(20)
            ).all()
            return {
                "enabled": self.store.settings.learning_enabled,
                "mechanism": "consented_supervised_threshold_calibration",
                "neural_weights_updated": False,
                "samples": dict(Counter(f"{s.role}:{s.state}:{s.split}" for s in samples)),
                "policies": [
                    {
                        "policy_id": p.policy_id,
                        "role": p.role,
                        "state": p.state,
                        "report": json.loads(self.store._decrypt(p.report)),
                    }
                    for p in policies
                ],
            }

    def run(self, tenant):
        if not self.runtime or not self.store.settings.learning_enabled:
            return {"status": "disabled"}
        return {
            role: self._refine(tenant, role, getattr(self.runtime, role))
            for role in ("face", "liveness")
        }

    def _refine(self, tenant, role, engine):
        # A single DB transaction fixes the snapshot and serializes promotion/deletion locally.
        with self.store.db.transaction() as db:
            if not self.store.db.in_memory:
                db.execute(text("BEGIN IMMEDIATE"))
            rows = db.scalars(
                select(LearningSampleRow)
                .where(
                    LearningSampleRow.tenant_id == tenant,
                    LearningSampleRow.role == role,
                    LearningSampleRow.model_fingerprint == engine.fingerprint,
                    LearningSampleRow.state == "labelled",
                    LearningSampleRow.retain_until > self.store.clock(),
                )
                .order_by(LearningSampleRow.created_at, LearningSampleRow.sample_id)
            ).all()
            samples, seen, seen_subjects = [], set(), {}
            for row in rows:
                data = json.loads(self.store._decrypt(row.payload))
                key = (data["pair"], data["label"], data.get("attack_type"))
                stratum = (data["label"], data.get("attack_type"))
                participants = set(data["pair"].split(":"))
                used = seen_subjects.setdefault(stratum, set())
                if key in seen or participants & used:
                    continue
                seen.add(key)
                used.update(participants)
                samples.append(
                    {
                        **data,
                        "sample_id": row.sample_id,
                        "split": row.split,
                        "positive": data["label"] in {"genuine", "live"},
                    }
                )
            split = {
                name: [s for s in samples if s["split"] == name]
                for name in ("calibration", "validation", "test")
            }
            counts = {
                name: rates(values, engine.calibration.threshold) for name, values in split.items()
            }
            for name, minimum in (
                ("calibration", self.gates.min_calibration_per_class),
                ("validation", self.gates.min_evaluation_per_class),
                ("test", self.gates.min_evaluation_per_class),
            ):
                if min(counts[name]["positive_count"], counts[name]["negative_count"]) < minimum:
                    return {"status": "collecting", "counts": counts}
            # Fix the first N samples per class/group/attack: new test samples cannot reset budget.
            panel, panel_counts = [], Counter()
            for sample in split["test"]:
                key = (sample["positive"], sample["device_group"], sample.get("attack_type"))
                if panel_counts[key] < self.gates.min_evaluation_per_class:
                    panel.append(sample)
                    panel_counts[key] += 1
            split["test"] = panel
            panel_hash = hashlib.sha256(
                json.dumps([s["sample_id"] for s in panel]).encode()
            ).hexdigest()
            snapshot = hashlib.sha256(
                json.dumps([s["sample_id"] for s in samples]).encode()
            ).hexdigest()
            existing = db.scalar(
                select(CalibrationRow).where(
                    CalibrationRow.tenant_id == tenant,
                    CalibrationRow.role == role,
                    CalibrationRow.model_fingerprint == engine.fingerprint,
                    CalibrationRow.snapshot_hash == snapshot,
                )
            )
            if existing:
                return {"status": "unchanged", "policy_id": existing.policy_id}
            attempts = len(
                list(
                    db.scalars(
                        select(CalibrationRow.policy_id).where(
                            CalibrationRow.tenant_id == tenant,
                            CalibrationRow.role == role,
                            CalibrationRow.model_fingerprint == engine.fingerprint,
                        )
                    )
                )
            )
            if attempts >= self.gates.max_attempts_per_test_panel:
                return {"status": "holdout_budget_exhausted"}
            candidates = sorted(
                {
                    engine.calibration.threshold,
                    *[
                        math.nextafter(s["score"], math.inf)
                        for s in split["calibration"]
                        if s["score"] < 1
                    ],
                }
            )
            acceptable = [(rates(split["calibration"], t), t) for t in candidates if -1 <= t <= 1]
            acceptable = [
                (r, t)
                for r, t in acceptable
                if r["false_accept_rate"] <= self.gates.max_false_accept_upper
            ]
            if not acceptable:
                return {"status": "no_safe_candidate"}
            _, threshold = min(
                acceptable,
                key=lambda item: (
                    item[0]["false_reject_rate"],
                    item[0]["false_accept_rate"],
                    abs(item[1] - engine.calibration.threshold),
                ),
            )
            active = self._active(db, tenant, role, engine.fingerprint)
            incumbent = (
                db.get(CalibrationRow, active.policy_id) if active and active.policy_id else None
            )
            report, failures = (
                {
                    "threshold": threshold,
                    "gates": self.gates.__dict__,
                    "metric_names": ["FMR", "FNMR"] if role == "face" else ["APCER", "BPCER"],
                },
                [],
            )
            improved = not self._valid_policy(db, incumbent)
            for name in ("validation", "test"):
                metrics, failed = evaluation(split[name], threshold, self.gates, role)
                report[name] = metrics
                failures.extend(f"{name}:{f}" for f in failed)
                if not improved or self._valid_policy(db, incumbent):
                    old, _ = evaluation(split[name], incumbent.threshold, self.gates, role)
                    for group, metric in metrics.items():
                        for rate in ("false_accept_rate", "false_reject_rate"):
                            if metric[rate] > old[group][rate]:
                                failures.append(f"{name}:{group}:regression:{rate}")
                            improved |= metric[rate] < old[group][rate]
            if incumbent and self._valid_policy(db, incumbent) and not improved:
                failures.append("no_measurable_improvement")
            policy_id = f"mth_cal_{uuid4().hex}"
            report["failures"] = failures
            policy = CalibrationRow(
                policy_id=policy_id,
                tenant_id=tenant,
                role=role,
                model_fingerprint=engine.fingerprint,
                state="failed" if failures else "passed",
                threshold=threshold,
                snapshot_hash=snapshot,
                test_panel_hash=panel_hash,
                report=self.store._encrypt(json.dumps(report)),
                created_at=self.store.clock(),
            )
            db.add(policy)
            db.add_all(
                CalibrationMemberRow(policy_id=policy_id, sample_id=s["sample_id"]) for s in samples
            )
            if not failures:
                if not active:
                    active = ActiveCalibrationRow(
                        tenant_id=tenant,
                        role=role,
                        model_fingerprint=engine.fingerprint,
                        generation=0,
                    )
                    db.add(active)
                active.policy_id = policy_id
                active.generation += 1
            self.store._event(
                db,
                Principal(tenant, "learning-worker", frozenset()),
                policy_id,
                "calibration_blocked" if failures else "calibration_promoted",
                uuid4().hex,
            )
            return {
                "status": "blocked" if failures else "promoted",
                "policy_id": policy_id,
                "failures": failures,
            }

    def rollback(self, tenant, role, policy_id, principal=None, request_id=None):
        with self.store.db.transaction() as db:
            if not self.store.db.in_memory:
                db.execute(text("BEGIN IMMEDIATE"))
            policy = db.get(CalibrationRow, policy_id)
            engine = getattr(self.runtime, role) if self.runtime else None
            if (
                not policy
                or policy.tenant_id != tenant
                or policy.role != role
                or not engine
                or policy.model_fingerprint != engine.fingerprint
                or not self._valid_policy(db, policy)
            ):
                raise MuthError(409, "policy_unavailable", "Versão inválida, expirada ou revogada.")
            active = self._active(db, tenant, role, engine.fingerprint)
            if not active:
                active = ActiveCalibrationRow(
                    tenant_id=tenant, role=role, model_fingerprint=engine.fingerprint, generation=0
                )
                db.add(active)
            active.policy_id = policy_id
            active.generation += 1
            if principal:
                self.store._event(db, principal, policy_id, "calibration_rollback", request_id)
            return {"policy_id": policy_id, "generation": active.generation}
