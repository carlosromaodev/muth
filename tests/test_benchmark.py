import json
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

from muth.benchmark import (
    DatasetSample,
    Observation,
    _contained_file,
    _group_report,
    _verified_image,
    benchmark_biometrics,
    load_biometric_dataset,
    summarize_observations,
)
from muth.config import Settings
from muth.domain import Check, Outcome

ROOT = Path(__file__).parents[1]


def png_bytes(color=(40, 90, 130), size=(100, 100)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def sample(**overrides):
    return {
        "role": "face",
        "split": "test",
        "selfie": "private-selfie.png",
        "reference": "private-reference.png",
        "subject_id": "opaque-person-001",
        "reference_subject_id": "opaque-person-001",
        "label": "genuine",
        "source_reference": "opaque-review-001",
        **overrides,
    }


def live_sample(**overrides):
    return sample(
        **{
            "role": "liveness",
            "reference": None,
            "reference_subject_id": None,
            "label": "live",
            **overrides,
        }
    )


def observation(index, label, score, *, role="face", subject=None, reasons=None, **overrides):
    metadata = (sample if role == "face" else live_sample)(
        subject_id=subject or f"person-{index}",
        label=label,
        **overrides,
    )
    if role == "face":
        metadata["reference_subject_id"] = (
            metadata["subject_id"] if label == "genuine" else f"reference-{index}"
        )
    if role == "liveness" and label == "spoof":
        metadata.setdefault("attack_type", "screen_replay")
    default_reason = (
        "face_threshold_not_locally_calibrated"
        if role == "face"
        else "liveness_threshold_not_locally_calibrated"
    )
    check = Check(
        outcome=Outcome.INCONCLUSIVE,
        score=score,
        score_kind="cosine_similarity" if role == "face" else "liveness_softmax",
        reasons=[default_reason] if reasons is None else reasons,
        provider="test-fixture",
        model_version="test",
    )
    return Observation(DatasetSample.model_validate(metadata), check, float(index + 1))


class DatasetValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "private-selfie.png").write_bytes(png_bytes())
        (self.root / "private-reference.png").write_bytes(png_bytes((100, 90, 140)))
        (self.root / "authorization.txt").write_text(
            "Local test declaration, no biometric identity."
        )
        self.manifest = {
            "schema_version": "biometric-dataset-v1",
            "dataset_id": "opaque-dataset-secret-name",
            "purpose": "consented_evaluation",
            "authorization_file": "authorization.txt",
            "label_source": "human_review",
            "samples": [sample()],
        }

    def write_manifest(self):
        path = self.root / "dataset.json"
        path.write_text(json.dumps(self.manifest))
        return path

    def test_load_validates_images_without_inference_dependencies(self):
        loaded = load_biometric_dataset(self.write_manifest())
        self.assertEqual(len(loaded.samples), 1)
        self.assertEqual(loaded.samples[0].metadata.label, "genuine")
        self.assertEqual(_verified_image(loaded, loaded.samples[0].selfie).width, 100)

    def test_rejects_extra_metadata_and_incoherent_labels(self):
        invalid = [
            {"subject_id": "different-subject"},
            {"label": "impostor"},
            {"label": "live"},
            {"attack_type": "print"},
            {"reference": None},
            {"untrusted_score": 0.99},
        ]
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                DatasetSample.model_validate(sample(**update))
        for update in [{"label": "spoof"}, {"attack_type": "print"}, {"reference": "ref.png"}]:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                DatasetSample.model_validate(live_sample(**update))

    def test_upstream_and_unknown_subject_do_not_claim_consent(self):
        self.manifest["label_source"] = "upstream_example"
        with self.assertRaises(ValidationError):
            load_biometric_dataset(self.write_manifest())
        self.manifest["purpose"] = "research_fixture"
        self.manifest["samples"] = [
            sample(subject_id="subject-unknown", reference_subject_id="subject-unknown")
        ]
        load_biometric_dataset(self.write_manifest())
        self.manifest["purpose"] = "consented_evaluation"
        self.manifest["label_source"] = "human_review"
        with self.assertRaises(ValidationError):
            load_biometric_dataset(self.write_manifest())

    def test_empty_authorization_and_traversal_are_rejected(self):
        (self.root / "authorization.txt").write_text(" \n")
        with self.assertRaisesRegex(ValueError, "autorização"):
            load_biometric_dataset(self.write_manifest())
        (self.root / "authorization.txt").write_text("Research declaration")
        for name in ["../private-selfie.png", str(self.root / "private-selfie.png"), "a\\b.png"]:
            self.manifest["samples"][0]["selfie"] = name
            with self.subTest(path=name), self.assertRaises(ValueError):
                load_biometric_dataset(self.write_manifest())

    def test_symlink_cannot_escape_dataset_root(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.png"
            target.write_bytes(png_bytes())
            (self.root / "escape.png").symlink_to(target)
            self.manifest["samples"][0]["selfie"] = "escape.png"
            with self.assertRaisesRegex(ValueError, "dentro"):
                load_biometric_dataset(self.write_manifest())

    def test_subject_and_reference_identities_cannot_cross_splits(self):
        for field in ["subject_id", "reference_subject_id"]:
            second = sample(
                split="calibration",
                subject_id="another-person",
                reference_subject_id="third-person",
                label="impostor",
            )
            second[field] = "opaque-person-001"
            self.manifest["samples"] = [sample(), second]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Participante"):
                load_biometric_dataset(self.write_manifest())

    def test_identical_image_bytes_cannot_cross_splits_under_other_names(self):
        (self.root / "renamed.png").write_bytes((self.root / "private-selfie.png").read_bytes())
        self.manifest["samples"].append(
            live_sample(split="calibration", selfie="renamed.png", subject_id="different-person")
        )
        with self.assertRaisesRegex(ValueError, "Imagem reutilizada"):
            load_biometric_dataset(self.write_manifest())

    def test_identical_image_cannot_inflate_participant_count_under_other_identities(self):
        self.manifest["samples"] = [
            live_sample(subject_id="person-first"),
            live_sample(subject_id="person-contradictory"),
        ]
        with self.assertRaisesRegex(ValueError, "identidades contraditórias"):
            load_biometric_dataset(self.write_manifest())

    def test_impostor_pair_cannot_assign_same_image_to_distinct_participants(self):
        self.manifest["samples"] = [
            sample(
                reference="private-selfie.png",
                label="impostor",
                reference_subject_id="another-identity",
            )
        ]
        with self.assertRaisesRegex(ValueError, "identidades contraditórias"):
            load_biometric_dataset(self.write_manifest())

    def test_changed_image_bytes_are_rechecked_before_inference(self):
        loaded = load_biometric_dataset(self.write_manifest())
        (self.root / "private-selfie.png").write_bytes(png_bytes((255, 1, 0)))
        with self.assertRaisesRegex(ValueError, "alterada"):
            _verified_image(loaded, loaded.samples[0].selfie)

    def test_path_swap_after_resolution_cannot_follow_final_symlink(self):
        loaded = load_biometric_dataset(self.write_manifest())
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.png"
            outside.write_bytes(png_bytes((255, 0, 0)))

            def swap_after_resolution(root, name):
                path = _contained_file(root, name)
                path.unlink()
                path.symlink_to(outside)
                return path

            with (
                patch("muth.benchmark._contained_file", side_effect=swap_after_resolution),
                self.assertRaisesRegex(ValueError, "durante a leitura"),
            ):
                _verified_image(loaded, loaded.samples[0].selfie)

    def test_directory_swap_after_resolution_cannot_escape_root(self):
        folder = self.root / "images"
        folder.mkdir()
        (folder / "selfie.png").write_bytes(png_bytes())
        self.manifest["samples"][0]["selfie"] = "images/selfie.png"
        loaded = load_biometric_dataset(self.write_manifest())
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (outside / "selfie.png").write_bytes(png_bytes((255, 0, 0)))

            def swap_after_resolution(root, name):
                path = _contained_file(root, name)
                folder.rename(self.root / "previous-images")
                folder.symlink_to(outside, target_is_directory=True)
                return path

            with (
                patch("muth.benchmark._contained_file", side_effect=swap_after_resolution),
                self.assertRaisesRegex(ValueError, "durante a leitura"),
            ):
                _verified_image(loaded, loaded.samples[0].selfie)

    def test_image_byte_pixel_and_corruption_limits(self):
        path = self.write_manifest()
        for config in [
            Settings(_env_file=None, max_upload_bytes=10),
            Settings(_env_file=None, max_image_pixels=100),
        ]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                load_biometric_dataset(path, settings=config)
        (self.root / "private-selfie.png").write_bytes(b"not-a-real-image")
        with self.assertRaisesRegex(ValueError, "Imagem do dataset"):
            load_biometric_dataset(path)

    def test_reports_only_aggregates_and_fixed_thresholds(self):
        class FakeRuntime:
            def __init__(self, path):
                self.face = SimpleNamespace(fingerprint="f" * 64)
                self.liveness = SimpleNamespace(fingerprint="e" * 64)

            def bundle(self):
                face = observation(1, "genuine", 0.9).check
                liveness = observation(2, "live", 0.9, role="liveness").check
                return SimpleNamespace(
                    face=SimpleNamespace(compare=lambda reference, selfie: face),
                    liveness=SimpleNamespace(assess=lambda selfie: liveness),
                )

        (self.root / "private-live.png").write_bytes(png_bytes((70, 11, 99)))
        self.manifest["samples"].append(
            live_sample(selfie="private-live.png", subject_id="opaque-person-002")
        )
        with patch("muth.engines.biometric.BiometricRuntime", FakeRuntime):
            report = benchmark_biometrics(
                self.write_manifest(), self.root / "unused-model.json", face_threshold=0.7
            )
        serialized = json.dumps(report)
        for secret in [
            self.manifest["dataset_id"],
            "private-selfie.png",
            "opaque-person-001",
            "opaque-person-002",
            "opaque-review-001",
            "authorization.txt",
        ]:
            self.assertNotIn(secret, serialized)
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["thresholds_fitted"])
        self.assertEqual(report["thresholds"]["face"], 0.7)
        self.assertEqual(report["splits"]["test"]["face"]["threshold_evaluable"], 1)
        self.assertEqual(report["splits"]["test"]["face"]["acquisition_failures"], 0)
        self.assertEqual(report["splits"]["test"]["face"]["baseline_policy_abstentions"], 1)

    def test_mutation_during_model_initialization_cannot_change_evaluated_images(self):
        class MutatingRuntime:
            def __init__(runtime, path):
                (self.root / "private-selfie.png").write_bytes(png_bytes((1, 2, 3)))

            def bundle(runtime):
                return None

        with (
            patch("muth.engines.biometric.BiometricRuntime", MutatingRuntime),
            self.assertRaisesRegex(ValueError, "alterada"),
        ):
            benchmark_biometrics(self.write_manifest(), self.root / "unused.json")

    def test_invalid_thresholds_fail_before_model_loading(self):
        for face, liveness in [(float("nan"), 0.8), (0.3, float("inf")), (-1.1, 0.8), (0.3, -0.1)]:
            with self.subTest(face=face, liveness=liveness), self.assertRaises(ValueError):
                benchmark_biometrics(
                    Path("missing"),
                    Path("missing"),
                    face_threshold=face,
                    liveness_threshold=liveness,
                )


