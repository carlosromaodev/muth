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
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from io import BytesIO, StringIO
from pathlib import Path

from PIL import Image, ImageOps

from muth.document_image import prepare_document_image
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
        "BILHETE DE IDENTIDADE N.O",
        "BILHETE DE IDENTIDADE NO",
        "BILHETE DE IDENTIDADE N°",
        "BILHETE DE IDENTIDADE N",
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
    # NASCIMENTO alone is a common surname, including on Angolan identity cards.
    "birth_date": ("DATA DE NASCIMENTO", "DATA NASCIMENTO", "DATE OF BIRTH", "BIRTH DATE"),
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
    r"(?<![A-Z0-9])(" + "|".join(re.escape(alias) for alias, _ in _LABELS) + r")(?=\s|[.:=°]|$)"
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
class OCRWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float | None = None
    words: tuple[OCRWord, ...] = ()
    left: int | None = None
    top: int | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class _SideReading:
    fields: dict[str, DocumentField] = field(default_factory=dict)
    document_type: str = "unknown"
    issuing_country: str | None = None
    reasons: list[str] = field(default_factory=list)
    mrz_checks: dict[str, bool] = field(default_factory=dict)


def _tsv_lines(content: str) -> list[OCRLine]:
    """Keep geometry, then join sparse blocks that share a physical text row.

    Tesseract PSM 11 puts label and value into separate blocks. Treating those
    blocks as independent lines loses the document's columns and reading order.
    """
    grouped: dict[tuple[str, ...], list[OCRWord]] = {}
    reader = csv.DictReader(StringIO(content), delimiter="\t")
    if not {"level", "conf", "text", "page_num", "block_num", "par_num", "line_num"}.issubset(
        reader.fieldnames or ()
    ):
        raise _OCRFailure("ocr_output_format_invalid")
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
        # Very weak glyph guesses (often a portrait, stamp or background) must
        # not distort row geometry or become personal-name characters.
        if not 0.25 <= confidence <= 1:
            continue
        key = tuple(
            row.get(name) or "" for name in ("page_num", "block_num", "par_num", "line_num")
        )
        if key not in grouped and len(grouped) >= _MAX_LINES:
            break
        try:
            box = tuple(int(row.get(name) or "0") for name in ("left", "top", "width", "height"))
        except ValueError:
            continue
        if any(value < 0 or value > 100_000 for value in box):
            continue
        grouped.setdefault(key, []).append(OCRWord(text, confidence, *box))
    rows = list(grouped.values())
    if rows and all(word.height > 0 for words in rows for word in words):
        rows.sort(
            key=lambda words: (min(word.top for word in words), min(word.left for word in words))
        )
        merged: list[list[OCRWord]] = []
        for words in rows:
            top = min(word.top for word in words)
            bottom = max(word.top + word.height for word in words)
            match = None
            for previous in reversed(merged[-3:]):
                previous_top = min(word.top for word in previous)
                previous_bottom = max(word.top + word.height for word in previous)
                overlap = min(bottom, previous_bottom) - max(top, previous_top)
                if overlap >= 0.55 * min(bottom - top, previous_bottom - previous_top):
                    match = previous
                    break
            if match is None:
                merged.append(words)
            else:
                match.extend(words)
        rows = merged
    result = []
    for words in rows:
        if all(word.height > 0 for word in words):
            words.sort(key=lambda word: word.left)
            left, top = min(word.left for word in words), min(word.top for word in words)
            width = max(word.left + word.width for word in words) - left
            height = max(word.top + word.height for word in words) - top
        else:
            left = top = width = height = None
        result.append(
            OCRLine(
                " ".join(word.text for word in words)[:2048],
                sum(len(word.text) * word.confidence for word in words)
                / sum(len(word.text) for word in words),
                tuple(words),
                left,
                top,
                width,
                height,
            )
        )
    return result


