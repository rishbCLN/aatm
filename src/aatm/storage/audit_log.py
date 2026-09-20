"""Append-only, tamper-evident audit trail (JSONL + SHA-256 chaining).

Each entry embeds the hash of the previous entry, forming a chain. Any edit to a
historical line breaks every subsequent hash, which :func:`AuditLog.verify` (and
the ``verify-audit`` CLI command) detects.

Entry shape (spec section 19):

    {
      "seq": 42,
      "timestamp": "...",
      "run_id": "...",
      "event": "ACTION_COMPLETE",
      "entity_id": "...",
      "payload": {...},
      "payload_hash": "...",
      "prev_hash": "...",
      "hash": "..."
    }
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..enums import AuditEvent
from ..models import canonical_json, utcnow

GENESIS_HASH = "0" * 64


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hmac_hex(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def compute_entry_hash(
    seq: int,
    timestamp: str,
    run_id: str,
    event: str,
    entity_id: str,
    payload_hash: str,
    prev_hash: str,
) -> str:
    """Deterministic hash over the immutable header fields + prev_hash."""
    material = canonical_json(
        {
            "seq": seq,
            "timestamp": timestamp,
            "run_id": run_id,
            "event": event,
            "entity_id": entity_id,
            "payload_hash": payload_hash,
            "prev_hash": prev_hash,
        }
    )
    return _sha256_hex(material)


class AuditVerificationResult:
    """Outcome of verifying an audit chain."""

    def __init__(
        self,
        valid: bool,
        entry_count: int,
        detail: str = "",
        broken_seq: Optional[int] = None,
    ) -> None:
        self.valid = valid
        self.entry_count = entry_count
        self.detail = detail
        self.broken_seq = broken_seq

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "entry_count": self.entry_count,
            "detail": self.detail,
            "broken_seq": self.broken_seq,
        }

    def __bool__(self) -> bool:
        return self.valid


class AuditLog:
    """Hash-chained append-only log persisted as JSON Lines."""

    def __init__(self, path: Path | str, secret_key: Optional[str] = None,
                 redact: Optional[bool] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Optional HMAC signing key. When set (explicitly or via the
        # AATM_AUDIT_HMAC_KEY env var), every entry is signed so the log becomes
        # tamper-*proof*, not merely tamper-*evident*: an attacker who edits the
        # file cannot forge valid signatures without the secret.
        import os

        key = secret_key if secret_key is not None else os.environ.get(
            "AATM_AUDIT_HMAC_KEY"
        )
        self._key: Optional[bytes] = key.encode("utf-8") if key else None
        # PII/secret redaction is on by default so sensitive values never touch
        # disk. The hash chain is computed over the redacted payload, so
        # verification stays consistent.
        if redact is None:
            redact = os.environ.get("AATM_REDACT_PII", "1") not in ("0", "false",
                                                                     "False", "")
        from ..redaction import Redactor

        self._redactor = Redactor(enabled=redact)
        self._seq, self._last_hash = self._load_tail()

    @property
    def signed(self) -> bool:
        return self._key is not None

    def _load_tail(self) -> tuple[int, str]:
        """Read the last entry to resume the chain (seq + last hash)."""
        if not self.path.exists():
            return 0, GENESIS_HASH
        last_line = ""
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if not last_line:
            return 0, GENESIS_HASH
        entry = json.loads(last_line)
        return int(entry["seq"]), str(entry["hash"])

    # --- append --------------------------------------------------------------

    def append(
        self,
        event: AuditEvent | str,
        *,
        run_id: str,
        entity_id: str = "",
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Append a new hash-chained entry and return it."""
        payload = payload or {}
        # Redact PII/secrets BEFORE hashing so nothing sensitive is persisted and
        # the chain covers exactly what is stored.
        payload = self._redactor.redact(payload)
        event_name = event.value if isinstance(event, AuditEvent) else str(event)
        seq = self._seq + 1
        timestamp = utcnow().isoformat()
        payload_str = canonical_json(payload)
        payload_hash = _sha256_hex(payload_str)
        prev_hash = self._last_hash
        entry_hash = compute_entry_hash(
            seq, timestamp, run_id, event_name, entity_id, payload_hash, prev_hash
        )
        entry = {
            "seq": seq,
            "timestamp": timestamp,
            "run_id": run_id,
            "event": event_name,
            "entity_id": entity_id,
            "payload": payload,
            "payload_hash": payload_hash,
            "prev_hash": prev_hash,
            "hash": entry_hash,
        }
        if self._key is not None:
            entry["sig"] = _hmac_hex(self._key, entry_hash)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
            fh.flush()
            import os

            os.fsync(fh.fileno())
        self._seq = seq
        self._last_hash = entry_hash
        return entry

    # --- read ----------------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    out.append(json.loads(stripped))
        return out

    def count(self) -> int:
        return len(self.entries())

    def events_by_type(self, event: AuditEvent | str) -> list[dict[str, Any]]:
        name = event.value if isinstance(event, AuditEvent) else str(event)
        return [e for e in self.entries() if e.get("event") == name]

    # --- verification --------------------------------------------------------

    def verify(self) -> AuditVerificationResult:
        """Recompute the chain and confirm integrity.

        Detects: tampered payloads (payload_hash mismatch), tampered headers or
        broken links (hash mismatch), out-of-order or missing sequence numbers.
        """
        entries = self.entries()
        if not entries:
            return AuditVerificationResult(True, 0, "empty log")

        prev_hash = GENESIS_HASH
        expected_seq = 1
        for entry in entries:
            seq = entry.get("seq")
            if seq != expected_seq:
                return AuditVerificationResult(
                    False,
                    len(entries),
                    f"sequence gap/reorder: expected {expected_seq}, got {seq}",
                    broken_seq=seq if isinstance(seq, int) else expected_seq,
                )

            # Recompute payload hash.
            payload_hash = _sha256_hex(canonical_json(entry.get("payload", {})))
            if payload_hash != entry.get("payload_hash"):
                return AuditVerificationResult(
                    False,
                    len(entries),
                    f"payload tampering detected at seq {seq}",
                    broken_seq=seq,
                )

            # Recompute link hash.
            recomputed = compute_entry_hash(
                seq,
                entry.get("timestamp", ""),
                entry.get("run_id", ""),
                entry.get("event", ""),
                entry.get("entity_id", ""),
                payload_hash,
                prev_hash,
            )
            if entry.get("prev_hash") != prev_hash:
                return AuditVerificationResult(
                    False,
                    len(entries),
                    f"broken chain link (prev_hash) at seq {seq}",
                    broken_seq=seq,
                )
            if recomputed != entry.get("hash"):
                return AuditVerificationResult(
                    False,
                    len(entries),
                    f"hash mismatch (header tampering) at seq {seq}",
                    broken_seq=seq,
                )

            # HMAC signature check (only when a signing key is available).
            if self._key is not None:
                expected_sig = _hmac_hex(self._key, entry["hash"])
                if entry.get("sig") != expected_sig:
                    return AuditVerificationResult(
                        False,
                        len(entries),
                        f"HMAC signature mismatch at seq {seq}",
                        broken_seq=seq,
                    )

            prev_hash = entry["hash"]
            expected_seq += 1

        detail = "chain intact (HMAC-signed)" if self._key else "chain intact"
        return AuditVerificationResult(True, len(entries), detail)

    # --- anchoring -----------------------------------------------------------

    def head(self) -> dict[str, Any]:
        """Return the current chain head (seq + hash), for external anchoring."""
        return {"run_id": None, "seq": self._seq, "hash": self._last_hash}

    def anchor(self, anchor_path: Path | str) -> dict[str, Any]:
        """Append the current head to an external append-only anchor file.

        Anchoring the head hash somewhere outside the log lets you later prove the
        log has not been truncated or rewritten wholesale (which an in-file chain
        alone cannot detect).
        """
        head = {"seq": self._seq, "hash": self._last_hash,
                "timestamp": utcnow().isoformat()}
        if self._key is not None:
            head["sig"] = _hmac_hex(self._key, self._last_hash)
        p = Path(anchor_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(head, default=str) + "\n")
        return head


def verify_audit_chain(path: Path | str,
                       secret_key: Optional[str] = None) -> AuditVerificationResult:
    """Convenience wrapper used by the CLI and tests.

    Picks up the HMAC key from ``AATM_AUDIT_HMAC_KEY`` automatically when set.
    """
    return AuditLog(path, secret_key=secret_key).verify()
