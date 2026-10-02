"""Offline CPU evaluation from local labelled images, without changing policies.

Subjects and image hashes exist only during validation/evaluation. Reports contain
aggregate measurements: they never contain sample identifiers, filenames, images,
embeddings or individual scores.
"""

import hashlib
import math
import os
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from muth.config import Settings
from muth.domain import Check, Outcome
from muth.evaluation import _percentile, wilson
from muth.media import ImageInput, decode_image

Role = Literal["face", "liveness"]
Purpose = Literal["consented_evaluation", "research_fixture"]
DeviceGroup = Literal["android_entry", "android_modern", "ios", "web", "unknown"]
SUPPORTED_PAI = {"print", "screen_replay", "mask"}
UNKNOWN_SUBJECT = "subject-unknown"
OPAQUE_PATTERN = r"^[A-Za-z0-9_.:-]{1,80}$"


class DatasetSample(BaseModel):
    model_config = {"extra": "forbid"}
    role: Role
    split: Literal["calibration", "validation", "test"]
    selfie: str = Field(min_length=1, max_length=500)
    reference: str | None = Field(default=None, min_length=1, max_length=500)
    subject_id: str = Field(pattern=OPAQUE_PATTERN)
    reference_subject_id: str | None = Field(default=None, pattern=OPAQUE_PATTERN)
    label: Literal["genuine", "impostor", "live", "spoof"]
    device_group: DeviceGroup = "unknown"
    attack_type: Literal["print", "screen_replay", "mask", "injection", "other"] | None = None
    source_reference: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")

    @model_validator(mode="after")
    def coherent_labels(self):
        if self.role == "face":
            if (
                self.label not in {"genuine", "impostor"}
                or self.reference is None
                or self.reference_subject_id is None
                or self.attack_type is not None
            ):
                raise ValueError("A comparação facial exige referência e rótulo facial.")
            same_subject = self.subject_id == self.reference_subject_id
            if self.label == "genuine" and not same_subject:
                raise ValueError("Genuine exige a mesma identidade nos dois lados.")
            if self.label == "impostor" and same_subject:
                raise ValueError("Impostor exige identidades distintas e confirmadas.")
        elif (
            self.label not in {"live", "spoof"}
            or self.reference is not None
            or self.reference_subject_id is not None
            or (self.label == "spoof") != (self.attack_type is not None)
        ):
            raise ValueError("Liveness exige live ou spoof; apenas spoof tem attack_type.")
        return self


class BiometricDatasetManifest(BaseModel):
    model_config = {"extra": "forbid"}
    schema_version: Literal["biometric-dataset-v1"]
    dataset_id: str = Field(pattern=OPAQUE_PATTERN)
    purpose: Purpose
    authorization_file: str = Field(min_length=1, max_length=500)
    label_source: Literal["human_review", "upstream_example"]
    samples: list[DatasetSample] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def authorization_contract(self):
        if self.label_source == "upstream_example" and self.purpose != "research_fixture":
            raise ValueError("Exemplos públicos são apenas fixtures de investigação.")
        for sample in self.samples:
            subjects = {sample.subject_id, sample.reference_subject_id} - {None}
            if UNKNOWN_SUBJECT in subjects and (
                self.purpose != "research_fixture" or sample.label == "impostor"
            ):
                raise ValueError(
                    "Uma identidade desconhecida não confirma consentimento ou impostor."
                )
        return self


@dataclass(frozen=True)
class LoadedAsset:
    relative_path: str = field(repr=False)
    sha256: str = field(repr=False)


@dataclass(frozen=True)
class LoadedSample:
    metadata: DatasetSample = field(repr=False)
    selfie: LoadedAsset = field(repr=False)
    reference: LoadedAsset | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LoadedDataset:
    manifest: BiometricDatasetManifest = field(repr=False)
    root: Path = field(repr=False)
    samples: tuple[LoadedSample, ...] = field(repr=False)
    settings: Settings = field(repr=False)


