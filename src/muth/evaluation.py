import csv
import hashlib
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class Asset(BaseModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelManifest(BaseModel):
    model_config = {"extra": "forbid"}
    model_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    role: Literal["face", "liveness", "ocr"]
    permitted_use: Literal["research", "commercial"]
    license_reference: str = Field(min_length=1)
    license_evidence_file: str
    assets: list[Asset] = Field(min_length=1)


def _local_path(root: Path, name: str) -> Path:
    path = Path(name)
    resolved = (root / path).resolve()
    if path.is_absolute() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("O manifesto deve referenciar ficheiros locais dentro da sua pasta.")
    return resolved


def validate_manifest(path: Path, *, require_commercial: bool = False) -> dict:
    manifest = ModelManifest.model_validate_json(path.read_text())
    root = path.parent.resolve()
    if require_commercial and manifest.permitted_use != "commercial":
        raise ValueError("O manifesto não declara utilização comercial.")
    evidence = _local_path(root, manifest.license_evidence_file)
    if not evidence.read_text().strip():
        raise ValueError("A evidência da revisão da licença está vazia.")
    for asset in manifest.assets:
        file = _local_path(root, asset.path)
        with file.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != asset.sha256:
            raise ValueError("Checksum de um artefacto não corresponde ao manifesto.")
    return {
        "model_id": manifest.model_id,
        "version": manifest.version,
        "role": manifest.role,
        "permitted_use": manifest.permitted_use,
        "integrity_valid": True,
        "legal_approval": False,
    }


def wilson(errors: int, total: int) -> dict:
    if total == 0:
        return {"errors": 0, "total": 0, "rate": None, "ci95": None}
    z = 1.959963984540054
    p = errors / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {
        "errors": errors,
        "total": total,
        "rate": p,
        "ci95": [max(0, center - margin), min(1, center + margin)],
    }


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * p
    left = int(position)
    right = min(left + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (position - left)


def _rates(rows, threshold):
    genuine = [r for r in rows if r["label"] == "genuine"]
    impostor = [r for r in rows if r["label"] == "impostor"]
    return {
        "fmr": wilson(sum(r["score"] >= threshold for r in impostor), len(impostor)),
        "fnmr": wilson(sum(r["score"] < threshold for r in genuine), len(genuine)),
        "sample_warning": len(genuine) < 30 or len(impostor) < 30,
    }


def evaluate_face(path: Path, threshold: float) -> dict:
    if not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError("Limiar de similaridade coseno inválido.")
    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        required = {"split", "label", "score", "latency_ms"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError("CSV deve conter split,label,score,latency_ms.")
        rows = []
        for raw in reader:
            if raw["split"] not in {"calibration", "test"}:
                raise ValueError("Split deve ser calibration ou test.")
            if raw["split"] == "calibration":
                continue
            score, latency = float(raw["score"]), float(raw["latency_ms"])
            if raw["label"] not in {"genuine", "impostor"}:
                raise ValueError("Label facial inválida.")
            if not math.isfinite(score) or not -1 <= score <= 1:
                raise ValueError("Similaridade coseno inválida.")
            if not math.isfinite(latency) or latency < 0:
                raise ValueError("Latência inválida.")
            group = raw.get("group") or "unspecified"
            if len(group) > 80:
                raise ValueError("Nome de grupo demasiado longo.")
            rows.append({"label": raw["label"], "score": score, "latency": latency, "group": group})
    if not rows:
        raise ValueError("O conjunto de teste está vazio.")
    latencies = [r["latency"] for r in rows]
    return {
        "schema_version": "face-evaluation-v1",
        "threshold": threshold,
        "comparison": "cosine_similarity >= threshold",
        "test_rows": len(rows),
        **_rates(rows, threshold),
        "latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
        "groups": {
            group: _rates([r for r in rows if r["group"] == group], threshold)
            for group in sorted({r["group"] for r in rows})
        },
        "limitations": [
            "Pair rates are not liveness metrics or operational fraud rates.",
            "Wilson intervals assume independent trials; "
            "subject correlation needs additional analysis.",
            "Threshold must be selected on separate calibration identities.",
        ],
    }
