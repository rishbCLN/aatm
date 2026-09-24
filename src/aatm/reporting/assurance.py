"""Assurance Contract v1 proof-pack builder.

Maps AATM's native evidence report (see :mod:`aatm.reporting.evidence_report`)
onto the *frozen* shared ``assurance.json`` shape defined by the
``assurance_contract`` package (contract version ``1.0.0``). This is the single
artifact the control plane (keystone/assura) harvests to aggregate the
transaction-safety pillar, so the shape here MUST match the contract exactly.

Nothing in this module recomputes the verdict: it reads the values already
produced by the scorer/audit chain and re-expresses them. See
``ASSURANCE_CONTRACT.md`` (sections 4-6) and ``PRODUCTION_READINESS.md`` (section
1) for the mapping rationale.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import BUILD_VERSION

# Pinned to the frozen contract. The control plane rejects a mismatched major.
CONTRACT_VERSION = "1.0.0"
ENGINE = "aatm"

# aatm emits the canonical three plus its native superset. The control plane
# collapses FAIL/INCONSISTENT to BLOCKED; we still emit the native status.
_ALLOWED_STATUSES = (
    "PASS",
    "PASS_WITH_CONDITIONS",
    "FAIL",
    "BLOCKED",
    "INCONSISTENT",
)

_VALID_SEVERITIES = ("critical", "high", "medium", "low", "info")

DISCLAIMER = "engineering signal, not a legal certification"


def _content_hash(report: dict[str, Any]) -> str:
    """STABLE sha256 over the seed-deterministic verdict content (Contract v1 §2.2).

    Excludes wall-clock provenance (``meta.generated_at`` and any per-step timing
    under ``result``) so two runs of the same workflow + seed reproduce an
    identical hash for regression + audit comparison. Hashing the whole native
    report previously folded ``generated_at`` (and other timing) into the digest
    and broke cross-run reproducibility.
    """
    score = report.get("score") or {}
    audit_chain = report.get("audit_chain") or {}
    result = report.get("result") or {}
    dead_letters = report.get("dead_letters") or []
    payload = {
        "engine": ENGINE,
        "contract_version": CONTRACT_VERSION,
        "score_total": score.get("total"),
        "score_status": str(score.get("status") or ""),
        "floor_violations": sorted(str(v) for v in (score.get("floor_violations") or [])),
        "audit_chain_valid": bool(audit_chain.get("valid", False)),
        "consistent": result.get("consistent"),
        "dead_letters": len(dead_letters),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


def _created_at(report: dict[str, Any]) -> str:
    meta = report.get("meta") or {}
    generated = meta.get("generated_at")
    if isinstance(generated, str) and generated.strip():
        return generated
    return datetime.now(timezone.utc).isoformat()


def _finding(
    title: str,
    severity: str,
    is_hard_blocker: bool,
    *,
    what_happened: str = "",
    expected: str = "",
    actual: str = "",
    remediation: str = "",
    evidence: Optional[dict[str, Any]] = None,
    source_ref: str = "",
) -> dict[str, Any]:
    """Build one Finding in the shared schema (only the canonical keys)."""
    finding: dict[str, Any] = {
        "title": title,
        "severity": severity if severity in _VALID_SEVERITIES else "info",
        "is_hard_blocker": bool(is_hard_blocker),
    }
    if what_happened:
        finding["what_happened"] = what_happened
    if expected:
        finding["expected"] = expected
    if actual:
        finding["actual"] = actual
    if remediation:
        finding["remediation"] = remediation
    if evidence is not None:
        finding["evidence"] = evidence
    if source_ref:
        finding["source_ref"] = source_ref
    return finding


def _derive_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive Findings from floor violations, audit-chain breaks and dead letters."""
    score = report.get("score") or {}
    audit_chain = report.get("audit_chain") or {}
    dead_letters = report.get("dead_letters") or []

    findings: list[dict[str, Any]] = []

    # 1) Mandatory-floor safety violations -> critical hard blockers. These are
    #    exactly what forced the native status to FAIL.
    floor_violations = score.get("floor_violations") or []
    audit_covered_by_floor = False
    for violation in floor_violations:
        v = str(violation)
        if "audit" in v.lower():
            audit_covered_by_floor = True
        findings.append(
            _finding(
                f"Mandatory safety floor violated: {v}",
                "critical",
                True,
                what_happened=v,
                expected="No mandatory-floor safety violation is detected.",
                actual=v,
                remediation=(
                    "Investigate the violated floor; the run must not be released "
                    "until the underlying transaction-safety defect is fixed."
                ),
                evidence={"violation": v},
                source_ref="aatm.reporting.scoring",
            )
        )

    # 2) Audit-chain invalidity -> critical hard blocker (dedup: the scorer also
    #    records this as a floor violation when the chain is broken).
    audit_valid = bool(audit_chain.get("valid", True))
    if not audit_valid and not audit_covered_by_floor:
        findings.append(
            _finding(
                "Audit hash chain is invalid",
                "critical",
                True,
                what_happened="The tamper-evident audit hash chain failed verification.",
                expected="Audit chain verifies (valid == true).",
                actual=str(audit_chain.get("detail") or "chain invalid"),
                remediation=(
                    "Treat the run as untrusted. Preserve the audit log and "
                    "investigate potential tampering or corruption."
                ),
                evidence=audit_chain if isinstance(audit_chain, dict) else {"audit_chain": audit_chain},
                source_ref="aatm.storage.audit_log",
            )
        )

    # 3) Dead-letter entries -> high-severity hard blockers (unrecoverable
    #    compensations / forward-escalations left for manual intervention).
    for i, entry in enumerate(dead_letters):
        step_id = entry.get("step_id", "?") if isinstance(entry, dict) else "?"
        tool = entry.get("tool") if isinstance(entry, dict) else None
        findings.append(
            _finding(
                f"Unrecoverable action in dead-letter queue (step {step_id})",
                "high",
                True,
                what_happened=(
                    f"Step {step_id}"
                    + (f" (tool {tool})" if tool else "")
                    + " could not be recovered automatically and was dead-lettered "
                    "for manual intervention; the run is business-inconsistent."
                ),
                expected="All compensations/forward-recoveries complete automatically.",
                actual="Entry left open in the durable dead-letter queue.",
                remediation=(
                    "Redrive with 'aatm redrive --run-id <RUN_ID>' or resolve the "
                    "entry manually, then re-verify the run."
                ),
                evidence=entry if isinstance(entry, dict) else {"entry": entry, "index": i},
                source_ref="aatm.storage.dead_letter",
            )
        )

    return findings


