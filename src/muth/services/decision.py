from muth.domain import Checks, Outcome, VerificationStatus

POLICY_VERSION = "baseline-v1"


def decide(checks: Checks, *, demo: bool = False) -> tuple[VerificationStatus, list[str]]:
    """Política inicial explícita: falha rejeita; incerteza pede revisão; demo nunca aprova."""
    outcomes = [checks.document.outcome, checks.liveness.outcome, checks.face_match.outcome]
    reasons = [
        f"{name}:{reason}"
        for name in ("document", "liveness", "face_match")
        for reason in getattr(checks, name).reasons
    ]
    if demo:
        return VerificationStatus.REVIEW, ["demo_not_identity_verification", *reasons]
    if Outcome.FAIL in outcomes:
        return VerificationStatus.REJECTED, reasons
    if Outcome.INCONCLUSIVE in outcomes:
        return VerificationStatus.REVIEW, reasons
    return VerificationStatus.APPROVED, reasons
