"""Explicit, pinned public model download for local evaluation; never runs in the API."""

import hashlib
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZOO = "47534e27c9851bb1128ccc0102f1145e27f23f98"
FAS = "b6d5f04ad78778917853b25c778acef6d5626d15"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def save(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def blob(content: bytes) -> str:
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def main():
    folder = ROOT / "models" / "biometrics"
    records = []
    for name, subdir, filename in [
        ("yunet", "face_detection_yunet", "face_detection_yunet_2023mar.onnx"),
        ("sface", "face_recognition_sface", "face_recognition_sface_2021dec.onnx"),
    ]:
        relative = f"models/{subdir}/{filename}"
        pointer = fetch(f"https://raw.githubusercontent.com/opencv/opencv_zoo/{ZOO}/{relative}")
        match = re.search(rb"oid sha256:([a-f0-9]{64})", pointer)
        if not match:
            raise ValueError("Expected a pinned Git LFS pointer.")
        expected = match[1].decode()
        content = fetch(
            f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{ZOO}/{relative}"
        )
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("Model does not match its Git LFS checksum.")
        save(folder / f"{name}.onnx", content)
        license_bytes = fetch(
            f"https://raw.githubusercontent.com/opencv/opencv_zoo/{ZOO}/models/{subdir}/LICENSE"
        )
        save(ROOT / "vendor" / "licenses" / f"{name}.LICENSE", license_bytes)
        records.append(
            {
                "name": name,
                "sha256": expected,
                "source_commit": ZOO,
                "source": f"https://github.com/opencv/opencv_zoo/blob/{ZOO}/{relative}",
            }
        )
    for filename, expected in [
        ("2.7_80x80_MiniFASNetV2.pth", "47c4af2023fb072c0f5b0e0ade4824053a0558e1"),
        ("4_0_0_80x80_MiniFASNetV1SE.pth", "55a25b316ef33ced3925687007a31a5e306990db"),
    ]:
        content = fetch(
            f"https://raw.githubusercontent.com/minivision-ai/Silent-Face-Anti-Spoofing/{FAS}"
            f"/resources/anti_spoof_models/{filename}"
        )
        if blob(content) != expected:
            raise ValueError("MiniFASNet weights do not match the pinned Git blob.")
        save(folder / filename, content)
        records.append(
            {"name": filename, "sha256": hashlib.sha256(content).hexdigest(), "source_commit": FAS}
        )
    architecture = fetch(
        f"https://raw.githubusercontent.com/minivision-ai/Silent-Face-Anti-Spoofing/{FAS}"
        "/src/model_lib/MiniFASNet.py"
    )
    if blob(architecture) != "548f77346020bcbdb5d2bcff362a10d0bb338104":
        raise ValueError("MiniFASNet architecture does not match the reviewed source.")
    save(ROOT / "vendor" / "minifasnet" / "MiniFASNet.py", architecture)
    save(
        ROOT / "vendor" / "minifasnet" / "LICENSE",
        fetch(
            f"https://raw.githubusercontent.com/minivision-ai/Silent-Face-Anti-Spoofing/{FAS}/LICENSE"
        ),
    )
    save(folder / "sources.json", (json.dumps(records, indent=2) + "\n").encode())
    print("Pinned YuNet, SFace and MiniFASNet assets fetched and verified.")


if __name__ == "__main__":
    main()
