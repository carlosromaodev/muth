import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import ValidationError

from muth.benchmark import benchmark_biometrics, load_biometric_dataset
from muth.config import SCOPES, Settings, TenantKey
from muth.evaluation import evaluate_face, validate_manifest
from muth.storage import Database, SessionStore


def bootstrap(path: Path, tenant: str) -> None:
    api_key = secrets.token_urlsafe(32)
    key = TenantKey(
        tenant_id=tenant,
        key_id=f"{tenant}-dev",
        key_sha256=hashlib.sha256(api_key.encode()).hexdigest(),
        scopes=SCOPES,
    )
    tenant_json = json.dumps([key.model_dump(mode="json")], separators=(",", ":"))
    content = (
        "# Configuração local gerada; não publicar nem partilhar este ficheiro.\n"
        "MUTH_ENGINE_MODE=demo\n"
        "MUTH_DATABASE_URL=sqlite:///./data/muth.db\n"
        f"MUTH_DATA_KEY={Fernet.generate_key().decode()}\n"
        f"MUTH_TENANTS='{tenant_json}'\n"
        "# Apenas para usar os exemplos locais; a API valida o hash em MUTH_TENANTS.\n"
        f"MUTH_LOCAL_API_KEY={api_key}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
    print(f"Configuração criada em {path}; chaves guardadas apenas nesse ficheiro.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="muth")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Criar configuração local sem sobrescrever ficheiros")
    init.add_argument("--env-file", type=Path, default=Path(".env"))
    init.add_argument("--tenant", default="local")
    commands.add_parser("migrate", help="Aplicar migrações da DB configurada")
    commands.add_parser("purge", help="Limpar payloads que excederam a retenção")
    manifest = commands.add_parser("validate-model", help="Validar manifesto local e checksums")
    manifest.add_argument("manifest", type=Path)
    manifest.add_argument("--require-commercial", action="store_true")
    benchmark = commands.add_parser("evaluate-face", help="Avaliar pares com limiar fixado")
    benchmark.add_argument("csv", type=Path)
    benchmark.add_argument("--threshold", type=float, required=True)
    benchmark.add_argument("--output", type=Path)
    dataset = commands.add_parser(
        "validate-biometric-dataset",
        help="Validar dataset local, autorização e separação de pessoas",
    )
    dataset.add_argument("dataset", type=Path)
    cpu = commands.add_parser(
        "benchmark-biometrics", help="Avaliar imagens locais em CPU com limiares fixos"
    )
    cpu.add_argument("dataset", type=Path)
    cpu.add_argument("--model-manifest", type=Path, required=True)
    cpu.add_argument("--face-threshold", type=float, default=0.363)
    cpu.add_argument("--liveness-threshold", type=float, default=0.8)
    cpu.add_argument(
        "--output", type=Path, default=Path("data/evaluation/biometric-benchmark.json")
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            bootstrap(args.env_file, args.tenant)
        elif args.command == "validate-model":
            print(
                json.dumps(
                    validate_manifest(args.manifest, require_commercial=args.require_commercial),
                    indent=2,
                )
            )
        elif args.command == "evaluate-face":
            result = json.dumps(evaluate_face(args.csv, args.threshold), indent=2)
            if args.output:
                args.output.write_text(result + "\n")
                print(f"Relatório criado em {args.output}.")
            else:
                print(result)
        elif args.command == "validate-biometric-dataset":
            loaded = load_biometric_dataset(args.dataset)
            print(
                f"Dataset validado: {len(loaded.samples)} ensaios; "
                "autorização declarada localmente."
            )
        elif args.command == "benchmark-biometrics":
            result = benchmark_biometrics(
                args.dataset,
                args.model_manifest,
                face_threshold=args.face_threshold,
                liveness_threshold=args.liveness_threshold,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
            print(f"Relatório agregado criado em {args.output}; nenhuma política foi promovida.")
        else:
            settings = Settings()
            if settings.in_memory:
                raise ValueError("Configure uma base persistente antes desta operação.")
            database = Database(settings)
            try:
                if args.command == "migrate":
                    database.migrate()
                    print("Migrações aplicadas.")
                else:
                    if not database.ready():
                        raise ValueError("Schema indisponível.")
                    count = SessionStore(database, settings).purge()
                    print(f"Payloads eliminados por retenção: {count}.")
            finally:
                database.engine.dispose()
        return 0
    except FileExistsError:
        print("O ficheiro já existe; nenhuma configuração foi substituída.", file=sys.stderr)
    except ValidationError:
        print("Configuração ou manifesto inválido; reveja os campos exigidos.", file=sys.stderr)
    except (ValueError, OSError):
        print(
            "Operação inválida; reveja os ficheiros, configuração e critérios documentados.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
