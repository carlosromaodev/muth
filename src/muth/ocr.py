"""Bounded local Tesseract OCR and conservative BI/passport field parsing.

Only structured, labelled fields leave this module. Image files exist briefly in
an owner-only temporary directory; OCR text and subprocess diagnostics are never
logged or included in the verification response.
"""

import csv
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO, StringIO
from pathlib import Path

from PIL import Image, ImageOps

from muth.document_models import DocumentConflict, DocumentData, DocumentField
from muth.media import ImageInput

_MAX_INPUT_BYTES = 32 * 1024 * 1024
_MAX_INPUT_PIXELS = 32_000_000
_MAX_LINES = 2_000
_MAX_FIELD_LENGTH = 256


def _fold(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value).upper()
        if not unicodedata.combining(character)
    )


_ALIASES = {
    "name": ("NOME COMPLETO", "FULL NAME", "NOME", "NAME"),
    "document_number": (
        "NUMERO DO BILHETE DE IDENTIDADE",
        "NUMERO DO DOCUMENTO",
        "DOCUMENT NUMBER",
        "NUMERO DO PASSAPORTE",
        "PASSPORT NUMBER",
        "NUMERO DO BI",
        "N.º DO BI",
        "Nº DO BI",
        "N.º",
        "Nº",
        "BILHETE N",
        "BI N",
        "N BI",
        "NUMERO",
    ),
    "birth_date": ("DATA DE NASCIMENTO", "DATE OF BIRTH", "NASCIMENTO", "BIRTH DATE"),
    "expiry_date": (
        "DATA DE VALIDADE",
        "DATA DE EXPIRACAO",
        "DATE OF EXPIRY",
        "EXPIRY DATE",
        "VALIDO ATE",
        "VALIDADE",
        "EXPIRY",
    ),
    "sex": ("SEXO", "SEX", "GENDER"),
    "nationality": ("NACIONALIDADE", "NATIONALITY"),
    "parentage": ("FILIACAO", "PARENTAGE"),
    "father_name": ("NOME DO PAI", "FATHER NAME", "PAI"),
    "mother_name": ("NOME DA MAE", "MOTHER NAME", "MAE"),
}
_LABELS = sorted(
    [(_fold(alias), key) for key, aliases in _ALIASES.items() for alias in aliases],
    key=lambda item: len(item[0]),
    reverse=True,
)
_LABEL_PATTERN = re.compile(
    r"(?<![A-Z0-9])(" + "|".join(re.escape(alias) for alias, _ in _LABELS) + r")(?=\s|:|=|$)"
)
_KEY_BY_LABEL = dict(_LABELS)
_BI_NUMBER = re.compile(r"^[0-9]{9}[A-Z]{2}[0-9]{3}$")