def _date_field(value: str, key: str) -> tuple[str, str]:
    # Extract a complete observed date token, never infer a missing component.
    tokens = re.findall(
        r"(?<![0-9])(?:[0-9]{4}[/.-][0-9]{2}[/.-][0-9]{2}|[0-9]{2}[/.-][0-9]{2}[/.-][0-9]{4})(?![0-9])",
        value,
    )
    if len(tokens) != 1:
        return value, "invalid"
    candidate = tokens[0].replace(".", "/").replace("-", "/")
    for pattern in ("%d/%m/%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(candidate, pattern).date()
        except ValueError:
            continue
        valid = 1900 <= parsed.year <= 2100
        if key == "birth_date":
            valid = valid and parsed <= date.today()
        return parsed.isoformat(), "valid" if valid else "invalid"
    return tokens[0], "invalid"


def _normalise_field(key: str, value: str, document_type: str) -> tuple[str, str] | None:
    value = _clean(value.strip(" :;=.°º"))
    if not value or len(value) > _MAX_FIELD_LENGTH or any(char in value for char in "<>\\{}"):
        return None
    if key in {"birth_date", "expiry_date"}:
        if not re.search(
            r"[0-9]{2}[/.-][0-9]{2}[/.-][0-9]{4}|[0-9]{4}[/.-][0-9]{2}[/.-][0-9]{2}", value
        ):
            return None
        return _date_field(value, key)
    if key == "document_number":
        observed = re.search(
            r"(?<![A-Z0-9])(?:[0-9]\s*){9}(?:[A-Z]\s*){2}(?:[0-9]\s*){3}(?![A-Z0-9])", value.upper()
        )
        if document_type == "bi" and observed:
            return re.sub(r"\s+", "", observed.group()), "valid"
        number = re.sub(r"\s+", "", value.upper())
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{3,24}", number):
            return None
        if document_type == "bi":
            return number, "valid" if _BI_NUMBER.fullmatch(number) else "invalid"
        return number, "unvalidated"
    if key == "sex":
        mapping = {"M": "M", "F": "F", "X": "X", "MASCULINO": "M", "FEMININO": "F"}
        token = _fold(value).split()[0].strip(".,;")
        return mapping.get(token, value), "valid" if token in mapping else "invalid"
    if not any(unicodedata.category(character).startswith("L") for character in value):
        return None
    if any(
        not (unicodedata.category(character)[0] in {"L", "M"} or character in " '-.,/")
        for character in value
    ):
        return None
    if key in {"name", "father_name", "mother_name", "parentage"}:
        if sum(character.isalpha() for character in value) < 4:
            return None
        if any(
            word in _fold(value).split()
            for word in (
                "ASSINATURA",
                "TITULAR",
                "DIRECTOR",
                "DIRETOR",
                "IDENTIFICACAO",
                "REPUBLICA",
                "DOCUMENTO",
            )
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


def _label_matches(line: OCRLine):
    """Require a label anchor rather than matching a word inside a person's name."""
    matches = []
    for match in _LABEL_PATTERN.finditer(_fold(line.text)):
        following = line.text[match.end() :].lstrip()
        at_start = not line.text[: match.start()].strip()
        explicit = following.startswith((":", "="))
        standalone = at_start and not following
        short = match.group() in {"NOME", "NAME", "PAI", "MAE", "NUMERO"}
        if explicit or standalone or (at_start and not short):
            matches.append(match)
    return matches


def _span_confidence(line: OCRLine, start: int, end: int) -> float | None:
    selected = []
    cursor = 0
    for word in line.words:
        word_end = cursor + len(word.text)
        if cursor < end and word_end > start:
            selected.append(word)
        cursor = word_end + 1
    if not selected:
        return line.confidence
    return sum(len(word.text) * word.confidence for word in selected) / sum(
        len(word.text) for word in selected
    )


def _name_pieces(
    lines: list[OCRLine], index: int, initial: str, confidence: float | None, key: str
) -> list[tuple[str, float | None, OCRLine]]:
    origin = lines[index]
    pieces = [(initial, confidence, origin)] if initial else []
    previous = origin
    max_lines = 8 if key == "parentage" else 4
    for following in lines[index + 1 : index + 1 + max_lines]:
        if not following.text or _label_matches(following):
            break
        if ":" in following.text or any(character.isdigit() for character in following.text):
            break
        if (
            _fold(following.text).strip() not in {"E", "&"}
            and _normalise_field("name", following.text, "bi") is None
        ):
            break
        if all(
            value is not None
            for value in (
                origin.left,
                following.left,
                previous.top,
                previous.height,
                following.top,
                following.height,
            )
        ):
            height = max(previous.height or 0, following.height or 0, 1)
            if abs((following.left or 0) - (origin.left or 0)) > max(35, 2.5 * height):
                break
            gap = (following.top or 0) - ((previous.top or 0) + (previous.height or 0))
            # A larger gap in the filiacao section separates the two parents.
            if gap > (2.6 if key == "parentage" else 1.3) * height:
                break
        pieces.append((following.text, following.confidence, following))
        previous = following
    return pieces


def _parent_names(
    pieces: list[tuple[str, float | None, OCRLine]],
) -> list[list[tuple[str, float | None, OCRLine]]]:
    groups = [[]]
    previous = None
    for piece in pieces:
        value, _, line = piece
        if _fold(value).strip() in {"E", "&"}:
            if groups[-1]:
                groups.append([])
            previous = None
            continue
        if previous is not None and all(
            v is not None for v in (previous.top, previous.height, line.top, line.height)
        ):
            height = max(previous.height or 0, line.height or 0, 1)
            gap = (line.top or 0) - ((previous.top or 0) + (previous.height or 0))
            if gap > 0.9 * height and groups[-1]:
                groups.append([])
        groups[-1].append(piece)
        previous = line
    return [group for group in groups if group]


def _parse_side(
    lines: list[OCRLine],
    side: str,
    *,
    document_type: str = "unknown",
    issuing_country: str | None = None,
) -> _SideReading:
    reading = _SideReading(document_type=document_type, issuing_country=issuing_country)
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
        matches = _label_matches(line)
        for match_index, match in enumerate(matches):
            end = (
                matches[match_index + 1].start()
                if match_index + 1 < len(matches)
                else len(line.text)
            )
            key = _KEY_BY_LABEL[match.group()]
            segment = line.text[match.end() : end]
            value = segment.strip(" :;=.°º")
            value_start = match.end() + len(segment) - len(segment.lstrip(" :;=.°º"))
            confidence = _span_confidence(line, value_start, end)
            if key in {"name", "father_name", "mother_name", "parentage"}:
                pieces = _name_pieces(lines, index, value, confidence, key)
                if key == "parentage":
                    groups = _parent_names(pieces)
                    value = " e ".join(" ".join(piece[0] for piece in group) for group in groups)
                    if (
                        reading.document_type == "bi"
                        and reading.issuing_country == "AO"
                        and len(groups) == 2
                    ):
                        for parent_key, group in zip(
                            ("father_name", "mother_name"), groups, strict=True
                        ):
                            normalised_parent = _normalise_field(
                                parent_key, " ".join(piece[0] for piece in group), "bi"
                            )
                            if normalised_parent:
                                parent_confidences = [
                                    piece[1] for piece in group if piece[1] is not None
                                ]
                                _add_field(
                                    reading,
                                    parent_key,
                                    DocumentField(
                                        value=normalised_parent[0],
                                        confidence=min(parent_confidences)
                                        if parent_confidences
                                        else None,
                                        source_side=side,
                                        validation="unvalidated",
                                    ),
                                )
                        reading.reasons.append("parent_names_read_from_angolan_bi_layout")
                else:
                    value = " ".join(piece[0] for piece in pieces)
                confidences = [piece[1] for piece in pieces if piece[1] is not None]
                confidence = min(confidences) if confidences else None
            elif not value and index + 1 < len(lines):
                following = lines[index + 1]
                if not _label_matches(following):
                    value, confidence = following.text, following.confidence
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


def _orientation_evidence(reading: _SideReading) -> int:
    return (
        sum(2 for value in reading.fields.values() if value.validation != "invalid")
        + (3 if reading.document_type != "unknown" else 0)
        + (3 if reading.issuing_country is not None else 0)
    )


def _readable_image(image: Image.Image) -> Image.Image:
    image = image.convert("L")
    factor = min(1800 / image.width, 2200 / image.height)
    if factor > 1 and image.width >= 320:
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)),
            Image.Resampling.BICUBIC,
        )
    else:
        image.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
    return ImageOps.autocontrast(image, cutoff=1)


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
        if any(
            f"document_{key}_conflict_{suffix}" in merged.reasons
            for suffix in ("same_side", "layout")
        ):
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
        elif (
            key in {"name", "parentage", "father_name", "mother_name"}
            and candidate.source == previous.source == "ocr"
            and _is_name_extension(previous.value, candidate.value)
        ):
            merged.fields[key] = candidate
            merged.reasons.append(f"ocr_multiline_completed_{key}")
        elif (
            key in {"name", "parentage", "father_name", "mother_name"}
            and candidate.source == previous.source == "ocr"
            and _is_name_extension(candidate.value, previous.value)
        ):
            continue
        elif rank[candidate.validation] > rank[previous.validation]:
            merged.fields[key] = candidate
            merged.reasons.append(f"ocr_layout_recovered_{key}")
        elif candidate.validation == previous.validation == "invalid":
            # Competing malformed OCR is not a conflict between plausible
            # credentials; a later complete observed reading may recover it.
            if (candidate.confidence or 0) > (previous.confidence or 0):
                merged.fields[key] = candidate
            merged.reasons.append(f"ocr_layout_invalid_readings_{key}")
        elif rank[candidate.validation] == rank[previous.validation]:
            merged.fields.pop(key)
            merged.reasons.append(f"document_{key}_conflict_layout")
    return merged


