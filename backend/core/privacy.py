"""Privacy gateway: finds sensitive data in prompts and decides where they run.

Detection is pattern-based, so it can't recognise names or street addresses. To
cover those, admins add their own blocked terms, and cloud routing stays off
unless it is deliberately enabled. Uncertain cases always resolve to local.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Tuple

LOCAL = "local"
CLOUD = "cloud"
BLOCKED = "blocked"

MODEL_AUTO = "auto"
MODEL_CLOUD = "cloud"

ON_SENSITIVE_LOCAL = "local"
ON_SENSITIVE_BLOCK = "block"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    compact = re.sub(r"\s", "", value).upper()
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _verhoeff_ok(digits: str) -> bool:
    check = 0
    for i, ch in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(ch)]]
    return check == 0


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


# (category, pattern, optional validator). Validators cut false positives where
# the format has a checksum; the rest deliberately err towards keeping data local.
_DETECTORS: List[Tuple[str, "re.Pattern[str]", object]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), None),
    ("credit_card", re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])"), lambda v: _luhn_ok(_digits(v))),
    ("iban", re.compile(r"\b[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]){11,30}\b"), _iban_ok),
    ("us_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), None),
    ("emirates_id", re.compile(r"\b784[- ]?\d{4}[- ]?\d{7}[- ]?\d\b"), None),
    ("aadhaar", re.compile(r"(?<![\w-])\d{4}[ -]?\d{4}[ -]?\d{4}(?![\w-])"), lambda v: _verhoeff_ok(_digits(v))),
    ("india_pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), None),
    ("phone", re.compile(
        r"(?<![\w+])(?:\+\d{1,3}[\s.-]?(?:\(?\d{1,4}\)?[\s.-]?){2,4}\d{2,4}"
        r"|\(\d{3}\)[\s.-]?\d{3}[\s.-]?\d{4}"
        r"|\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b"
        r"|\b[6-9]\d{9}\b)"
    ), None),
    ("account_number", re.compile(r"(?<![\w.,-])\d{9,18}(?![\w-])"), None),
    ("credential", re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|mp_[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,})\b"
        r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"|\b(?:password|passwd|api[_ -]?key|secret)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ), None),
]


def scan_text(text: str, blocked_terms: Iterable[str] = ()) -> Dict[str, int]:
    """Returns {category: count}. Only counts are returned, never the matches,
    so results can be stored in the audit trail without copying the data."""
    found: Dict[str, int] = {}
    for category, pattern, validator in _DETECTORS:
        count = sum(1 for m in pattern.finditer(text) if validator is None or validator(m.group(0)))
        if count:
            found[category] = count
    lowered = text.lower()
    term_hits = sum(1 for term in blocked_terms if term and term.lower() in lowered)
    if term_hits:
        found["blocked_term"] = term_hits
    return found


def messages_text(messages: List[dict]) -> Tuple[str, bool]:
    """Returns the text of chat messages and whether all of it could be read.
    Anything but text (an image, say) can't be scanned, so it stays local."""
    parts: List[str] = []
    fully_scannable = True
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                else:
                    fully_scannable = False
        elif content is not None:
            fully_scannable = False
    return "\n".join(parts), fully_scannable


@dataclass
class RouteDecision:
    route: str
    reason: str
    sensitive: Dict[str, int] = field(default_factory=dict)


def decide_route(
    requested_model: str,
    text: str,
    fully_scannable: bool,
    cloud_available: bool,
    cloud_enabled_by_policy: bool,
    on_sensitive: str,
    blocked_terms: Iterable[str],
    auto_cloud_min_chars,
) -> RouteDecision:
    """Pure routing rule. Only an explicit `cloud` request, or `auto` over the
    admin's length threshold, can leave the building, and then only if both
    switches are on and nothing sensitive was found."""
    sensitive = scan_text(text, blocked_terms)

    explicit_cloud = requested_model == MODEL_CLOUD
    auto = requested_model == MODEL_AUTO
    if not explicit_cloud and not auto:
        return RouteDecision(LOCAL, "local model requested", sensitive)
    if auto and (auto_cloud_min_chars is None or len(text) < auto_cloud_min_chars):
        return RouteDecision(LOCAL, "auto: below the cloud threshold", sensitive)

    if not cloud_available or not cloud_enabled_by_policy:
        reason = "cloud routing is disabled" if cloud_available else "cloud routing is switched off on this server"
        return RouteDecision(BLOCKED if explicit_cloud else LOCAL, reason, sensitive)

    if sensitive or not fully_scannable:
        what = ", ".join(sorted(sensitive)) or "content that could not be scanned"
        if on_sensitive == ON_SENSITIVE_BLOCK:
            return RouteDecision(BLOCKED, f"blocked from cloud: {what}", sensitive)
        return RouteDecision(LOCAL, f"kept local: {what}", sensitive)

    return RouteDecision(CLOUD, "no sensitive data found", sensitive)