def build_assurance_pack(
    report: dict[str, Any],
    *,
    seed: int,
    target_ref: str,
    engine_version: Optional[str] = None,
) -> dict[str, Any]:
    """Build the Contract v1 ``assurance.json`` pack from a native report dict.

    Parameters
    ----------
    report:
        The dict returned by :meth:`EvidenceReport.generate` (has ``result``,
        ``score{total,status,floor_violations}``, ``audit_chain{valid}``,
        ``dead_letters``, ``meta`` and ``_paths``).
    seed:
        The deterministic seed used for the run (recorded in the manifest).
    target_ref:
        The run target - for aatm this is the workflow path.
    engine_version:
        Override for the engine version; defaults to ``aatm.BUILD_VERSION``.
    """
    score = report.get("score") or {}
    audit_chain = report.get("audit_chain") or {}
    result = report.get("result") or {}
    dead_letters = report.get("dead_letters") or []

    # Native status -> release_status. Pass the aatm superset through unchanged;
    # fail closed to BLOCKED for anything unrecognized (contract section 2).
    native_status = str(score.get("status") or "").strip()
    release_status = native_status if native_status in _ALLOWED_STATUSES else "BLOCKED"

    headline: dict[str, Any] = {
        "score_total": score.get("total"),
        "score_status": native_status or None,
        "floor_violations": score.get("floor_violations") or [],
        "audit_chain_valid": bool(audit_chain.get("valid", False)),
        "consistent": result.get("consistent"),
        "dead_letters": len(dead_letters),
    }

    manifest: dict[str, Any] = {
        "content_hash": _content_hash(report),
        "seed": int(seed),
        "created_at": _created_at(report),
        "target_ref": str(target_ref) if str(target_ref).strip() else "unknown",
    }

    pack: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "engine": ENGINE,
        "engine_version": engine_version or BUILD_VERSION,
        "release_status": release_status,
        "headline": headline,
        "findings": _derive_findings(report),
        "manifest": manifest,
        "disclaimer": DISCLAIMER,
    }

    # Optional evidence paths (native JSON + HTML report), if present.
    paths = report.get("_paths") or {}
    evidence_paths: dict[str, str] = {}
    if isinstance(paths, dict):
        if paths.get("json"):
            evidence_paths["native_json"] = str(paths["json"])
        if paths.get("html"):
            evidence_paths["html"] = str(paths["html"])
    if evidence_paths:
        pack["evidence_paths"] = evidence_paths

    return pack


def write_assurance_pack(
    report: dict[str, Any],
    out_dir: str | Path,
    *,
    seed: int,
    target_ref: str,
    engine_version: Optional[str] = None,
) -> Path:
    """Build the pack and write it to ``<out_dir>/assurance.json``; return the path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pack = build_assurance_pack(
        report, seed=seed, target_ref=target_ref, engine_version=engine_version
    )
    pack_path = out / "assurance.json"
    pack_path.write_text(json.dumps(pack, indent=2, default=str), encoding="utf-8")
    return pack_path