class AggregateMetricsTests(unittest.TestCase):
    def test_failed_acquisition_and_baseline_abstention_keep_distinct_denominators(self):
        rows = [
            observation(1, "genuine", 0.9),
            observation(2, "genuine", 0.2),
            observation(3, "genuine", None, reasons=["no_face_detected"]),
            observation(4, "impostor", 0.8),
            observation(5, "impostor", 0.1),
        ]
        result = summarize_observations(rows, 0.5)
        self.assertEqual(result["metrics"]["fmr"]["rate"], 0.5)
        self.assertEqual(result["metrics"]["fnmr"]["rate"], 0.5)
        self.assertEqual(result["metrics"]["fnmr"]["total"], 2)
        self.assertAlmostEqual(result["metrics"]["positive_diagnostic_non_accept"]["rate"], 2 / 3)
        self.assertEqual(result["metrics"]["positive_engine_non_accept"]["rate"], 1)
        self.assertEqual(result["class_counts"]["genuine"]["attempted"], 3)
        self.assertEqual(result["threshold_evaluable"], 4)
        self.assertEqual(result["engine_abstentions"], 5)
        self.assertEqual(result["baseline_policy_abstentions"], 4)
        self.assertEqual(result["acquisition_failures"], 1)
        self.assertEqual(result["diagnostic_abstentions"], 1)
        self.assertEqual(result["latency_ms"]["p50"], 4)

    def test_repeated_participants_or_public_fixtures_withhold_binomial_intervals(self):
        rows = [observation(1, "genuine", 0.9), observation(2, "genuine", 0.1)]
        result = summarize_observations(rows, 0.5)
        self.assertIsNotNone(result["metrics"]["fnmr"]["ci95"])
        rows[1] = observation(2, "genuine", 0.1, subject="person-1")
        result = summarize_observations(rows, 0.5)
        self.assertTrue(result["metrics"]["fnmr"]["repeated_participants"])
        self.assertIsNone(result["metrics"]["fnmr"]["ci95"])
        fixture = summarize_observations([rows[0]], 0.5, purpose="research_fixture")
        self.assertIsNone(fixture["metrics"]["fnmr"]["ci95"])

    def test_diagnostic_acceptance_does_not_hide_engine_abstention(self):
        rows = [
            observation(1, "genuine", 0.9),
            observation(2, "genuine", 0.9),
            observation(3, "genuine", 0.1),
        ]
        rows[0].check.outcome = Outcome.PASS
        rows[2].check.outcome = Outcome.FAIL
        result = summarize_observations(rows, 0.5)
        self.assertAlmostEqual(result["metrics"]["positive_diagnostic_non_accept"]["rate"], 1 / 3)
        self.assertAlmostEqual(result["metrics"]["positive_engine_non_accept"]["rate"], 2 / 3)
        self.assertEqual(result["engine_abstentions"], 1)

    def test_transparent_capture_is_a_visible_acquisition_quality_failure(self):
        result = summarize_observations(
            [observation(1, "genuine", None, reasons=["transparent_capture_unsupported"])], 0.5
        )
        self.assertEqual(result["quality_failures"], 1)
        self.assertEqual(result["acquisition_failures"], 1)
        self.assertEqual(result["reason_counts"], {"transparent_capture_unsupported": 1})

    def test_shared_impostor_reference_is_participant_correlation(self):
        first, second = observation(1, "impostor", 0.9), observation(2, "impostor", 0.1)
        second.sample.reference_subject_id = first.sample.reference_subject_id
        result = summarize_observations([first, second], 0.5)
        self.assertIsNone(result["metrics"]["fmr"]["ci95"])

    def test_disagreement_quality_and_unsupported_attacks_are_not_hidden(self):
        rows = [
            observation(1, "live", 0.9, role="liveness"),
            observation(2, "live", 0.4, role="liveness"),
            observation(3, "live", None, role="liveness", reasons=["face_too_blurred"]),
            observation(
                4, "live", 0.95, role="liveness", reasons=["liveness_ensemble_disagreement"]
            ),
            observation(5, "spoof", 0.9, role="liveness", attack_type="print"),
            observation(6, "spoof", 0.1, role="liveness", attack_type="screen_replay"),
            observation(7, "spoof", 0.99, role="liveness", attack_type="injection"),
        ]
        result = _group_report(rows, 0.8, "consented_evaluation")
        self.assertEqual(result["metrics"]["apcer_supported_pai"]["rate"], 0.5)
        self.assertEqual(result["metrics"]["apcer_supported_pai"]["total"], 2)
        self.assertEqual(result["metrics"]["bpcer"]["rate"], 0.5)
        self.assertEqual(result["metrics"]["positive_diagnostic_non_accept"]["rate"], 0.75)
        self.assertEqual(result["metrics"]["positive_engine_non_accept"]["rate"], 1)
        worst = result["metrics"]["worst_supported_pai_apcer"]
        self.assertEqual(worst["rate"], 1)
        self.assertEqual(worst["attack_type"], "print")
        self.assertIsNone(worst["ci95"])
        self.assertEqual(result["diagnostic_abstentions"], 2)
        self.assertEqual(result["quality_failures"], 1)
        self.assertEqual(result["unsupported_attack_trials"], 1)
        injection = result["by_attack_type"]["injection"]
        self.assertEqual(injection["metrics"]["apcer_supported_pai"]["total"], 0)
        self.assertFalse(injection["rgb_pad_supported_attack_category"])
        self.assertIsNone(injection["unsupported_threshold_diagnostic"]["ci95"])
        self.assertFalse(injection["attack_coverage_certified"])

    def test_unexpected_reason_does_not_leak_private_identifiers(self):
        result = summarize_observations(
            [observation(1, "genuine", 0.9, reasons=["private-customer-123"])], 0.5
        )
        self.assertEqual(result["threshold_evaluable"], 0)
        self.assertEqual(result["reason_counts"], {"other_engine_reason": 1})
        self.assertNotIn("private-customer-123", json.dumps(result))

    def test_invalid_mixed_role_and_negative_latency_are_rejected(self):
        with self.assertRaises(ValueError):
            summarize_observations([], 0.5)
        with self.assertRaises(ValueError):
            summarize_observations(
                [observation(1, "genuine", 0.9), observation(2, "live", 0.9, role="liveness")], 0.5
            )
        row = observation(1, "genuine", 0.9)
        with self.assertRaises(ValueError):
            summarize_observations([Observation(row.sample, row.check, -1)], 0.5)