def _contained_file(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError("O dataset exige caminhos relativos sem traversal.")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("O dataset exige ficheiros locais dentro da sua pasta.")
    return resolved


def _bounded_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit or not content:
        raise ValueError("Ficheiro vazio ou acima do limite de avaliação.")
    return content


def _contained_bytes(root: Path, name: str, limit: int) -> bytes:
    """Read beneath the resolved root without following swapped symlinks.

    File descriptors pin each directory while opening the next component. A
    path that was valid during resolution therefore cannot later redirect the
    evaluator through an attacker-controlled directory or final-file symlink.
    """
    resolved = _contained_file(root, name)
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("A avaliação exige suporte a leitura local sem seguir symlinks.")
    parts = resolved.relative_to(root).parts
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(root, directory_flags)
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("A avaliação exige ficheiros regulares locais.")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            content = stream.read(limit + 1)
    except OSError as exc:
        raise ValueError("Ficheiro alterado ou inacessível durante a leitura local.") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    if len(content) > limit or not content:
        raise ValueError("Ficheiro vazio ou acima do limite de avaliação.")
    return content


def _image(root: Path, name: str, settings: Settings) -> tuple[ImageInput, str]:
    content = _contained_bytes(root, name, settings.max_upload_bytes)
    try:
        image = decode_image(content, settings)
    except HTTPException as exc:
        raise ValueError("Imagem do dataset inválida ou acima dos limites.") from exc
    return image, hashlib.sha256(content).hexdigest()


def _media_settings() -> Settings:
    # Do not load the service's local database or credentials during evaluation.
    return Settings(
        _env_file=None,
        engine_mode="disabled",
        database_url="sqlite://",
        data_key="",
        api_key="",
        tenants=[],
        max_upload_bytes=5 * 1024 * 1024,
        max_image_pixels=12_000_000,
    )


def load_biometric_dataset(path: Path, *, settings: Settings | None = None) -> LoadedDataset:
    """Validate metadata, media bounds and disjoint subjects/images without models.

    Authorization and labels are local declarations; this function does not
    independently establish consent, identity or the provenance of human review.
    """
    settings = settings or _media_settings()
    manifest = BiometricDatasetManifest.model_validate_json(_bounded_bytes(path, 8 * 1024 * 1024))
    root = path.parent.resolve()
    evidence = _contained_bytes(root, manifest.authorization_file, 128 * 1024)
    if not evidence.strip():
        raise ValueError("A declaração de autorização está vazia.")
    subject_splits: dict[str, str] = {}
    image_splits: dict[str, str] = {}
    image_subjects: dict[str, str] = {}
    samples = []
    for metadata in manifest.samples:
        for subject in {metadata.subject_id, metadata.reference_subject_id} - {None}:
            previous = subject_splits.setdefault(subject, metadata.split)
            if previous != metadata.split:
                raise ValueError("Participante reutilizado entre splits; avaliação recusada.")
        assets = []
        image_subject_pairs = [(metadata.selfie, metadata.subject_id)]
        if metadata.reference is not None:
            image_subject_pairs.append((metadata.reference, metadata.reference_subject_id))
        for name, subject in image_subject_pairs:
            _, digest = _image(root, name, settings)
            previous = image_splits.setdefault(digest, metadata.split)
            if previous != metadata.split:
                raise ValueError("Imagem reutilizada entre splits; avaliação recusada.")
            if manifest.purpose == "consented_evaluation":
                known_subject = image_subjects.setdefault(digest, subject)
                if known_subject != subject:
                    raise ValueError("Imagem idêntica atribuída a identidades contraditórias.")
            assets.append(LoadedAsset(name, digest))
        samples.append(LoadedSample(metadata, assets[0], assets[1] if len(assets) > 1 else None))
    return LoadedDataset(manifest, root, tuple(samples), settings)


def _verified_image(dataset: LoadedDataset, asset: LoadedAsset) -> ImageInput:
    image, digest = _image(dataset.root, asset.relative_path, dataset.settings)
    if digest != asset.sha256:
        raise ValueError("Imagem alterada após validação; avaliação recusada.")
    return image


@dataclass(frozen=True)
class Observation:
    sample: DatasetSample = field(repr=False)
    check: Check = field(repr=False)
    latency_ms: float


NONBLOCKING_REASONS = {
    "face": {"face_threshold_not_locally_calibrated", "face_match", "face_mismatch"},
    "liveness": {
        "liveness_threshold_not_locally_calibrated",
        "rgb_pad_does_not_prove_capture_freshness",
        "rgb_pad_pass",
        "presentation_attack",
    },
}
QUALITY_REASONS = {
    "transparent_capture_unsupported",
    "face_too_small",
    "face_too_blurred",
    "face_pose_unsupported",
    "face_exposure_unsupported",
    "face_outside_frame",
    "invalid_face_landmarks",
    "multiple_faces_detected",
}
ACQUISITION_REASONS = QUALITY_REASONS | {
    "no_face_detected",
    "empty_face_crop",
    "invalid_capture_image",
    "invalid_liveness_crop",
    "empty_liveness_crop",
}
KNOWN_REASONS = (
    ACQUISITION_REASONS
    | set.union(*NONBLOCKING_REASONS.values())
    | {
        "liveness_ensemble_disagreement",
        "face_inference_failed",
        "invalid_detector_output",
        "invalid_embedding",
        "invalid_liveness_output",
        "liveness_inference_failed",
        "embedding_inference_failed",
    }
)


def _eligible(observation: Observation) -> bool:
    role, check = observation.sample.role, observation.check
    low = -1 if role == "face" else 0
    kind = "cosine_similarity" if role == "face" else "liveness_softmax"
    return (
        check.score is not None
        and math.isfinite(check.score)
        and low <= check.score <= 1
        and check.score_kind == kind
        and set(check.reasons) <= NONBLOCKING_REASONS[role]
    )


def _participants(observation: Observation) -> set[str]:
    sample = observation.sample
    return {sample.subject_id, sample.reference_subject_id} - {None}


def _rate(observations: list[Observation], errors: int, purpose: Purpose) -> dict:
    """Only display a binomial interval when labelled participants do not repeat."""
    total = len(observations)
    participants = [subject for row in observations for subject in _participants(row)]
    repeated = len(participants) != len(set(participants))
    unknown = UNKNOWN_SUBJECT in participants
    independent_design = purpose == "consented_evaluation" and not repeated and not unknown
    result = wilson(errors, total)
    if not independent_design:
        result["ci95"] = None
    result["independent_participants_design"] = independent_design if total else None
    result["repeated_participants"] = repeated
    result["interval_assumption"] = (
        "Nonrepeated labelled participants; residual device/environment correlation unassessed."
        if independent_design and total
        else "Binomial interval withheld: research fixture, unknown or repeated participants."
    )
    return result


def summarize_observations(
    observations: list[Observation], threshold: float, *, purpose: Purpose = "consented_evaluation"
) -> dict:
    """Aggregate one role, preserving unscored/abstained trials and denominators."""
    if not observations or len({row.sample.role for row in observations}) != 1:
        raise ValueError("O agregado deve conter exemplos de um único motor.")
    role = observations[0].sample.role
    if not math.isfinite(threshold) or not (-1 if role == "face" else 0) <= threshold <= 1:
        raise ValueError("Limiar de avaliação inválido.")
    if any(not math.isfinite(row.latency_ms) or row.latency_ms < 0 for row in observations):
        raise ValueError("Latência de avaliação inválida.")
    eligible = [row for row in observations if _eligible(row)]
    positive_label, negative_label = (
        ("genuine", "impostor") if role == "face" else ("live", "spoof")
    )
    positive = [row for row in eligible if row.sample.label == positive_label]
    negative = [
        row
        for row in eligible
        if row.sample.label == negative_label
        and (role == "face" or row.sample.attack_type in SUPPORTED_PAI)
    ]
    false_accept = sum(row.check.score >= threshold for row in negative)
    false_reject = sum(row.check.score < threshold for row in positive)
    attempted_positive = [row for row in observations if row.sample.label == positive_label]
    diagnostic_non_accept = sum(
        not _eligible(row) or row.check.score < threshold for row in attempted_positive
    )
    engine_non_accept = sum(row.check.outcome != Outcome.PASS for row in attempted_positive)
    subjects = {subject for row in observations for subject in _participants(row)}
    reasons = Counter(
        reason if reason in KNOWN_REASONS else "other_engine_reason"
        for row in observations
        for reason in set(row.check.reasons)
    )
    baseline_reason = (
        "face_threshold_not_locally_calibrated"
        if role == "face"
        else "liveness_threshold_not_locally_calibrated"
    )
    metrics = {
        "fmr" if role == "face" else "apcer_supported_pai": _rate(negative, false_accept, purpose),
        "fnmr" if role == "face" else "bpcer": _rate(positive, false_reject, purpose),
        "positive_diagnostic_non_accept": _rate(attempted_positive, diagnostic_non_accept, purpose),
        "positive_engine_non_accept": _rate(attempted_positive, engine_non_accept, purpose),
    }
    if role == "liveness":
        per_attack = []
        for attack in sorted({row.sample.attack_type for row in negative}):
            attack_rows = [row for row in negative if row.sample.attack_type == attack]
            rate = _rate(
                attack_rows, sum(row.check.score >= threshold for row in attack_rows), purpose
            )
            per_attack.append((attack, rate))
        worst = max(per_attack, key=lambda item: item[1]["rate"], default=None)
        metrics["worst_supported_pai_apcer"] = {
            "rate": worst[1]["rate"] if worst else None,
            "attack_type": worst[0] if worst else None,
            "errors": worst[1]["errors"] if worst else 0,
            "total": worst[1]["total"] if worst else 0,
            "observed_pai_count": len(per_attack),
            "ci95": None,
            "selection_note": (
                "Maximum observed rate among threshold-evaluable supported PAI categories; "
                "descriptive selection only, without a combined confidence interval."
            ),
        }
    class_counts = {}
    for label in (positive_label, negative_label):
        rows = [row for row in observations if row.sample.label == label]
        class_counts[label] = {
            "attempted": len(rows),
            "threshold_evaluable": sum(_eligible(row) for row in rows),
            "unscored": sum(row.check.score is None for row in rows),
            "diagnostic_abstentions": sum(not _eligible(row) for row in rows),
        }
    return {
        "attempted": len(observations),
        "distinct_known_participants": len(subjects - {UNKNOWN_SUBJECT}),
        "unknown_participant_trials": sum(
            UNKNOWN_SUBJECT in _participants(row) for row in observations
        ),
        "scored": sum(row.check.score is not None for row in observations),
        "threshold_evaluable": len(eligible),
        "unscored": sum(row.check.score is None for row in observations),
        "engine_abstentions": sum(
            row.check.outcome == Outcome.INCONCLUSIVE for row in observations
        ),
        "baseline_policy_abstentions": sum(
            row.check.outcome == Outcome.INCONCLUSIVE and baseline_reason in row.check.reasons
            for row in observations
        ),
        "diagnostic_abstentions": len(observations) - len(eligible),
        "quality_failures": sum(
            bool(set(row.check.reasons) & QUALITY_REASONS) for row in observations
        ),
        "acquisition_failures": sum(
            row.check.score is None and bool(set(row.check.reasons) & ACQUISITION_REASONS)
            for row in observations
        ),
        "unscored_other_failures": sum(
            row.check.score is None and not set(row.check.reasons) & ACQUISITION_REASONS
            for row in observations
        ),
        "unsupported_attack_trials": sum(
            row.sample.label == "spoof" and row.sample.attack_type not in SUPPORTED_PAI
            for row in observations
        ),
        "class_counts": class_counts,
        "metrics": metrics,
        "reason_counts": dict(sorted(reasons.items())),
        "latency_ms": {
            "p50": _percentile([row.latency_ms for row in observations], 0.5),
            "p95": _percentile([row.latency_ms for row in observations], 0.95),
        },
        "sample_warning": min(len(positive), len(negative)) < 30,
        "attack_coverage_certified": False,
    }


def _group_report(observations: list[Observation], threshold: float, purpose: Purpose) -> dict:
    result = summarize_observations(observations, threshold, purpose=purpose)
    result["by_device"] = {
        group: summarize_observations(
            [row for row in observations if row.sample.device_group == group],
            threshold,
            purpose=purpose,
        )
        for group in sorted({row.sample.device_group for row in observations})
    }
    if observations[0].sample.role == "liveness":
        result["by_attack_type"] = {}
        for attack in sorted({row.sample.attack_type for row in observations} - {None}):
            rows = [row for row in observations if row.sample.attack_type == attack]
            aggregate = summarize_observations(rows, threshold, purpose=purpose)
            aggregate["rgb_pad_supported_attack_category"] = attack in SUPPORTED_PAI
            if attack not in SUPPORTED_PAI:
                eligible = [row for row in rows if _eligible(row)]
                aggregate["unsupported_threshold_diagnostic"] = _rate(
                    eligible, sum(row.check.score >= threshold for row in eligible), purpose
                )
                aggregate["unsupported_threshold_diagnostic"]["ci95"] = None
                aggregate["unsupported_threshold_diagnostic"]["interval_assumption"] = (
                    "Unsupported attack: descriptive threshold diagnostic, no PAD coverage claim."
                )
            result["by_attack_type"][attack] = aggregate
    return result


def benchmark_biometrics(
    dataset: Path,
    model_manifest: Path,
    *,
    face_threshold: float = 0.363,
    liveness_threshold: float = 0.8,
) -> dict:
    """Run both CPU engines on verified images, at fixed diagnostic thresholds.

    Calibration, validation and test reports stay separate. No threshold is fitted
    and no production policy or model is created, promoted or modified.
    """
    if (
        not math.isfinite(face_threshold)
        or not -1 <= face_threshold <= 1
        or not math.isfinite(liveness_threshold)
        or not 0 <= liveness_threshold <= 1
    ):
        raise ValueError("Limiares fixos de avaliação inválidos.")
    loaded = load_biometric_dataset(dataset)
    # Optional dependencies are required only for actual inference, not validation.
    from muth.engines.biometric import BiometricRuntime

    runtime = BiometricRuntime(model_manifest)
    bundle = runtime.bundle()
    observations = []
    for sample in loaded.samples:
        selfie = _verified_image(loaded, sample.selfie)
        reference = _verified_image(loaded, sample.reference) if sample.reference else None
        started = time.monotonic()
        check = (
            bundle.face.compare(reference, selfie)
            if sample.metadata.role == "face"
            else bundle.liveness.assess(selfie)
        )
        observations.append(
            Observation(sample.metadata, check, (time.monotonic() - started) * 1000)
        )
    thresholds = {"face": face_threshold, "liveness": liveness_threshold}
    purpose = loaded.manifest.purpose
    splits = {}
    for split in ("calibration", "validation", "test"):
        split_rows = [row for row in observations if row.sample.split == split]
        if split_rows:
            splits[split] = {
                role: _group_report(
                    [row for row in split_rows if row.sample.role == role],
                    thresholds[role],
                    purpose,
                )
                for role in sorted({row.sample.role for row in split_rows})
            }
    return {
        "schema_version": "biometric-benchmark-v1",
        "purpose": purpose,
        "label_source": loaded.manifest.label_source,
        "authorization_independently_verified": False,
        "promotion_allowed": False,
        "thresholds": thresholds,
        "thresholds_fitted": False,
        "comparison": "score >= fixed_threshold; scored baseline abstentions are diagnostic only",
        "model_fingerprints": {
            "face": runtime.face.fingerprint,
            "liveness": runtime.liveness.fingerprint,
        },
        "splits": splits,
        "limitations": [
            "This report never authorizes policy promotion or certifies production accuracy.",
            "Authorization and human labels are declarations, not independently verified evidence.",
            "False match/PAD rates use threshold-evaluable scores; acquisition failures and "
            "abstentions are reported separately. Positive diagnostic non-accept applies the "
            "fixed score threshold and acquisition/quality blocks; engine non-accept counts all "
            "outcomes other than PASS, including baseline policy abstention.",
            "Baseline threshold abstention with a valid score is not acquisition failure.",
            "Participant-disjoint splits and image hashes reduce leakage, but cannot detect "
            "transformed duplicates or different photos of renamed identities. Identical image "
            "bytes cannot be assigned contradictory known subjects in consented evaluation.",
            "Wilson intervals are withheld for repeated/unknown participants and public fixtures; "
            "unique participants alone do not establish independence from devices or environments.",
            "RGB PAD does not prove camera freshness or resist injection; injection and other "
            "attacks contribute no evidence of supported PAD coverage.",
            "Research fixtures demonstrate inference only, not accuracy for Angola or Africa.",
            "Latency measures CPU engine calls, excluding dataset decoding and model startup.",
        ],
    }
