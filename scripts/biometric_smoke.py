"""Public upstream examples are an inference smoke check, not a population benchmark."""

import argparse
import hashlib
import importlib.util
import json
import platform
import urllib.request
from pathlib import Path

from muth.config import Settings
from muth.engines.biometric import BiometricRuntime, minifas_crop
from muth.media import decode_image

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "b6d5f04ad78778917853b25c778acef6d5626d15"
SAMPLES = {
    "T1": "11e7f4c7179b54e7928a2df0a35c0bc5cddd8e31",
    "F1": "6b6e761fac1008cf785203e0f2a74c7595983d58",
    "F2": "bab2eeb26ebcd8488dfdac3663a8da8c37249349",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-public-samples", action="store_true")
    args = parser.parse_args()
    folder = ROOT / "models" / "biometrics"
    samples = folder / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    runtime = BiometricRuntime(folder / "manifest.json")
    report = {
        "purpose": "public_example_smoke_only",
        "local_accuracy_measured": False,
        "platform": platform.platform(),
        "samples": {},
        "parity": {},
    }
    for name, expected in SAMPLES.items():
        path = samples / f"image_{name}.jpg"
        if args.fetch_public_samples:
            url = (
                "https://raw.githubusercontent.com/minivision-ai/"
                f"Silent-Face-Anti-Spoofing/{COMMIT}/images/sample/image_{name}.jpg"
            )
            with urllib.request.urlopen(url, timeout=60) as response:
                path.write_bytes(response.read())
        content = path.read_bytes()
        actual = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if actual != expected:
            raise ValueError("Sample integrity mismatch")
        image = decode_image(content, Settings(_env_file=None))
        report["samples"][name] = {
            "self_comparison": runtime.face.compare(image, image).model_dump(mode="json"),
            "liveness": runtime.liveness.assess(image).model_dump(mode="json"),
        }
    # Check exported ONNX logits against the original PyTorch weights on real crops.
    import numpy as np
    import torch

    architecture = ROOT / "vendor" / "minifasnet" / "MiniFASNet.py"
    content = architecture.read_bytes()
    if hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() != (
        "548f77346020bcbdb5d2bcff362a10d0bb338104"
    ):
        raise ValueError("Unreviewed architecture source")
    spec = importlib.util.spec_from_file_location("minifasnet", architecture)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    torch.set_num_threads(1)
    image = decode_image((samples / "image_T1.jpg").read_bytes(), Settings(_env_file=None))
    frame, face, _ = runtime.face.core.locate(image)
    for name, factory, scale, session in zip(
        ["2.7_80x80_MiniFASNetV2.pth", "4_0_0_80x80_MiniFASNetV1SE.pth"],
        [module.MiniFASNetV2, module.MiniFASNetV1SE],
        [2.7, 4.0],
        runtime.liveness.core.sessions,
        strict=True,
    ):
        model = factory(conv6_kernel=(5, 5))
        sources = {
            r["name"]: r["sha256"] for r in json.loads((folder / "sources.json").read_text())
        }
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != sources[name]:
            raise ValueError("Unverified PyTorch weights")
        state = torch.load(folder / name, weights_only=True, map_location="cpu")
        model.load_state_dict({k.removeprefix("module."): v for k, v in state.items()}, strict=True)
        model.eval()
        tensor = minifas_crop(frame, face, scale, runtime.face.core.cv)
        with torch.inference_mode():
            expected = model(torch.from_numpy(tensor)).numpy()
        actual = session.run(None, {session.get_inputs()[0].name: tensor})[0]
        np.testing.assert_allclose(actual, expected, atol=0.001, rtol=0.001)
        report["parity"][name] = {
            "max_absolute_logit_error": float(np.max(np.abs(actual - expected))),
            "passed": True,
        }
    output = ROOT / "data" / "evaluation" / "biometric-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report": str(output),
                "parity": report["parity"],
                "scores": {
                    k: {"face": v["self_comparison"]["score"], "liveness": v["liveness"]["score"]}
                    for k, v in report["samples"].items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