def _clean(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return " ".join(
        "".join(
            character for character in value if not unicodedata.category(character).startswith("C")
        )
        .strip()
        .split()
    )


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float | None = None


@dataclass
class _SideReading:
    fields: dict[str, DocumentField] = field(default_factory=dict)
    document_type: str = "unknown"
    issuing_country: str | None = None
    reasons: list[str] = field(default_factory=list)
    mrz_checks: dict[str, bool] = field(default_factory=dict)


def _tsv_lines(content: str) -> list[OCRLine]:
    grouped: dict[tuple[str, ...], list[tuple[str, float]]] = {}
    reader = csv.DictReader(StringIO(content), delimiter="\t")
    for row in reader:
        if row.get("level") != "5":
            continue
        text = _clean(row.get("text") or "")
        if not text:
            continue
        try:
            confidence = float(row.get("conf") or "-1") / 100
        except ValueError:
            continue
        if not 0 <= confidence <= 1:
            continue
        key = tuple(
            row.get(name) or "" for name in ("page_num", "block_num", "par_num", "line_num")
        )
        if key not in grouped and len(grouped) >= _MAX_LINES:
            break
        grouped.setdefault(key, []).append((text, confidence))
    return [
        OCRLine(
            " ".join(word for word, _ in words)[:2048],
            sum(len(word) * confidence for word, confidence in words)
            / sum(len(word) for word, _ in words),
        )
        for words in grouped.values()
    ]


def _date_field(value: str, key: str) -> tuple[str, str]:
    # Ambiguous or incomplete dates remain as read; no missing date component is inferred.
    candidate = value.replace(".", "/").replace("-", "/")
    for pattern in ("%d/%m/%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(candidate, pattern).date()
        except ValueError:
            continue
        valid = 1900 <= parsed.year <= 2100
        if key == "birth_date":
            valid = valid and parsed <= date.today()
        return parsed.isoformat(), "valid" if valid else "invalid"
    return value, "invalid"


def _normalise_field(key: str, value: str, document_type: str) -> tuple[str, str] | None:
    value = _clean(value.strip(" :;="))
    if not value or len(value) > _MAX_FIELD_LENGTH or any(char in value for char in "<>\\{}"):
        return None
    if key in {"birth_date", "expiry_date"}:
        return _date_field(value, key)
    if key == "document_number":
        number = re.sub(r"\s+", "", value.upper())
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{3,24}", number):
            return None
        if document_type == "bi":
            return number, "valid" if _BI_NUMBER.fullmatch(number) else "invalid"
        return number, "unvalidated"
    if key == "sex":
        mapping = {"M": "M", "F": "F", "X": "X", "MASCULINO": "M", "FEMININO": "F"}
        return mapping.get(_fold(value), value), "valid" if _fold(value) in mapping else "invalid"
    if not any(unicodedata.category(character).startswith("L") for character in value):
        return None
    if any(
        not (unicodedata.category(character)[0] in {"L", "M"} or character in " '-.,/")
        for character in value
    ):
        return None
    return value, "valid" if key in {"name", "father_name", "mother_name"} else "unvalidated"


def _add_field(reading: _SideReading, key: str, candidate: DocumentField) -> None:
    previous = reading.fields.get(key)
    if previous is None:
        if f"document_{key}_conflict_same_side" not in reading.reasons:
            reading.fields[key] = candidate
    elif _fold(previous.value) != _fold(candidate.value):
        reading.fields.pop(key)
        reading.reasons.append(f"document_{key}_conflict_same_side")
    elif (candidate.confidence or 0) > (previous.confidence or 0):
        reading.fields[key] = candidate


def _checksum(value: str, check: str) -> bool:
    if not check.isdigit():
        return False
    total = 0
    for index, character in enumerate(value):
        if character == "<":
            number = 0
        elif "0" <= character <= "9":
            number = ord(character) - ord("0")
        elif "A" <= character <= "Z":
            number = ord(character) - ord("A") + 10
        else:
            return False
        total += number * (7, 3, 1)[index % 3]
    return total % 10 == int(check)


def _mrz_date(value: str, key: str) -> tuple[str, str]:
    if not re.fullmatch(r"[0-9]{6}", value):
        return value, "invalid"
    year = 2000 + int(value[:2])
    if key == "birth_date" and year > date.today().year:
        year -= 100
    return _date_field(f"{year:04d}-{value[2:4]}-{value[4:6]}", key)


def _parse_mrz(reading: _SideReading, lines: list[OCRLine], side: str) -> None:
    candidates = [(re.sub(r"\s+", "", _fold(line.text)), line.confidence) for line in lines]
    for index in range(len(candidates) - 1):
        first, first_confidence = candidates[index]
        second, second_confidence = candidates[index + 1]
        if not (
            first.startswith("P<")
            and len(first) == len(second) == 44
            and re.fullmatch(r"[A-Z0-9<]{44}", first)
            and re.fullmatch(r"[A-Z0-9<]{44}", second)
        ):
            continue
        reading.document_type = "passport"
        if first[2:5] == "AGO":
            reading.issuing_country = "AO"
        checks = {
            "document_number": _checksum(second[:9], second[9]),
            "birth_date": _checksum(second[13:19], second[19]),
            "expiry_date": _checksum(second[21:27], second[27]),
            "composite": _checksum(second[:10] + second[13:20] + second[21:43], second[43]),
        }
        reading.mrz_checks.update(checks)
        reading.reasons.append(
            "mrz_checkdigits_valid" if all(checks.values()) else "mrz_checkdigits_failed"
        )
        confidence = min(first_confidence or 0, second_confidence or 0) or None
        surname, _, given_names = first[5:].partition("<<")
        name = _clean(given_names.replace("<", " ") + " " + surname.replace("<", " "))
        values = {
            "name": (name, "unvalidated"),
            "document_number": (
                second[:9].rstrip("<"),
                "valid" if checks["document_number"] else "invalid",
            ),
            "birth_date": _mrz_date(second[13:19], "birth_date"),
            "expiry_date": _mrz_date(second[21:27], "expiry_date"),
            "sex": (second[20].replace("<", "X"), "valid" if second[20] in "MFX<" else "invalid"),
            "nationality": (second[10:13], "unvalidated"),
        }
        for key, (value, validation) in values.items():
            if not value:
                continue
            if key in {"birth_date", "expiry_date"} and not checks[key]:
                validation = "invalid"
            _add_field(
                reading,
                key,
                DocumentField(
                    value=value,
                    confidence=confidence,
                    source_side=side,
                    validation=validation,
                    source="mrz",
                ),
            )
        break


def _parse_side(lines: list[OCRLine], side: str) -> _SideReading:
    reading = _SideReading()
    folded = "\n".join(_fold(line.text) for line in lines)
    if (
        "REPUBLICA DE ANGOLA" in folded
        or "REPUBLIC OF ANGOLA" in folded
        or any(_fold(line.text).strip() == "ANGOLA" for line in lines)
    ):
        reading.issuing_country = "AO"
    if "BILHETE DE IDENTIDADE" in folded or "IDENTITY CARD" in folded:
        reading.document_type = "bi"
    elif "PASSAPORTE" in folded or "PASSPORT" in folded:
        reading.document_type = "passport"
    for index, line in enumerate(lines[:_MAX_LINES]):
        normal = _fold(line.text)
        matches = [
            match
            for match in _LABEL_PATTERN.finditer(normal)
            if not match.start() or line.text[match.end() :].lstrip().startswith((":", "="))
        ]
        for match_index, match in enumerate(matches):
            end = (
                matches[match_index + 1].start()
                if match_index + 1 < len(matches)
                else len(line.text)
            )
            value = line.text[match.end() : end].strip(" :;=")
            confidence = line.confidence
            if not value and index + 1 < len(lines):
                following = lines[index + 1]
                if not _LABEL_PATTERN.search(_fold(following.text)):
                    value, confidence = following.text, following.confidence
            key = _KEY_BY_LABEL[match.group()]
            normalised = _normalise_field(key, value, reading.document_type)
            if normalised is not None:
                value, validation = normalised
                _add_field(
                    reading,
                    key,
                    DocumentField(
                        value=value,
                        confidence=confidence,
                        source_side=side,
                        validation=validation,
                    ),
                )
    _parse_mrz(reading, lines, side)
    return reading


def parse_document_text(
    front: str | list[OCRLine],
    back: str | list[OCRLine],
    *,
    ocr_version: str | None = None,
    reasons: list[str] | None = None,
) -> DocumentData:
    """Parse labelled text; exposed for reproducible, non-image parser benchmarks."""
    readings = []
    for side, text in (("front", front), ("back", back)):
        lines = [OCRLine(line) for line in text.splitlines()] if isinstance(text, str) else text
        readings.append(_parse_side(lines[:_MAX_LINES], side))
    return _combine_readings(readings, ocr_version=ocr_version, reasons=reasons)


def _combine_readings(
    readings: list[_SideReading],
    *,
    ocr_version: str | None = None,
    reasons: list[str] | None = None,
) -> DocumentData:
    first, second = readings
    fields = dict(first.fields)
    conflicts = []
    combined_reasons = list(reasons or []) + first.reasons + second.reasons
    for key, candidate in second.fields.items():
        previous = fields.get(key)
        if previous is None:
            fields[key] = candidate
        elif _fold(previous.value) == _fold(candidate.value):
            available = [
                item for item in (previous.confidence, candidate.confidence) if item is not None
            ]
            fields[key] = previous.model_copy(
                update={"source_side": "both", "confidence": min(available) if available else None}
            )
        else:
            conflicts.append(
                DocumentConflict(field=key, front_value=previous.value, back_value=candidate.value)
            )
            fields.pop(key)
    document_type = (
        first.document_type if first.document_type != "unknown" else second.document_type
    )
    if first.document_type != second.document_type and "unknown" not in {
        first.document_type,
        second.document_type,
    }:
        document_type = "unknown"
        combined_reasons.append("document_type_conflict")
    if conflicts:
        combined_reasons.append("document_fields_conflicting")
    for key in list(fields):
        if any(
            f"document_{key}_conflict_{suffix}" in combined_reasons
            for suffix in ("same_side", "layout")
        ):
            fields.pop(key)
    expiry = fields.get("expiry_date")
    if expiry is not None and expiry.validation == "valid":
        if date.fromisoformat(expiry.value) < date.today():
            combined_reasons.append("document_expired")
    if any(item.validation == "invalid" for item in fields.values()):
        combined_reasons.append("document_fields_invalid")
    status = "unavailable"
    if fields:
        complete = {"name", "document_number", "birth_date"}.issubset(fields)
        status = (
            "extracted"
            if complete
            and document_type != "unknown"
            and not conflicts
            and not any(item.validation == "invalid" for item in fields.values())
            and not any(reason.endswith("conflict_same_side") for reason in combined_reasons)
            and not any(reason.endswith("conflict_layout") for reason in combined_reasons)
            and "mrz_checkdigits_failed" not in combined_reasons
            and not any(
                reason in combined_reasons
                for reason in ("ocr_timeout", "ocr_process_failed", "ocr_output_limit_exceeded")
            )
            else "partial"
        )
    else:
        combined_reasons.append("document_fields_not_read")
    combined_reasons.append("document_authenticity_not_verified")
    return DocumentData(
        status=status,
        document_type=document_type,
        issuing_country=first.issuing_country or second.issuing_country,
        fields=fields,
        conflicts=conflicts,
        reasons=list(dict.fromkeys(combined_reasons)),
        ocr_version=ocr_version,
        mrz_checks=first.mrz_checks or second.mrz_checks,
    )


def _needs_sparse_layout(reading: _SideReading, side: str) -> bool:
    if any(item.validation == "invalid" for item in reading.fields.values()):
        return True
    if len(reading.fields) < 3:
        return True
    if side == "front":
        return not {"name", "document_number", "birth_date", "expiry_date", "sex"}.issubset(
            reading.fields
        )
    return False


def _merge_layout_readings(primary: _SideReading, sparse: _SideReading) -> _SideReading:
    """Recover a clean reading; retain uncertainty when plausible readings differ."""
    merged = _SideReading(
        fields=dict(primary.fields),
        document_type=primary.document_type,
        issuing_country=primary.issuing_country or sparse.issuing_country,
        reasons=list(dict.fromkeys(primary.reasons + sparse.reasons + ["ocr_sparse_layout_used"])),
        mrz_checks=primary.mrz_checks or sparse.mrz_checks,
    )
    if merged.document_type == "unknown":
        merged.document_type = sparse.document_type
    elif sparse.document_type not in {"unknown", merged.document_type}:
        merged.document_type = "unknown"
        merged.reasons.append("document_type_conflict_layout")
    if sparse.mrz_checks and all(sparse.mrz_checks.values()):
        merged.mrz_checks = sparse.mrz_checks
        if not primary.mrz_checks or not all(primary.mrz_checks.values()):
            merged.reasons = [
                reason for reason in merged.reasons if reason != "mrz_checkdigits_failed"
            ]
    # "Unvalidated" is still plausible text, including MRZ names without a
    # name checksum. It must conflict with a different plausible visual name.
    rank = {"valid": 1, "unvalidated": 1, "invalid": 0}
    for key, candidate in sparse.fields.items():
        if f"document_{key}_conflict_same_side" in merged.reasons:
            merged.fields.pop(key, None)
            continue
        previous = merged.fields.get(key)
        if previous is None:
            merged.fields[key] = candidate
        elif _fold(previous.value) == _fold(candidate.value):
            if rank[candidate.validation] > rank[previous.validation] or (
                rank[candidate.validation] == rank[previous.validation]
                and (candidate.confidence or 0) > (previous.confidence or 0)
            ):
                merged.fields[key] = candidate
        elif rank[candidate.validation] > rank[previous.validation]:
            merged.fields[key] = candidate
            merged.reasons.append(f"ocr_layout_recovered_{key}")
        elif rank[candidate.validation] == rank[previous.validation]:
            merged.fields.pop(key)
            merged.reasons.append(f"document_{key}_conflict_layout")
    return merged


class _OCRFailure(Exception):
    pass


def _run_bounded(arguments: list[str], timeout: float, limit: int, cwd: str | None = None) -> bytes:
    """Read stdout incrementally and kill the child on timeout or output overflow."""
    environment = dict(os.environ)
    environment.update({"OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"})
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        cwd=cwd,
        env=environment,
    )
    assert process.stdout is not None
    content = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _OCRFailure("ocr_timeout")
                if not selector.select(remaining):
                    raise _OCRFailure("ocr_timeout")
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > limit:
                    raise _OCRFailure("ocr_output_limit_exceeded")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _OCRFailure("ocr_timeout")
        if process.wait(timeout=remaining) != 0:
            raise _OCRFailure("ocr_process_failed")
        return bytes(content)
    except subprocess.TimeoutExpired as exc:
        raise _OCRFailure("ocr_timeout") from exc
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


class TesseractDocumentEngine:
    def __init__(
        self,
        *,
        enabled: bool = True,
        executable: str = "tesseract",
        languages: str = "por+eng",
        timeout_seconds: float = 12,
        max_output_bytes: int = 1024 * 1024,
    ):
        if not 0.05 <= timeout_seconds <= 120:
            raise ValueError("Timeout OCR fora dos limites.")
        if not 4096 <= max_output_bytes <= 4 * 1024 * 1024:
            raise ValueError("Limite de saída OCR inválido.")
        if not re.fullmatch(r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", languages):
            raise ValueError("Lista de idiomas OCR inválida.")
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.version = None
        self.languages = ""
        self.reasons = []
        self.executable = shutil.which(executable) if enabled else None
        if not enabled:
            self.reasons.append("ocr_disabled")
        elif not self.executable:
            self.reasons.append("ocr_engine_unavailable")
        else:
            try:
                version = _run_bounded([self.executable, "--version"], 2, 16384).decode(
                    "utf-8", "replace"
                )
                match = re.search(r"^tesseract\s+([0-9][A-Za-z0-9.+-]{0,40})", version)
                if not match:
                    raise _OCRFailure("ocr_engine_probe_failed")
                self.version = match.group(1)
                output = _run_bounded([self.executable, "--list-langs"], 2, 16384).decode(
                    "utf-8", "replace"
                )
                available = {
                    line.strip()
                    for line in output.splitlines()
                    if re.fullmatch(r"[A-Za-z0-9_]+", line.strip())
                }
                requested = languages.split("+")
                chosen = [
                    language
                    for language in requested
                    if language in available and language != "osd"
                ]
                if not chosen and "eng" in available:
                    chosen = ["eng"]
                if not chosen:
                    raise _OCRFailure("ocr_languages_unavailable")
                self.languages = "+".join(chosen)
                if chosen != requested:
                    self.reasons.append(
                        "ocr_language_fallback_eng"
                        if chosen == ["eng"]
                        else "ocr_language_partial_availability"
                    )
            except (OSError, _OCRFailure) as exc:
                self.executable = None
                self.reasons.append(
                    str(exc) if isinstance(exc, _OCRFailure) else "ocr_engine_probe_failed"
                )

    def extract(self, front: ImageInput, back: ImageInput) -> DocumentData:
        if not self.executable:
            return parse_document_text("", "", ocr_version=self.version, reasons=self.reasons)
        deadline = time.monotonic() + self.timeout_seconds
        readings = []
        output_budget = self.max_output_bytes
        reasons = list(self.reasons)
        with tempfile.TemporaryDirectory(prefix="muth-ocr-") as directory:
            for side, source in (("front", front), ("back", back)):
                reading = None
                try:
                    if len(source.content) > _MAX_INPUT_BYTES:
                        raise _OCRFailure("ocr_image_limits_exceeded")
                    path = Path(directory) / f"{side}.png"
                    with Image.open(BytesIO(source.content)) as original:
                        if original.width * original.height > _MAX_INPUT_PIXELS:
                            raise _OCRFailure("ocr_image_limits_exceeded")
                        image = ImageOps.exif_transpose(original).convert("L")
                        image.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
                        image = ImageOps.autocontrast(image, cutoff=1)
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, "wb") as stream:
                            image.save(stream, format="PNG")
                    for mode in ("6", "11"):
                        if reading is not None and not _needs_sparse_layout(reading, side):
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise _OCRFailure("ocr_timeout")
                        if output_budget <= 0:
                            raise _OCRFailure("ocr_output_limit_exceeded")
                        content = _run_bounded(
                            [
                                self.executable,
                                str(path),
                                "stdout",
                                "-l",
                                self.languages,
                                "--psm",
                                mode,
                                "--dpi",
                                "300",
                                "tsv",
                            ],
                            remaining,
                            output_budget,
                            cwd=directory,
                        )
                        output_budget -= len(content)
                        attempt = _parse_side(_tsv_lines(content.decode("utf-8", "replace")), side)
                        reading = (
                            attempt if reading is None else _merge_layout_readings(reading, attempt)
                        )
                    readings.append(reading or _SideReading())
                except (
                    OSError,
                    ValueError,
                    csv.Error,
                    Image.DecompressionBombError,
                    _OCRFailure,
                ) as exc:
                    code = (
                        str(exc) if isinstance(exc, _OCRFailure) else "ocr_image_or_process_failed"
                    )
                    reasons.append(code)
                    readings.append(reading or _SideReading())
        return _combine_readings(readings, ocr_version=self.version, reasons=reasons)


def create_document_engine(**kwargs) -> TesseractDocumentEngine:
    return TesseractDocumentEngine(**kwargs)
