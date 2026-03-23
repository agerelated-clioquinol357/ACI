"""PII detection and masking for OpenClaw screen data.

Scans text extracted from UI elements and screenshots for personally
identifiable information (PII) and replaces matches with ``[MASKED_PII]``
before the data reaches the LLM or is logged.
"""

import re
import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Replacement sentinel
_MASK = "[MASKED_PII]"

# Pre-compiled regex patterns for various PII types.
# Order matters: more specific patterns should come first to avoid partial matches.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "aws_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "chinese_id",
        re.compile(r"\b\d{17}[\dXx]\b"),
    ),
    (
        "ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        "credit_card",
        re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"),
    ),
    (
        "email",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        ),
    ),
    (
        "phone",
        re.compile(
            r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
            r"|\+\d{1,3}[-.\s]?\d{4,14}\b"
        ),
    ),
    (
        "api_key",
        re.compile(
            r"\b(?:sk|pk|api|key|token|secret|password)[-_]?[A-Za-z0-9]{16,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ipv4",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
        ),
    ),
]


class DataMasker:
    """Regex-based PII scrubber for text, dicts, and UI element lists.

    Usage::

        masker = DataMasker()
        clean = masker.mask("Call me at 555-123-4567")
        # => "Call me at [MASKED_PII]"

    The masker is stateless and thread-safe.  You can instantiate one globally
    or create per-request instances -- either approach works.
    """

    def __init__(self, extra_patterns: list[tuple[str, re.Pattern]] | None = None):
        """Initialize the masker.

        Args:
            extra_patterns: Optional list of ``(name, compiled_regex)`` tuples
                to append to the built-in pattern list.
        """
        self._patterns = list(_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def mask(self, text: str) -> str:
        """Apply all PII patterns to a string and replace matches.

        Args:
            text: Input text that may contain PII.

        Returns:
            Sanitized text with PII replaced by ``[MASKED_PII]``.
        """
        if not text:
            return text

        result = text
        for name, pattern in self._patterns:
            count_before = len(pattern.findall(result))
            if count_before > 0:
                result = pattern.sub(_MASK, result)
                logger.debug(f"Masked {count_before} {name} occurrence(s)")

        return result

    def mask_dict(self, data: dict) -> dict:
        """Recursively mask all string values in a dictionary.

        Creates a deep copy -- the original dict is never modified.

        Args:
            data: Input dictionary (may be nested).

        Returns:
            New dictionary with all string values masked.
        """
        return self._recurse(copy.deepcopy(data))

    def _recurse(self, obj: Any) -> Any:
        """Walk a nested structure and mask all strings in-place."""
        if isinstance(obj, str):
            return self.mask(obj)
        elif isinstance(obj, dict):
            return {k: self._recurse(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._recurse(item) for item in obj]
        elif isinstance(obj, tuple):
            return tuple(self._recurse(item) for item in obj)
        return obj

    def mask_elements(self, elements: list[dict]) -> list[dict]:
        """Mask PII in a list of UI element (UIDNode) dictionaries.

        Targets the ``text``, ``value``, ``label``, ``title``, and
        ``placeholder`` fields commonly found in accessibility tree nodes.

        Args:
            elements: List of element dicts (e.g., from ``ContextPerception.elements``).

        Returns:
            New list with sensitive fields masked. Original list is not modified.
        """
        sensitive_keys = {"text", "value", "label", "title", "placeholder", "name"}
        masked = []
        for elem in elements:
            new_elem = dict(elem)
            for key in sensitive_keys:
                if key in new_elem and isinstance(new_elem[key], str):
                    new_elem[key] = self.mask(new_elem[key])
            # Recurse into nested children if present
            if "children" in new_elem and isinstance(new_elem["children"], list):
                new_elem["children"] = self.mask_elements(new_elem["children"])
            masked.append(new_elem)
        return masked

    def has_pii(self, text: str) -> bool:
        """Check whether a string contains any detectable PII.

        Args:
            text: Input text to scan.

        Returns:
            True if any PII pattern matches.
        """
        if not text:
            return False
        for _, pattern in self._patterns:
            if pattern.search(text):
                return True
        return False

    def scan(self, text: str) -> list[dict]:
        """Return details of all PII matches found in the text.

        Args:
            text: Input text to scan.

        Returns:
            List of dicts with keys: ``type``, ``match``, ``start``, ``end``.
        """
        findings: list[dict] = []
        if not text:
            return findings
        for name, pattern in self._patterns:
            for m in pattern.finditer(text):
                findings.append({
                    "type": name,
                    "match": m.group(),
                    "start": m.start(),
                    "end": m.end(),
                })
        return findings

    def __repr__(self) -> str:
        return f"<DataMasker patterns={len(self._patterns)}>"
