/**
 * OpenClaw 2.0 ACI Framework - T1 DOM Supplement.
 * Viewport-only extraction for elements the a11y tree misses.
 */
(function () {
  "use strict";
  const MAX_SUPPLEMENT = 200;
  const VIEWPORT_MARGIN = 100;
  const IOU_THRESHOLD = 0.7;

  function iou(a, b) {
    const x1 = Math.max(a[0], b[0]);
    const y1 = Math.max(a[1], b[1]);
    const x2 = Math.min(a[0] + a[2], b[0] + b[2]);
    const y2 = Math.min(a[1] + a[3], b[1] + b[3]);
    if (x2 <= x1 || y2 <= y1) return 0;
    const inter = (x2 - x1) * (y2 - y1);
    const areaA = a[2] * a[3];
    const areaB = b[2] * b[3];
    return inter / (areaA + areaB - inter);
  }

  function inViewport(rect, vw, vh) {
    if (rect.right < -VIEWPORT_MARGIN || rect.bottom < -VIEWPORT_MARGIN) return false;
    if (rect.left > vw + VIEWPORT_MARGIN || rect.top > vh + VIEWPORT_MARGIN) return false;
    return true;
  }

  function isSupplementCandidate(el) {
    try {
      if (el.disabled === true || el.getAttribute("aria-hidden") === "true") return false;
      const role = el.getAttribute("role");
      if (role) return false;
      const tag = el.tagName;
      if (["A", "BUTTON", "INPUT", "TEXTAREA", "SELECT"].includes(tag)) return false;
      if (el.hasAttribute("onclick") || typeof el.onclick === "function") return true;
      if (el.getAttribute("contenteditable") === "true") return true;
      if (el.hasAttribute("tabindex") && el.tabIndex >= 0) return true;
      const cs = getComputedStyle(el);
      if (cs.cursor === "pointer" && el.offsetWidth > 0 && el.offsetWidth < 500) return true;
      return false;
    } catch (_) { return false; }
  }

  function getCleanText(el) {
    try {
      const aria = el.getAttribute("aria-label");
      if (aria && aria.trim()) return aria.trim().slice(0, 100);
      const inner = el.innerText;
      if (inner && inner.trim()) return inner.trim().slice(0, 100);
      const ph = el.getAttribute("placeholder");
      if (ph && ph.trim()) return ph.trim().slice(0, 100);
      const title = el.getAttribute("title");
      if (title && title.trim()) return title.trim().slice(0, 100);
      return "";
    } catch (_) { return ""; }
  }

  window.OpenClawSupplement = {
    extractSupplement: function (t0Bboxes) {
      const vw = window.innerWidth || document.documentElement.clientWidth;
      const vh = window.innerHeight || document.documentElement.clientHeight;
      const results = [];
      const allElements = document.body.querySelectorAll("*");
      for (const el of allElements) {
        if (results.length >= MAX_SUPPLEMENT) break;
        if (!isSupplementCandidate(el)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        if (!inViewport(rect, vw, vh)) continue;
        const bbox = [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)];
        let isDup = false;
        for (const t0b of t0Bboxes) {
          if (iou(bbox, t0b) > IOU_THRESHOLD) { isDup = true; break; }
        }
        if (isDup) continue;
        results.push({ tag: el.tagName.toLowerCase(), text: getCleanText(el), bbox: bbox, attrs: {} });
      }
      return results;
    }
  };
})();
