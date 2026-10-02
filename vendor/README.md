# Third-party notices

YuNet model directory is MIT; SFace model directory is Apache-2.0, as declared
by the pinned OpenCV Zoo sources. Copies of their licences are kept in `licenses/`.

`minifasnet/MiniFASNet.py` is unchanged upstream architecture code from
MiniVision Silent-Face-Anti-Spoofing commit
`b6d5f04ad78778917853b25c778acef6d5626d15`, under the adjacent Apache-2.0 licence.
It is used only by the explicit offline exporter/training tools. Runtime inference
uses ONNX and does not import this source or load pickle files.

Weights remain in ignored `models/`, not in source packages. These licence observations
do not replace evaluation of dataset rights or legal approval for commercial deployment.