def _is_name_extension(short: str, long: str) -> bool:
    first, second = _fold(short).split(), _fold(long).split()
    return (
        len(first) >= 2
        and len(second) > len(first)
        and any(
            second[index : index + len(first)] == first
            for index in range(len(second) - len(first) + 1)
        )
    )


def _targeted_regions(
    lines: list[OCRLine], reading: _SideReading, size: tuple[int, int]
) -> list[tuple[str, tuple[int, int, int, int], frozenset[str]]]:
    """Retry two difficult fields in label-anchored regions, avoiding the portrait.

    These are OCR windows, not document or identity detection. Only a label
    already observed on the isolated Angolan BI can create a window.
    """
    if reading.document_type != "bi" or reading.issuing_country != "AO":
        return []
    width, height = size
    anchors = {}
    for line in lines:
        if line.left is None or line.top is None or line.height is None:
            continue
        for match in _label_matches(line):
            key = _KEY_BY_LABEL[match.group()]
            if key not in {"parentage", "document_number"}:
                continue
            if key == "document_number" and not any(
                token in match.group() for token in ("IDENTIDADE", "BI", "DOCUMENTO")
            ):
                # A tiny N/O-like mark in a photograph is not a number anchor.
                continue
            selected = []
            cursor = 0
            for word in line.words:
                end = cursor + len(word.text)
                if cursor < match.end() and end > match.start():
                    selected.append(word)
                cursor = end + 1
            if selected:
                left, top = min(word.left for word in selected), min(word.top for word in selected)
                right = max(word.left + word.width for word in selected)
                bottom = max(word.top + word.height for word in selected)
                previous = anchors.get(key)
                if previous is None or right - left > previous[2] - previous[0]:
                    anchors[key] = (left, top, right, bottom)
    regions = []
    parentage = anchors.get("parentage")
    number = anchors.get("document_number")
    if parentage:
        left, top, right, bottom = parentage
        # Include the next complete label: its top box can overlap the last
        # parent-name line, while the parser reliably stops at the label itself.
        limit = number[3] if number and number[1] > bottom else min(height, top + height * 0.42)
        rectangle = (
            max(0, left - max(25, bottom - top)),
            max(0, top - 10),
            min(width, max(right + 30, left + int(width * 0.58))),
            min(height, int(limit)),
        )
        regions.append(
            ("parentage", rectangle, frozenset({"parentage", "father_name", "mother_name"}))
        )
    if number and (
        "document_number" not in reading.fields
        or reading.fields["document_number"].validation == "invalid"
    ):
        left, top, right, bottom = number
        rectangle = (
            max(0, left - max(25, bottom - top)),
            max(0, top - 10),
            min(width, max(right + 40, left + int(width * 0.58))),
            min(height, bottom + max(100, (bottom - top) * 3)),
        )
        regions.append(("document_number", rectangle, frozenset({"document_number"})))
    return regions


