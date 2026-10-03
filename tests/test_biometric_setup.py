import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from muth.cli import main
from muth.setup import BiometricSetupError, activate_biometrics


class BiometricSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = self.root / ".env"
        self.manifest = self.root / "models" / "manifest.json"
        self.original = (
            "# Configuração local, manter as chaves\r\n"
            "MUTH_ENGINE_MODE=demo\r\n"
            "MUTH_DATA_KEY=not-a-real-secret\r\n"
            'MUTH_TENANTS=\'[{"tenant_id":"local"}]\'\r\n'
            "MUTH_DOCUMENT_OCR_LANGUAGES=por+eng\r\n"
        ).encode()
        self.env.write_bytes(self.original)

    def test_activation_checks_native_runtime_before_preserving_credentials(self):
        def validate(path):
            self.assertEqual(path, self.manifest.resolve())
            self.assertEqual(self.env.read_bytes(), self.original)

        with patch("muth.setup.BiometricRuntime", side_effect=validate):
            activate_biometrics(self.env, self.manifest)
        updated = self.env.read_bytes()
        self.assertIn(b"MUTH_ENGINE_MODE=biometric\r\n", updated)
        for line in self.original.splitlines(keepends=True):
            if b"MUTH_ENGINE_MODE=" not in line:
                self.assertIn(line, updated)
        settings = dotenv_values(self.env)
        self.assertEqual(settings["MUTH_BIOMETRIC_MANIFEST"], str(self.manifest.resolve()))
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.env.stat().st_uid, os.getuid())
        self.assertEqual(list(self.root.glob(".env.muth-activate-*")), [])

    def test_runtime_failure_does_not_change_file_or_permissions(self):
        self.env.chmod(0o640)
        before = self.env.stat()
        with patch("muth.setup.BiometricRuntime", side_effect=ValueError("invalid SHA-256")):
            with self.assertRaises(BiometricSetupError):
                activate_biometrics(self.env, self.manifest)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertEqual(self.env.stat().st_mode, before.st_mode)
        self.assertEqual(self.env.stat().st_ino, before.st_ino)
        self.assertEqual(list(self.root.glob(".env.muth-activate-*")), [])

    def test_missing_dependencies_are_actionable_and_do_not_leak_error_details(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch(
                "muth.setup.BiometricRuntime",
                side_effect=ModuleNotFoundError("secret-from-native-error"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(
                [
                    "activate-biometrics",
                    "--env-file",
                    str(self.env),
                    "--manifest",
                    str(self.manifest),
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("uv sync --locked --extra biometrics", stderr.getvalue())
        self.assertNotIn("secret-from-native-error", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("not-a-real-secret", stdout.getvalue() + stderr.getvalue())
        self.assertEqual(self.env.read_bytes(), self.original)

    def test_missing_env_requires_explicit_init(self):
        self.env.unlink()
        with patch("muth.setup.BiometricRuntime") as runtime:
            with self.assertRaisesRegex(BiometricSetupError, "muth init"):
                activate_biometrics(self.env, self.manifest)
        runtime.assert_not_called()
        self.assertFalse(self.env.exists())

    def test_symlink_env_is_rejected_without_reading_or_changing_target(self):
        other = self.root / "private.env"
        self.env.rename(other)
        self.env.symlink_to(other)
        with patch("muth.setup.BiometricRuntime") as runtime:
            with self.assertRaisesRegex(BiometricSetupError, "links simbólicos"):
                activate_biometrics(self.env, self.manifest)
        runtime.assert_not_called()
        self.assertTrue(self.env.is_symlink())
        self.assertEqual(other.read_bytes(), self.original)

    def test_atomic_replace_failure_keeps_original_and_removes_temporary(self):
        with (
            patch("muth.setup.BiometricRuntime"),
            patch("muth.setup.os.replace", side_effect=PermissionError("synthetic failure")),
        ):
            with self.assertRaises(BiometricSetupError):
                activate_biometrics(self.env, self.manifest)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob(".env.muth-activate-*")), [])

    def test_file_fsync_failure_does_not_publish_or_leave_credentials_in_temporary(self):
        with (
            patch("muth.setup.BiometricRuntime"),
            patch("muth.setup.os.fsync", side_effect=OSError("synthetic write failure")),
        ):
            with self.assertRaises(BiometricSetupError):
                activate_biometrics(self.env, self.manifest)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob(".env.muth-activate-*")), [])

    def test_directory_fsync_failure_reports_that_atomic_update_already_happened(self):
        with (
            patch("muth.setup.BiometricRuntime"),
            patch("muth.setup.os.fsync", side_effect=[None, OSError("synthetic flush failure")]),
        ):
            with self.assertRaisesRegex(BiometricSetupError, "foi actualizada"):
                activate_biometrics(self.env, self.manifest)
        self.assertEqual(dotenv_values(self.env)["MUTH_ENGINE_MODE"], "biometric")
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".env.muth-activate-*")), [])

    def test_concurrent_update_is_preserved_instead_of_overwritten(self):
        changed = self.original + b"MUTH_SERVER_SETTING=new-value\n"

        def validate(_):
            self.env.write_bytes(changed)

        with patch("muth.setup.BiometricRuntime", side_effect=validate):
            with self.assertRaisesRegex(BiometricSetupError, "mudou durante"):
                activate_biometrics(self.env, self.manifest)
        self.assertEqual(self.env.read_bytes(), changed)
        self.assertEqual(list(self.root.glob(".env.muth-activate-*")), [])

    def test_duplicate_exported_keys_and_path_spaces_are_safe_and_idempotent(self):
        self.env.write_text(
            "export MUTH_ENGINE_MODE = demo\n"
            "MUTH_ENGINE_MODE=demo\n"
            "MUTH_BIOMETRIC_MANIFEST='old/path'\n"
            "# MUTH_ENGINE_MODE=demo\n"
            "MUTH_DOCUMENT_OCR_ENABLED=true"
        )
        path = self.root / "model's folder" / "manifest.json"
        with patch("muth.setup.BiometricRuntime"):
            activate_biometrics(self.env, path)
            first = self.env.read_bytes()
            activate_biometrics(self.env, path)
        self.assertEqual(self.env.read_bytes(), first)
        self.assertEqual(dotenv_values(self.env)["MUTH_BIOMETRIC_MANIFEST"], str(path.resolve()))
        self.assertIn(b"# MUTH_ENGINE_MODE=demo\n", first)
        self.assertIn(b"MUTH_DOCUMENT_OCR_ENABLED=true", first)
        self.assertEqual(first.count(b"MUTH_ENGINE_MODE=biometric"), 2)

    def test_cli_success_prints_restart_and_no_credentials_or_calibration_claim(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("muth.setup.BiometricRuntime"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(
                [
                    "activate-biometrics",
                    "--env-file",
                    str(self.env),
                    "--manifest",
                    str(self.manifest),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("Reinicia o servidor", stdout.getvalue())
        self.assertIn("não valida a autenticidade documental", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("not-a-real-secret", stdout.getvalue())

    def test_invalid_utf8_and_interpolated_paths_are_rejected_before_runtime(self):
        with patch("muth.setup.BiometricRuntime") as runtime:
            self.env.write_bytes(b"\xff")
            with self.assertRaisesRegex(BiometricSetupError, "UTF-8"):
                activate_biometrics(self.env, self.manifest)
            self.env.write_bytes(self.original)
            with self.assertRaisesRegex(BiometricSetupError, "caracteres incompatíveis"):
                activate_biometrics(self.env, self.root / "${SECRET}" / "manifest.json")
        runtime.assert_not_called()
        self.assertEqual(self.env.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
