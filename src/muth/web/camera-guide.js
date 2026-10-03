"use strict";

// Geometry here guides capture only. Identity decisions belong to verification.
((root) => {
  const validNumber = (value) => typeof value === "number" && Number.isFinite(value);
  function bounds(value) {
    if (!value || ![value.x, value.y, value.width, value.height].every(validNumber)) return null;
    if (value.x < 0 || value.y < 0 || value.width <= 0 || value.height <= 0 || value.x + value.width > 1.001 || value.y + value.height > 1.001) return null;
    return { x: value.x, y: value.y, width: value.width, height: value.height };
  }
  function signatureDistance(left, right) {
    if (!/^[a-f0-9]{16}$/i.test(left || "") || !/^[a-f0-9]{16}$/i.test(right || "")) return null;
    let bits = 0;
    for (let index = 0; index < 16; index += 1) {
      let value = parseInt(left[index], 16) ^ parseInt(right[index], 16);
      while (value) { bits += value & 1; value >>>= 1; }
    }
    return bits;
  }
  function createTracker() {
    let previous = null, started = null, count = 0;
    const reset = () => { previous = null; started = null; count = 0; };
    function assess(assessment, now, target) {
      const region = bounds(assessment?.bounds);
      const expectedKind = target === "selfie" ? "face" : "card";
      const valid = assessment?.version === "camera-assessment-v1" && assessment.target === target && assessment.detector_ready === true && assessment.detected === true && assessment.ready === true && assessment.kind === expectedKind && region && validNumber(now);
      if (!valid) { reset(); return { stable: false, progress: 0, count: 0 }; }
      if (previous) {
        const centerMotion = Math.hypot(region.x + region.width / 2 - previous.x - previous.width / 2, region.y + region.height / 2 - previous.y - previous.height / 2);
        const sizeMotion = Math.max(Math.abs(region.width - previous.width), Math.abs(region.height - previous.height));
        // A long pause is never counted as steady live observation.
        if (centerMotion > .025 || sizeMotion > .045 || now < previous.observedAt || now - previous.observedAt > 2400) reset();
      }
      if (started === null) started = now;
      previous = { ...region, observedAt: now }; count += 1;
      const elapsed = now - started;
      return { stable: count >= 3 && elapsed >= 1400, progress: Math.min(1, count / 3, elapsed / 1400), count };
    }
    return { reset, assess };
  }
  root.MuthCameraGuide = Object.freeze({ bounds, signatureDistance, createTracker });
})(typeof window === "object" ? window : globalThis);