def _translate_lines(lines: list[OCRLine], left: int, top: int) -> list[OCRLine]:
    return [
        replace(
            line,
            left=line.left + left if line.left is not None else None,
            top=line.top + top if line.top is not None else None,
            words=tuple(
                replace(word, left=word.left + left, top=word.top + top) for word in line.words
            ),
        )
        for line in lines
    ]


def _fuse_observed_lines(lines: list[OCRLine]) -> list[OCRLine]:
    """Select an observed row when complementary OCR passes missed different rows."""
    candidates = [line for line in lines if line.top is not None and line.height]
    candidates.sort(key=lambda line: (line.top or 0, line.left or 0))
    selected: list[OCRLine] = []
    for candidate in candidates:
        match = None
        for index in range(max(0, len(selected) - 3), len(selected)):
            previous = selected[index]
            overlap = min(
                (candidate.top or 0) + (candidate.height or 0),
                (previous.top or 0) + (previous.height or 0),
            ) - max(candidate.top or 0, previous.top or 0)
            if overlap >= 0.55 * min(candidate.height or 1, previous.height or 1):
                match = index
                break
        if match is None:
            selected.append(candidate)
            continue
        previous = selected[match]

        def rank(line: OCRLine):
            labels = _label_matches(line)
            plausible_name = _normalise_field("name", line.text, "bi") is not None
            return (
                max((len(label.group()) for label in labels), default=0),
                plausible_name,
                sum(len(word.text) * word.confidence for word in line.words)
                if line.words
                else len(line.text) * (line.confidence or 0),
            )

        if rank(candidate) > rank(previous):
            selected[match] = candidate
    return sorted(selected, key=lambda line: (line.top or 0, line.left or 0))


