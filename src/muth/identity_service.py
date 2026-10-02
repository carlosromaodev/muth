"""Transient extraction and 1:1 comparison against consented encrypted templates."""

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from muth.domain import Check, Outcome
from muth.engines.biometric import CaptureError


class IdentityRegistration(BaseModel):
    model_config = {"extra": "forbid"}
    status: Literal[
        "not_requested", "pending", "enrolled_provisional", "unavailable", "rejected", "deleted"
    ] = "not_requested"
    identity_id: str | None = Field(default=None, pattern=r"^mth_idn_[a-f0-9]{32}$")
    biometric_template_saved: bool = False
    authenticity_confirmed: Literal[False] = False
    retain_until: datetime | None = None
    identity_token: str | None = None
    explanation: str = "A identidade não foi guardada para reutilização."


class IdentityComparison(BaseModel):
    model_config = {"extra": "forbid"}
    identity_id: str
    authenticated: Literal[False] = False
    profile_status: Literal["provisional"] = "provisional"
    face_match: Check
    liveness: Check
    explanation: str = (
        "Comparação com registo provisório; ainda não permite autenticação de identidade."
    )


class IdentityService:
    def prepare(self, bundle, selfie, report):
        """Return private material separately; it must never become an HTTP response."""
        face, live = report.checks.face_match, report.checks.liveness
        core = getattr(bundle.face, "core", None)
        eligible = (
            core is not None
            and report.score.value is not None
            and bool(report.document_data.fields)
            and report.document_data.status != "unavailable"
            and report.status != "rejected"
            and face.score is not None
            and live.score is not None
            and face.score_kind == "cosine_similarity"
            and live.score_kind == "liveness_softmax"
            and face.score >= max(0.363, bundle.face.calibration.threshold)
            and live.score >= max(0.8, bundle.liveness.calibration.threshold)
            and "liveness_ensemble_disagreement" not in live.reasons
            and not any(
                c.outcome == Outcome.FAIL for c in (face, live, report.checks.document_front)
            )
        )
        if not eligible:
            return IdentityRegistration(
                status="unavailable",
                explanation="Os dados e sinais desta captura não permitem guardar a identidade.",
            ), None
        try:
            vector, _ = core.embedding(selfie)
        except CaptureError:
            return IdentityRegistration(
                status="unavailable",
                explanation="Recapture a selfie para guardar um template facial válido.",
            ), None
        return IdentityRegistration(status="pending"), (vector.tolist(), bundle.face.fingerprint)

    def compare(self, identity_id, template, fingerprint, bundle, selfie):
        liveness = bundle.liveness.assess(selfie)
        engine = bundle.face
        score, outcome = None, Outcome.INCONCLUSIVE
        reasons = ["identity_profile_provisional"]
        if fingerprint != getattr(engine, "fingerprint", None):
            reasons.append("identity_model_version_mismatch")
        elif not getattr(engine, "core", None):
            reasons.append("identity_engine_unavailable")
        else:
            try:
                vector, _ = engine.core.embedding(selfie)
                score = max(
                    -1.0,
                    min(
                        1.0, math.fsum(a * float(b) for a, b in zip(template, vector, strict=True))
                    ),
                )
                if engine.calibration.validated:
                    outcome = (
                        Outcome.PASS if score >= engine.calibration.threshold else Outcome.FAIL
                    )
                    reasons.append("face_match" if outcome == Outcome.PASS else "face_mismatch")
                else:
                    reasons.append("face_threshold_not_locally_calibrated")
            except CaptureError as exc:
                reasons.append(exc.reason)
        return IdentityComparison(
            identity_id=identity_id,
            face_match=Check(
                outcome=outcome,
                score=score,
                score_kind="cosine_similarity",
                reasons=reasons,
                provider="muth-sface-template",
                model_version="sface-template-v1",
                calibration_version=getattr(getattr(engine, "calibration", None), "version", None),
                model_fingerprint=getattr(engine, "fingerprint", None),
                threshold=getattr(getattr(engine, "calibration", None), "threshold", None),
            ),
            liveness=liveness,
        )
