"use strict";
const assert = require("node:assert/strict");
require("../src/muth/web/camera-guide.js");
const guide = globalThis.MuthCameraGuide;
const sample = (changes = {}) => ({ version: "camera-assessment-v1", target: "document_front", detector_ready: true, detected: true, ready: true, kind: "card", bounds: { x: .2, y: .2, width: .6, height: .4 }, ...changes });
let checked = 0;
function check(name, callback) { callback(); checked += 1; }
check("three current steady observations", () => {
  const tracker = guide.createTracker();
  assert.equal(tracker.assess(sample(), 0, "document_front").stable, false);
  assert.equal(tracker.assess(sample(), 700, "document_front").stable, false);
  assert.equal(tracker.assess(sample(), 1400, "document_front").stable, true);
});
check("three observations need elapsed time", () => {
  const tracker = guide.createTracker();
  for (const now of [0, 100, 200]) assert.equal(tracker.assess(sample(), now, "document_front").stable, false);
});
check("unready frame resets observation", () => {
  const tracker = guide.createTracker(); tracker.assess(sample(), 0, "document_front"); tracker.assess(sample(), 700, "document_front");
  tracker.assess(sample({ ready: false }), 1400, "document_front");
  assert.equal(tracker.assess(sample(), 2100, "document_front").count, 1);
});
check("movement resets observation", () => {
  const tracker = guide.createTracker(); tracker.assess(sample(), 0, "document_front"); tracker.assess(sample(), 700, "document_front");
  assert.equal(tracker.assess(sample({ bounds: { x: .3, y: .2, width: .6, height: .4 } }), 1400, "document_front").count, 1);
});
check("size change resets observation", () => {
  const tracker = guide.createTracker(); tracker.assess(sample(), 0, "document_front");
  assert.equal(tracker.assess(sample({ bounds: { x: .2, y: .2, width: .45, height: .4 } }), 700, "document_front").count, 1);
});
check("long observation pause resets", () => {
  const tracker = guide.createTracker(); tracker.assess(sample(), 0, "document_front"); tracker.assess(sample(), 700, "document_front");
  assert.equal(tracker.assess(sample(), 4000, "document_front").count, 1);
});
check("clock reversal resets", () => {
  const tracker = guide.createTracker(); tracker.assess(sample(), 700, "document_front");
  assert.equal(tracker.assess(sample(), 500, "document_front").count, 1);
});
check("wrong target or detector never captures", () => {
  for (const changes of [{ target: "selfie" }, { kind: "face" }, { detector_ready: false }, { detected: false }, { version: "future" }]) {
    const tracker = guide.createTracker();
    for (const now of [0, 700, 1400]) assert.equal(tracker.assess(sample(changes), now, "document_front").stable, false);
  }
});
check("face guidance accepts its own target", () => {
  const tracker = guide.createTracker(), face = sample({ target: "selfie", kind: "face" });
  tracker.assess(face, 0, "selfie"); tracker.assess(face, 700, "selfie");
  assert.equal(tracker.assess(face, 1400, "selfie").stable, true);
});
check("invalid normalized bounds rejected", () => {
  for (const value of [null, { x: -.1, y: 0, width: .5, height: .5 }, { x: .8, y: .8, width: .5, height: .5 }, { x: 0, y: 0, width: NaN, height: .5 }, { x: 0, y: 0, width: 0, height: .5 }]) assert.equal(guide.bounds(value), null);
});
check("perceptual signature counts all 64 bits", () => {
  assert.equal(guide.signatureDistance("0000000000000000", "0000000000000000"), 0);
  assert.equal(guide.signatureDistance("0000000000000000", "ffffffffffffffff"), 64);
  assert.equal(guide.signatureDistance("0000000000000000", "f000000000000000"), 4);
  assert.equal(guide.signatureDistance(null, "0000000000000000"), null);
  assert.equal(guide.signatureDistance("broken", "0000000000000000"), null);
});
check("explicit reset starts a fresh window", () => {
  const tracker = guide.createTracker(); tracker.assess(sample(), 0, "document_front"); tracker.assess(sample(), 700, "document_front"); tracker.reset();
  assert.equal(tracker.assess(sample(), 1400, "document_front").count, 1);
});
console.log(JSON.stringify({ passed: checked, failed: 0 }));