@unittest.skipUnless(os.environ.get("MUTH_TEST_BIOMETRICS") == "1", "Explicit local model tests")
class OfflineCpuBenchmarkTests(unittest.TestCase):
    def test_public_fixtures_report_inference_without_population_accuracy_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("T1", "F1", "F2"):
                shutil.copyfile(
                    ROOT / f"models/biometrics/samples/image_{name}.jpg", root / f"{name}.jpg"
                )
            (root / "authorization.txt").write_text(
                "Public upstream research examples, not individually consented evaluation data."
            )
            manifest = {
                "schema_version": "biometric-dataset-v1",
                "dataset_id": "public-inference-smoke",
                "purpose": "research_fixture",
                "authorization_file": "authorization.txt",
                "label_source": "upstream_example",
                "samples": [
                    sample(
                        selfie="T1.jpg",
                        reference="T1.jpg",
                        subject_id="subject-unknown",
                        reference_subject_id="subject-unknown",
                    ),
                    live_sample(selfie="T1.jpg", subject_id="subject-unknown"),
                    live_sample(
                        selfie="F1.jpg",
                        subject_id="subject-unknown",
                        label="spoof",
                        attack_type="other",
                    ),
                    live_sample(
                        selfie="F2.jpg",
                        subject_id="subject-unknown",
                        label="spoof",
                        attack_type="other",
                    ),
                ],
            }
            path = root / "dataset.json"
            path.write_text(json.dumps(manifest))
            report = benchmark_biometrics(path, ROOT / "models/biometrics/manifest.json")
        face = report["splits"]["test"]["face"]
        liveness = report["splits"]["test"]["liveness"]
        self.assertEqual(face["threshold_evaluable"], 1)
        self.assertEqual(face["baseline_policy_abstentions"], 1)
        self.assertEqual(face["acquisition_failures"], 0)
        self.assertEqual(face["metrics"]["fnmr"]["errors"], 0)
        self.assertIsNone(face["metrics"]["fnmr"]["ci95"])
        self.assertEqual(liveness["attempted"], 3)
        self.assertEqual(liveness["scored"], 3)
        self.assertEqual(liveness["unsupported_attack_trials"], 2)
        self.assertEqual(liveness["metrics"]["apcer_supported_pai"]["total"], 0)
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["authorization_independently_verified"])
