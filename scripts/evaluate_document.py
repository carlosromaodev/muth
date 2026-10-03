"""Evaluate local document photographs against private annotations.

Neither document values nor image contents are emitted. The output is aggregate
evidence; annotations and photographs remain on the caller's machine.
"""

import argparse
import json
import os
import sys
import time
import unicodedata
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from muth.config import Settings
from muth.media import decode_image
from muth.ocr import TesseractDocumentEngine


def canonical(value):
    value = unicodedata.normalize("NFKD", value).upper()
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


def image_input(path, variant, settings):
    content = path.read_bytes()
    original = decode_image(content, settings)
    if variant == "original":
        return original
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        if variant.startswith("rotate-"):
            image = image.rotate(int(variant.removeprefix("rotate-")), expand=True)
        elif variant == "web-jpeg":
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=94)
    return decode_image(buffer.getvalue(), settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("front", type=Path)
    parser.add_argument("back", type=Path)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=["original", "web-jpeg", "rotate-90", "rotate-180", "rotate-270"],
        default=["original", "web-jpeg"],
    )
    args = parser.parse_args()
    try:
        expected = json.loads(args.expected.read_text())
        assert expected.get("fields") and all(
            isinstance(v, str) for v in expected["fields"].values()
        )
        settings = Settings(_env_file=None, max_upload_bytes=32 * 1024 * 1024)
        engine = TesseractDocumentEngine()
        results = []
        for variant in args.variants:
            front = image_input(args.front, variant, settings)
            back = image_input(args.back, variant, settings)
            started = time.monotonic()
            document = engine.extract(front, back)
            matches = {
                key: key in document.fields
                and canonical(document.fields[key].value) == canonical(value)
                for key, value in expected["fields"].items()
            }
            absent = {key: key not in document.fields for key in expected.get("absent", [])}
            results.append(
                {
                    "variant": variant,
                    "status": document.status,
                    "expected_fields": len(matches),
                    "matched_fields": sum(matches.values()),
                    "field_matches": matches,
                    "unprinted_fields_not_inferred": absent,
                    "extracted_field_count": len(document.fields),
                    "invalid_fields": sorted(
                        key
                        for key, field in document.fields.items()
                        if field.validation == "invalid"
                    ),
                    "conflict_count": len(document.conflicts),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "processing_version": document.processing_version,
                    "passed": all(matches.values()) and all(absent.values()),
                }
            )
        report = {
            "scope": "private_single_document_regression",
            "ocr_languages": engine.languages,
            "ocr_version": engine.version,
            "cases": results,
            "passed": all(case["passed"] for case in results),
            "population_accuracy_measured": False,
            "document_authenticity_confirmed": False,
            "personal_values_included": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "cases": len(results),
                    "matched_fields": [case["matched_fields"] for case in results],
                }
            )
        )
        return 0 if report["passed"] else 1
    except (OSError, ValueError, AssertionError):
        print("Avaliação indisponível; reveja imagens, anotação e instalação OCR.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
