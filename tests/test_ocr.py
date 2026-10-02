import os
import random
import shutil
import sys
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError

from muth.document_models import DocumentConflict, DocumentData
from muth.media import ImageInput
from muth.ocr import (
    OCRLine,
    TesseractDocumentEngine,
    _merge_layout_readings,
    _OCRFailure,
    _parse_side,
    _run_bounded,
    _tsv_lines,
    parse_document_text,
)


def synthetic_document(*, back=False):
    image = Image.new("RGB", (1800, 1100), "white")
    draw = ImageDraw.Draw(image)
    fonts = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    )
    font_path = next((path for path in fonts if path.exists()), None)
    font = ImageFont.truetype(str(font_path), 48) if font_path else ImageFont.load_default(size=48)
    lines = (
        [
            "REPUBLICA DE ANGOLA",
            "BILHETE DE IDENTIDADE - SINTETICO",
            "Nome do pai: PAI SINTETICO",
            "Nome da mae: MAE SINTETICA",
            "Nacionalidade: Angolana",
        ]
        if back
        else [
            "REPUBLICA DE ANGOLA",
            "BILHETE DE IDENTIDADE - SINTETICO",
            "Nome: PESSOA SINTETICA",
            "Numero do BI: 123456789LA123",
            "Data de nascimento: 20/01/2000",
            "Validade: 20/01/2030",
            "Sexo: F",
        ]
    )
    for index, line in enumerate(lines):
        draw.text((80, 80 + index * 110), line, font=font, fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return ImageInput(buffer.getvalue(), *image.size, "PNG")


class DocumentParserTests(unittest.TestCase):
    def test_labelled_angolan_document_is_structured_without_authenticity(self):
        document = parse_document_text(
            """REPÚBLICA DE ANGOLA
BILHETE DE IDENTIDADE
Nome: PESSOA SINTÉTICA
Número do BI: 000000000 LA 000
Data de nascimento: 20.01.2000
Validade: 20/01/2030
Sexo: feminino""",
            """Nome do pai: PAI SINTÉTICO
Nome da mãe: MÃE SINTÉTICA
Nacionalidade: Angolana""",
        )
        self.assertEqual(document.status, "extracted")
        self.assertEqual(document.document_type, "bi")
        self.assertEqual(document.issuing_country, "AO")
        self.assertEqual(document.fields["name"].value, "PESSOA SINTÉTICA")
        self.assertEqual(document.fields["document_number"].value, "000000000LA000")
        self.assertEqual(document.fields["birth_date"].value, "2000-01-20")
        self.assertEqual(document.fields["expiry_date"].value, "2030-01-20")
        self.assertEqual(document.fields["sex"].value, "F")
        self.assertEqual(document.fields["mother_name"].source_side, "back")
        self.assertIsNone(document.fields["name"].confidence)
        self.assertFalse(document.authenticity_confirmed)
        self.assertIn("document_authenticity_not_verified", document.reasons)

    def test_separate_label_value_and_two_fields_on_one_line(self):
        document = parse_document_text(
            "Nome\nPESSOA SINTETICA\nSexo: F Nacionalidade: Angolana", ""
        )
        self.assertEqual(document.fields["name"].value, "PESSOA SINTETICA")
        self.assertEqual(document.fields["sex"].value, "F")
        self.assertEqual(document.fields["nationality"].value, "Angolana")

    def test_conflicting_front_back_data_is_not_silently_selected(self):
        document = parse_document_text("Nome: PESSOA SINTETICA", "Nome: OUTRA SINTETICA")
        self.assertNotIn("name", document.fields)
        self.assertEqual(len(document.conflicts), 1)
        self.assertEqual(document.conflicts[0].field, "name")
        self.assertEqual(document.conflicts[0].back_value, "OUTRA SINTETICA")
        self.assertIn("document_fields_conflicting", document.reasons)

    def test_repeated_matching_fields_preserve_both_sides_and_lower_confidence(self):
        document = parse_document_text(
            [OCRLine("Nome: PESSOA SINTETICA", 0.95)],
            [OCRLine("Nome: PESSOA SINTETICA", 0.65)],
        )
        self.assertEqual(document.fields["name"].source_side, "both")
        self.assertEqual(document.fields["name"].confidence, 0.65)

    def test_same_side_conflict_does_not_choose_a_higher_confidence_name(self):
        document = parse_document_text("Nome: PESSOA SINTETICA\nNome: OUTRA SINTETICA", "")
        self.assertNotIn("name", document.fields)
        self.assertIn("document_name_conflict_same_side", document.reasons)

    def test_invalid_calendar_date_and_bi_format_remain_flagged(self):
        document = parse_document_text(
            "BILHETE DE IDENTIDADE\nNome: SINTETICO\n"
            "Numero do BI: ABCD1234\nData de nascimento: 31/02/2000",
            "",
        )
        self.assertEqual(document.status, "partial")
        self.assertEqual(document.fields["birth_date"].validation, "invalid")
        self.assertEqual(document.fields["document_number"].validation, "invalid")
        self.assertIn("document_fields_invalid", document.reasons)

    def test_future_birth_date_is_not_accepted_as_valid(self):
        document = parse_document_text("Data de nascimento: 31/12/2099", "")
        self.assertEqual(document.fields["birth_date"].validation, "invalid")

    def test_angolan_nationality_is_not_proof_of_angolan_issuer(self):
        document = parse_document_text("Passport\nNationality: Angolana", "")
        self.assertIsNone(document.issuing_country)

    def test_expired_date_is_retained_and_expiry_is_reported(self):
        document = parse_document_text("Validade: 20/01/2020", "")
        self.assertEqual(document.fields["expiry_date"].value, "2020-01-20")
        self.assertEqual(document.fields["expiry_date"].validation, "valid")
        self.assertIn("document_expired", document.reasons)

    def test_number_sign_label_can_read_angolan_bi_number(self):
        document = parse_document_text("BILHETE DE IDENTIDADE\nNº: 123456789LA123", "")
        self.assertEqual(document.fields["document_number"].value, "123456789LA123")

    def test_unlabelled_numbers_and_injected_values_do_not_become_fields(self):
        document = parse_document_text(
            "000000000LA000\nNome: <script>alert(1)</script>\n"
            "Nacionalidade: https://example.invalid",
            "",
        )
        self.assertEqual(document.fields, {})
        self.assertEqual(document.status, "unavailable")
        self.assertNotIn("raw_text", document.model_dump())

    def test_unlabelled_next_field_is_not_consumed_as_missing_name(self):
        document = parse_document_text("Nome\nSexo: F", "")
        self.assertNotIn("name", document.fields)
        self.assertEqual(document.fields["sex"].value, "F")

    def test_standard_td3_example_has_checked_fields_without_authenticity(self):
        document = parse_document_text(
            "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\n"
            "L898902C36UTO7408122F1204159ZE184226B<<<<<10",
            "",
        )
        self.assertEqual(document.document_type, "passport")
        self.assertEqual(document.fields["name"].value, "ANNA MARIA ERIKSSON")
        self.assertEqual(document.fields["document_number"].value, "L898902C3")
        self.assertEqual(document.fields["birth_date"].value, "1974-08-12")
        self.assertEqual(document.fields["expiry_date"].value, "2012-04-15")
        self.assertEqual(document.fields["name"].source, "mrz")
        self.assertTrue(all(document.mrz_checks.values()))
        self.assertFalse(document.authenticity_confirmed)

    def test_mrz_bad_checkdigit_is_reported_and_not_validated(self):
        document = parse_document_text(
            "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\n"
            "L898902C35UTO7408122F1204159ZE184226B<<<<<10",
            "",
        )
        self.assertFalse(document.mrz_checks["document_number"])
        self.assertEqual(document.fields["document_number"].validation, "invalid")
        self.assertEqual(document.status, "partial")
        self.assertIn("mrz_checkdigits_failed", document.reasons)

    def test_data_contract_rejects_unknown_fields_and_authenticity_claim(self):
        with self.assertRaises(ValidationError):
            DocumentData(status="unavailable", fields={"raw_text": {}})
        with self.assertRaises(ValidationError):
            DocumentData(status="unavailable", authenticity_confirmed=True)
        with self.assertRaises(ValidationError):
            DocumentConflict(field="unbounded_unknown", front_value="A", back_value="B")

    def test_tsv_confidence_is_normalised_and_invalid_words_ignored(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tconf\ttext\n"
        document = _tsv_lines(
            header + "5\t1\t1\t1\t1\t90\tNome:\n"
            "5\t1\t1\t1\t1\t80\tSINTETICO\n"
            "5\t1\t1\t1\t2\t-1\tFAKE\n"
            "5\t1\t1\t1\t3\tnan\tFAKE\n"
        )
        self.assertEqual(len(document), 1)
        self.assertEqual(document[0].text, "Nome: SINTETICO")
        self.assertGreater(document[0].confidence, 0.8)
        self.assertLess(document[0].confidence, 0.9)

    def test_competing_plausible_layout_readings_remain_ambiguous(self):
        reading = _merge_layout_readings(
            _parse_side([OCRLine("Nome: PESSOA SINTETICA", 0.95)], "front"),
            _parse_side([OCRLine("Nome: OUTRA SINTETICA", 0.99)], "front"),
        )
        self.assertNotIn("name", reading.fields)
        self.assertIn("document_name_conflict_layout", reading.reasons)

    def test_sparse_layout_can_recover_an_observed_valid_number_from_invalid_glyphs(self):
        reading = _merge_layout_readings(
            _parse_side(
                [OCRLine("BILHETE DE IDENTIDADE"), OCRLine("Numero do BI: 123456789LA123 P")],
                "front",
            ),
            _parse_side(
                [OCRLine("BILHETE DE IDENTIDADE"), OCRLine("Numero do BI: 123456789LA123")], "front"
            ),
        )
        self.assertEqual(reading.fields["document_number"].value, "123456789LA123")
        self.assertEqual(reading.fields["document_number"].validation, "valid")
        self.assertIn("ocr_layout_recovered_document_number", reading.reasons)

    def test_valid_visual_name_does_not_override_a_different_mrz_name(self):
        reading = _merge_layout_readings(
            _parse_side([OCRLine("Nome: PESSOA SINTETICA")], "front"),
            _parse_side(
                [
                    OCRLine("P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<"),
                    OCRLine("L898902C36UTO7408122F1204159ZE184226B<<<<<10"),
                ],
                "front",
            ),
        )
        self.assertNotIn("name", reading.fields)
        self.assertIn("document_name_conflict_layout", reading.reasons)


class LocalOCRExecutionTests(unittest.TestCase):
    def test_disabled_and_missing_binary_are_honestly_unavailable(self):
        for arguments, reason in (
            ({"enabled": False}, "ocr_disabled"),
            ({"executable": "/does/not/exist/tesseract"}, "ocr_engine_unavailable"),
        ):
            with self.subTest(arguments=arguments):
                result = TesseractDocumentEngine(**arguments).extract(
                    synthetic_document(), synthetic_document(back=True)
                )
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.fields, {})
                self.assertIn(reason, result.reasons)

    def test_languages_are_never_executable_options(self):
        for language in ("eng;curl", "--help", "eng ../../etc", "eng+../../x"):
            with self.subTest(language=language), self.assertRaises(ValueError):
                TesseractDocumentEngine(enabled=False, languages=language)

    def test_output_limit_kills_child_and_does_not_return_partial_text(self):
        start = time.monotonic()
        with self.assertRaisesRegex(_OCRFailure, "ocr_output_limit_exceeded"):
            _run_bounded([sys.executable, "-c", "import os; os.write(1, b'x'*1000000)"], 3, 4096)
        self.assertLess(time.monotonic() - start, 2)

    def test_timeout_kills_child_and_reaps_it(self):
        children = []
        import subprocess

        original = subprocess.Popen

        def record(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child

        with patch("muth.ocr.subprocess.Popen", side_effect=record):
            with self.assertRaisesRegex(_OCRFailure, "ocr_timeout"):
                _run_bounded([sys.executable, "-c", "import time; time.sleep(10)"], 0.1, 4096)
        self.assertIsNotNone(children[0].returncode)
        self.assertIsNotNone(children[0].poll())

    def test_subprocess_failure_does_not_leak_stderr(self):
        with self.assertRaisesRegex(_OCRFailure, "^ocr_process_failed$"):
            _run_bounded(
                [sys.executable, "-c", "import sys;sys.stderr.write('SENSITIVE');sys.exit(1)"],
                1,
                4096,
            )

    def test_ocr_process_failure_cleans_private_temporary_images(self):
        engine = TesseractDocumentEngine(enabled=False)
        engine.executable = sys.executable
        engine.languages = "eng"
        engine.version = "test"
        engine.reasons = []
        paths = []

        def fail(arguments, timeout, limit, cwd=None):
            path = Path(arguments[1])
            paths.append(path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)
            with Image.open(path) as image:
                self.assertLessEqual(image.width, 2200)
            raise _OCRFailure("ocr_process_failed")

        with patch("muth.ocr._run_bounded", side_effect=fail):
            result = engine.extract(synthetic_document(), synthetic_document(back=True))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("ocr_process_failed", result.reasons)
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in paths))

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract local não instalado")
    def test_real_local_ocr_reads_only_the_generated_synthetic_document(self):
        engine = TesseractDocumentEngine(timeout_seconds=20)
        if not engine.executable:
            self.skipTest("Idiomas do Tesseract local não disponíveis")
        document = engine.extract(synthetic_document(), synthetic_document(back=True))
        self.assertIn(document.status, {"extracted", "partial"})
        self.assertEqual(document.fields["name"].value, "PESSOA SINTETICA")
        self.assertEqual(document.fields["document_number"].value, "123456789LA123")
        self.assertEqual(document.fields["birth_date"].value, "2000-01-20")
        self.assertEqual(document.fields["mother_name"].value, "MAE SINTETICA")
        self.assertGreater(document.fields["name"].confidence, 0)
        self.assertIsNotNone(document.ocr_version)
        self.assertFalse(document.authenticity_confirmed)
        if "por" not in engine.languages.split("+"):
            self.assertIn("ocr_language_fallback_eng", document.reasons)

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract local não instalado")
    def test_real_sparse_ocr_separates_a_textured_region_after_web_jpeg_normalisation(self):
        engine = TesseractDocumentEngine(timeout_seconds=12)
        if not engine.executable:
            self.skipTest("Idiomas do Tesseract local não disponíveis")
        # A generated illustration replaces the portrait, so this fixture stores no face/PII.
        with Image.open(BytesIO(synthetic_document().content)) as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((1280, 320, 1760, 1020), fill=(90, 110, 130))
            randomizer = random.Random(52)
            for _ in range(450):
                x, y = randomizer.randint(1280, 1750), randomizer.randint(320, 1010)
                draw.ellipse(
                    (x, y, x + randomizer.randint(3, 20), y + randomizer.randint(5, 20)),
                    fill=randomizer.choice(("white", "black")),
                )
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            stream = BytesIO()
            image.save(stream, "JPEG", quality=86)
            source = ImageInput(stream.getvalue(), *image.size, "JPEG")
        result = engine.extract(source, synthetic_document(back=True))
        self.assertEqual(result.fields["document_number"].value, "123456789LA123")
        self.assertEqual(result.fields["birth_date"].value, "2000-01-20")
        self.assertEqual(result.fields["expiry_date"].value, "2030-01-20")
        self.assertEqual(result.fields["sex"].value, "F")
        self.assertEqual(result.processing_version, "muth-document-ocr-v2")
        self.assertIn("ocr_sparse_layout_used", result.reasons)
        self.assertFalse(result.authenticity_confirmed)


if __name__ == "__main__":
    unittest.main()
