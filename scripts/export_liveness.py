"""Convert verified MiniFASNet state dictionaries to CPU ONNX; no unsafe pickle loading."""

import hashlib
import importlib.util
import json
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / "models" / "biometrics"
    records = json.loads((folder / "sources.json").read_text())
    records = [r for r in records if not r["name"].startswith("minifas-")]
    expected = {item["name"]: item["sha256"] for item in records}
    architecture = ROOT / "vendor" / "minifasnet" / "MiniFASNet.py"
    content = architecture.read_bytes()
    git_sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    if git_sha != "548f77346020bcbdb5d2bcff362a10d0bb338104":
        raise ValueError("Unreviewed architecture source.")
    spec = importlib.util.spec_from_file_location("minifasnet", architecture)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    torch.set_num_threads(1)
    for source, factory, output in [
        ("2.7_80x80_MiniFASNetV2.pth", module.MiniFASNetV2, "minifas-v2.onnx"),
        ("4_0_0_80x80_MiniFASNetV1SE.pth", module.MiniFASNetV1SE, "minifas-v1se.onnx"),
    ]:
        path = folder / source
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected[source]:
            raise ValueError("Unverified state dictionary.")
        state = torch.load(path, map_location="cpu", weights_only=True)
        state = {name.removeprefix("module."): value for name, value in state.items()}
        model = factory(conv6_kernel=(5, 5))
        model.load_state_dict(state, strict=True)
        model.eval()
        with torch.inference_mode():
            torch.onnx.export(
                model,
                torch.zeros(1, 3, 80, 80),
                folder / output,
                input_names=["input"],
                output_names=["logits"],
                opset_version=17,
                dynamo=False,
            )
        records.append(
            {
                "name": output,
                "sha256": hashlib.sha256((folder / output).read_bytes()).hexdigest(),
                "derived_from": source,
            }
        )
    (folder / "sources.json").write_text(json.dumps(records, indent=2) + "\n")
    evidence = (
        "Local research evaluation only. OpenCV Zoo YuNet directory declares MIT; "
        "SFace directory declares Apache-2.0. Silent-Face repository declares Apache-2.0. "
        "See pinned sources.json and vendor licences. No commercial legal approval, "
        "population accuracy or presentation attack certification is implied.\n"
    )
    (folder / "license-review.txt").write_text(evidence)
    files = {
        "yunet": "yunet.onnx",
        "sface": "sface.onnx",
        "minifas_v2": "minifas-v2.onnx",
        "minifas_v1se": "minifas-v1se.onnx",
    }
    manifest = {
        "schema_version": "biometrics-v1",
        "permitted_use": "research",
        "license_evidence_file": "license-review.txt",
        **{
            name: {"path": file, "sha256": hashlib.sha256((folder / file).read_bytes()).hexdigest()}
            for name, file in files.items()
        },
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("MiniFASNet CPU ONNX exports created.")


if __name__ == "__main__":
    main()
