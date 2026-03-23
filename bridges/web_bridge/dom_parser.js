/**
 * OpenClaw 2.0 ACI Framework - DOM Parser (Ultra-Optimized)
 *
 * Provides short-refs (@e1, @e2) for token saving.
 * Filters for visibility AND interactability.
 * Limits to 800 elements.
 */
(function () {
  "use strict";

  const MAX_ELEMENTS = 800;
  const VIEWPORT_MARGIN = 50;
  let _uidCounter = 0;

  const INTERACTABLE_TAGS = new Set(["A", "BUTTON", "INPUT", "TEXTAREA", "SELECT", "SUMMARY", "DETAILS"]);
  const INTERACTABLE_ROLES = new Set(["button", "link", "menuitem", "tab", "checkbox", "radio", "switch", "option", "slider", "spinbutton", "combobox", "searchbox", "textbox", "gridcell", "treeitem", "listbox"]);
  const RELEVANT_ATTRS = ["href", "type", "name", "placeholder", "aria-label", "aria-expanded", "aria-selected", "aria-checked"];

  function nextUID() {
    return "oc_" + _uidCounter++;
  }

  function isVisible(el) {
    try {
      if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
      const rect = el.getBoundingClientRect();
      const vw = window.innerWidth || document.documentElement.clientWidth;
      const vh = window.innerHeight || document.documentElement.clientHeight;
      if (rect.right < -VIEWPORT_MARGIN || rect.bottom < -VIEWPORT_MARGIN) return false;
      if (rect.left > vw + VIEWPORT_MARGIN || rect.top > vh + VIEWPORT_MARGIN) return false;
      if (typeof el.checkVisibility === 'function') {
        return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
      }
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
    } catch (_) { return false; }
  }

  function isInteractable(el) {
    try {
      if (el.disabled === true || el.getAttribute("aria-hidden") === "true") return false;
      const tag = el.tagName;
      if (INTERACTABLE_TAGS.has(tag)) return true;
      const role = el.getAttribute("role");
      if (role && INTERACTABLE_ROLES.has(role)) return true;
      if (el.hasAttribute("onclick") || typeof el.onclick === "function") return true;
      if (el.getAttribute("contenteditable") === "true") return true;
      if (el.hasAttribute("tabindex") && el.tabIndex >= 0) return true;
      const cs = getComputedStyle(el);
      return cs.cursor === "pointer" && el.offsetWidth > 0 && el.offsetWidth < 500;
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

  function getRelevantAttrs(el) {
    const attrs = {};
    for (const name of RELEVANT_ATTRS) {
      const val = el.getAttribute(name);
      if (val !== null) attrs[name] = val;
    }
    if ("value" in el && typeof el.value === "string") {
      attrs["value"] = el.type === "password" ? "[MASKED]" : el.value.slice(0, 100);
    }
    return attrs;
  }

  function extractFromRoot(root, results) {
    if (!root || results.length >= MAX_ELEMENTS) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null);
    let el = walker.currentNode;
    while (el) {
      if (results.length >= MAX_ELEMENTS) break;
      if (el.nodeType === Node.ELEMENT_NODE) {
        if (el.shadowRoot) extractFromRoot(el.shadowRoot, results);
        if (isInteractable(el) && isVisible(el)) {
          const uid = nextUID();
          el.setAttribute("data-oc-uid", uid);
          const rect = el.getBoundingClientRect();
          results.push({
            ref: "@e" + (results.length + 1),
            uid: uid,
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute("role") || "",
            text: getCleanText(el),
            attrs: getRelevantAttrs(el),
            bbox: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)],
          });
        }
      }
      el = walker.nextNode();
    }
  }

  window.OpenClawExtractor = {
    extractInteractables: function () {
      _uidCounter = 0;
      const results = [];
      extractFromRoot(document.body, results);
      return results;
    },
    findByUID: function (uid) {
      return document.querySelector('[data-oc-uid="' + uid + '"]');
    }
  };
})();
