"""Explicit local activation after loading and checking all biometric models."""

import os
import re
import stat
import tempfile
from pathlib import Path

from muth.engines.biometric import BiometricRuntime

_SETTING = re.compile(r"^[ \t]*(?:export[ \t]+)?(MUTH_ENGINE_MODE|MUTH_BIOMETRIC_MANIFEST)[ \t]*=")


class BiometricSetupError(ValueError):
    """A diagnostic safe to print without configuration or native error details."""


def _read_env(path: Path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise BiometricSetupError(
                    "O ficheiro de configuração deve ser um ficheiro regular."
                )
            return source.read(), metadata
    except FileNotFoundError as exc:
        raise BiometricSetupError(
            "Configuração em falta; executa muth init antes de activar os motores."
        ) from exc
    except OSError as exc:
        raise BiometricSetupError(
            "Não foi possível ler a configuração; verifica as permissões e evita links simbólicos."
        ) from exc


def _updated_env(content: str, manifest: Path) -> str:
    value = str(manifest)
    if any(character in value for character in ("\n", "\r", "\x00")) or "${" in value:
        raise BiometricSetupError(
            "O caminho do manifesto contém caracteres incompatíveis com .env."
        )
    quoted = value.replace("\\", "\\\\").replace("'", "\\'")
    assignments = {
        "MUTH_ENGINE_MODE": "MUTH_ENGINE_MODE=biometric",
        "MUTH_BIOMETRIC_MANIFEST": f"MUTH_BIOMETRIC_MANIFEST='{quoted}'",
    }
    newline = "\r\n" if "\r\n" in content else "\n"
    lines, found = [], set()
    for line in content.splitlines(keepends=True):
        match = _SETTING.match(line)
        if match is None:
            lines.append(line)
            continue
        name = match.group(1)
        found.add(name)
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        lines.append(assignments[name] + ending)
    result = "".join(lines)
    for name, assignment in assignments.items():
        if name not in found:
            if result and not result.endswith(("\n", "\r")):
                result += newline
            result += assignment + newline
    return result


def activate_biometrics(env_file: Path, manifest: Path) -> None:
    """Validate native inference, then atomically set only two local configuration keys."""
    original, metadata = _read_env(env_file)
    try:
        content = original.decode("utf-8")
    except UnicodeError as exc:
        raise BiometricSetupError("O ficheiro de configuração deve usar UTF-8.") from exc
    manifest = manifest.resolve()
    replacement = _updated_env(content, manifest).encode("utf-8")
    try:
        # The constructor checks each SHA-256 and loads the exact checked bytes.
        # It also verifies the YuNet/SFace/MiniFASNet native inference contracts.
        BiometricRuntime(manifest)
    except (ImportError, ModuleNotFoundError) as exc:
        raise BiometricSetupError(
            "Dependências biométricas em falta; executa uv sync --locked --extra biometrics."
        ) from exc
    except Exception as exc:
        raise BiometricSetupError(
            "Os motores não puderam ser validados. Verifica o manifesto, os pesos e os SHA-256; "
            "usa scripts/fetch_biometrics.py e scripts/export_liveness.py para preparar os modelos."
        ) from exc

    temporary, published = None, False
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".env.muth-activate-", dir=env_file.parent)
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            # Preserve ownership when possible; failure leaves the original intact.
            if (metadata.st_uid, metadata.st_gid) != (os.getuid(), os.getgid()):
                os.fchown(target.fileno(), metadata.st_uid, metadata.st_gid)
            target.write(replacement)
            target.flush()
            os.fsync(target.fileno())
        current, current_metadata = _read_env(env_file)
        if current != original or (current_metadata.st_dev, current_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise BiometricSetupError(
                "A configuração mudou durante a validação; repete o comando com o servidor parado."
            )
        os.replace(temporary, env_file)
        temporary = None
        published = True
        directory = os.open(env_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        if published:
            raise BiometricSetupError(
                "A configuração foi actualizada, mas a confirmação de persistência falhou; "
                "verifica o ficheiro antes de reiniciar o servidor."
            ) from exc
        raise BiometricSetupError(
            "Não foi possível guardar a configuração; verifica as permissões do ficheiro e pasta."
        ) from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
