"""PII / secret redaction for audit payloads and reports.

Agent tool calls routinely carry personal data (emails, phone numbers, card
numbers) and secrets (tokens, passwords). Those land in two durable places: the
audit log and the evidence report. This module redacts them at the boundary so
sensitive values never touch disk, while preserving enough shape for the log to
stay useful for debugging.

Two complementary strategies:

- **Key-based**: any dict key whose name matches a sensitive term (``password``,
  ``token``, ``ssn``, ``card_number``, ...) has its value masked regardless of
  content.
- **Value-based**: string values are scanned for well-known patterns (emails,
  credit-card-like digit runs, long bearer-token-like strings) and masked even
  when the key looks innocent.

Masking keeps a small, non-reversible hint (e.g. last 4 of a card, the email
domain) to aid debugging without exposing the full value. Redaction is applied to
a deep copy; inputs are never mutated.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

# Substrings that mark a key as sensitive (matched case-insensitively).
_SENSITIVE_KEY_TERMS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "private_key", "access_key",
    "ssn", "social_security", "card_number", "cardnumber", "card",
    "cvv", "cvc", "pin", "account_number", "routing", "iban", "sort_code",
    "dob", "date_of_birth", "passport", "license", "session",
)

# Keys that merely *contain* a term but are safe to keep (avoid over-masking).
_ALLOW_KEYS = {"card_type", "token_type", "auth_method", "account_type"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# 13-19 digit runs, optionally separated by spaces/dashes (card-like).
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# Long opaque tokens (JWT-ish / hex / base64-ish, 24+ chars).
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\.?[A-Za-z0-9_\-.]*\b")


def _mask_email(m: "re.Match[str]") -> str:
    return f"{REDACTED}@{m.group(1)}"


def _mask_card(m: "re.Match[str]") -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) < 13:
        return m.group(0)
    return f"{REDACTED}(last4={digits[-4:]})"


def _redact_string(value: str) -> str:
    original = value
    value = _EMAIL_RE.sub(_mask_email, value)
    value = _CARD_RE.sub(_mask_card, value)
    # Only mask long tokens if the whole string looks like a single opaque token,
    # to avoid clobbering ordinary prose/UUIDs used as identifiers.
    stripped = original.strip()
    if (_TOKEN_RE.fullmatch(stripped)
            and not _looks_like_uuid(stripped)
            and not _looks_like_hash(stripped)):
        return REDACTED
    return value


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


def _looks_like_uuid(value: str) -> bool:
    # UUIDs are legitimate identifiers (run_id, intent_id) - never redact them.
    return bool(_UUID_RE.match(value))


def _looks_like_hash(value: str) -> bool:
    # Pure-hex strings are digests/signatures (audit hashes, HMAC sigs), not
    # secrets to redact - masking them would corrupt the tamper-evident chain.
    return bool(_HEX_RE.match(value))


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if k in _ALLOW_KEYS:
        return False
    return any(term in k for term in _SENSITIVE_KEY_TERMS)


class Redactor:
    """Deep, non-mutating PII/secret redactor."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def redact(self, value: Any) -> Any:
        if not self.enabled:
            return value
        return self._walk(value, parent_sensitive=False)

    def _walk(self, value: Any, *, parent_sensitive: bool) -> Any:
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for k, v in value.items():
                sensitive = isinstance(k, str) and _is_sensitive_key(k)
                if sensitive:
                    out[k] = REDACTED if not isinstance(v, (dict, list)) \
                        else self._mask_container(v)
                else:
                    out[k] = self._walk(v, parent_sensitive=parent_sensitive)
            return out
        if isinstance(value, list):
            return [self._walk(v, parent_sensitive=parent_sensitive) for v in value]
        if isinstance(value, str):
            return REDACTED if parent_sensitive else _redact_string(value)
        return value

    def _mask_container(self, value: Any) -> Any:
        """A sensitive key holding a dict/list => mask every leaf inside it."""
        return self._walk(value, parent_sensitive=True)


# Module-level default redactor (enabled). Callers can pass their own.
default_redactor = Redactor(enabled=True)