def _number_strip(lines: list[OCRLine], size: tuple[int, int]):
    for line in lines:
        for match in _label_matches(line):
            if _KEY_BY_LABEL[match.group()] != "document_number":
                continue
            if line.top is None or line.height is None:
                continue
            bottom = line.top + line.height
            top = max(0, bottom - max(8, round(line.height * 0.18)))
            end = min(size[1], top + max(100, round(line.height * 1.6)))
            if end - top >= 25:
                return (0, top, size[0], end)
    return None


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

            def read_image(path: Path, mode: str, *, numeric: bool = False) -> list[OCRLine]:
                nonlocal output_budget
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _OCRFailure("ocr_timeout")
                if output_budget <= 0:
                    raise _OCRFailure("ocr_output_limit_exceeded")
                arguments = [
                    self.executable,
                    str(path),
                    "stdout",
                    "-l",
                    self.languages,
                    "--psm",
                    mode,
                    "--dpi",
                    "300",
                    "-c",
                    "tessedit_create_tsv=1",
                ]
                if numeric:
                    arguments.extend(
                        ["-c", "tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
                    )
                content = _run_bounded(
                    arguments,
                    remaining,
                    output_budget,
                    cwd=directory,
                )
                output_budget -= len(content)
                return _tsv_lines(content.decode("utf-8", "replace"))

            for side, source in (("front", front), ("back", back)):
                reading = None
                observed_lines = []
                try:
                    if len(source.content) > _MAX_INPUT_BYTES:
                        raise _OCRFailure("ocr_image_limits_exceeded")
                    path = Path(directory) / f"{side}.png"
                    with Image.open(BytesIO(source.content)) as original:
                        if original.width * original.height > _MAX_INPUT_PIXELS:
                            raise _OCRFailure("ocr_image_limits_exceeded")
                        oriented_source = ImageOps.exif_transpose(original).copy()
                        prepared = prepare_document_image(oriented_source)
                        reasons.extend(prepared.reasons)
                        if "document_transparency_unsupported" in prepared.reasons:
                            raise _OCRFailure("document_transparency_unsupported")
                        # Upscaling assists OCR, while capture quality still
                        # measures the native prepared document resolution.
                        image = _readable_image(prepared.image)
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, "wb") as stream:
                            image.save(stream, format="PNG")
                    lines = read_image(path, "6")
                    reading = _parse_side(lines, side)
                    observed_lines.extend(lines)
                    if prepared.source_isolated and _orientation_evidence(reading) < 4:
                        alternate_prepared = prepare_document_image(
                            oriented_source.transpose(Image.Transpose.ROTATE_180)
                        )
                        rotated = _readable_image(alternate_prepared.image)
                        rotated_path = Path(directory) / f"{side}-rotated.png"
                        descriptor = os.open(
                            rotated_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        with os.fdopen(descriptor, "wb") as stream:
                            rotated.save(stream, format="PNG")
                        rotated_lines = read_image(rotated_path, "6")
                        alternative = _parse_side(rotated_lines, side)
                        if _orientation_evidence(alternative) >= _orientation_evidence(reading) + 4:
                            image, path, reading = rotated, rotated_path, alternative
                            prepared = alternate_prepared
                            observed_lines = rotated_lines
                            reasons.append("document_text_orientation_corrected")
                    for mode in ("11",):
                        if reading is not None and not _needs_sparse_layout(reading, side):
                            break
                        lines = read_image(path, mode)
                        observed_lines.extend(lines)
                        attempt = _parse_side(lines, side)
                        reading = (
                            attempt if reading is None else _merge_layout_readings(reading, attempt)
                        )
                    if prepared.source_isolated and reading is not None:
                        for target in ("parentage", "document_number"):
                            regions = _targeted_regions(observed_lines, reading, image.size)
                            region = next(
                                (region for region in regions if region[0] == target), None
                            )
                            if region is None:
                                continue
                            label, rectangle, keys = region
                            region_path = Path(directory) / f"{side}-{label}.png"
                            descriptor = os.open(
                                region_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                            )
                            with os.fdopen(descriptor, "wb") as stream:
                                image.crop(rectangle).save(stream, format="PNG")
                            region_lines = read_image(region_path, "6")
                            observed_lines.extend(
                                _translate_lines(region_lines, rectangle[0], rectangle[1])
                            )
                            attempt = _parse_side(
                                region_lines,
                                side,
                                document_type=reading.document_type,
                                issuing_country=reading.issuing_country,
                            )
                            attempt.fields = {
                                key: value for key, value in attempt.fields.items() if key in keys
                            }
                            attempt.reasons.append(f"ocr_label_region_used_{label}")
                            reading = _merge_layout_readings(reading, attempt)
                            if label == "parentage":
                                # Sparse reading can recover a short connecting
                                # surname word missed by block segmentation.
                                # Compose observed rows before deriving names;
                                # an isolated suffix is not a competing full name.
                                sparse_lines = read_image(region_path, "11")
                                observed_lines.extend(
                                    _translate_lines(sparse_lines, rectangle[0], rectangle[1])
                                )
                            elif label == "document_number" and (
                                "document_number" not in reading.fields
                                or reading.fields["document_number"].validation == "invalid"
                            ):
                                with Image.open(region_path) as region_image:
                                    strip = _number_strip(region_lines, region_image.size)
                                    if strip is not None:
                                        strip_path = Path(directory) / f"{side}-number-strip.png"
                                        descriptor = os.open(
                                            strip_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                                        )
                                        with os.fdopen(descriptor, "wb") as stream:
                                            region_image.crop(strip).save(stream, format="PNG")
                                        numeric_lines = read_image(strip_path, "7", numeric=True)
                                        candidate = _normalise_field(
                                            "document_number",
                                            " ".join(line.text for line in numeric_lines),
                                            "bi",
                                        )
                                        if candidate and candidate[1] == "valid":
                                            confidences = [
                                                line.confidence
                                                for line in numeric_lines
                                                if line.confidence is not None
                                            ]
                                            precise = _SideReading(
                                                fields={
                                                    "document_number": DocumentField(
                                                        value=candidate[0],
                                                        confidence=min(confidences)
                                                        if confidences
                                                        else None,
                                                        source_side=side,
                                                        validation="valid",
                                                    )
                                                },
                                                document_type=reading.document_type,
                                                issuing_country=reading.issuing_country,
                                                reasons=["ocr_number_line_used"],
                                            )
                                            reading = _merge_layout_readings(reading, precise)
                        if reading.document_type == "bi" and reading.issuing_country == "AO":
                            fused = _parse_side(
                                _fuse_observed_lines(observed_lines),
                                side,
                                document_type=reading.document_type,
                                issuing_country=reading.issuing_country,
                            )
                            # Reassemble multiline names only. Numeric/date
                            # readings remain independent and retain conflicts.
                            fused.fields = {
                                key: value
                                for key, value in fused.fields.items()
                                if key in {"name", "parentage", "father_name", "mother_name"}
                            }
                            fused.reasons.append("ocr_complementary_rows_used")
                            reading = _merge_layout_readings(reading, fused)
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
