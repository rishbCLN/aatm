"""AATM - Agent Action Transaction Manager.

A runtime safety/recovery layer around AI-agent tool calls that:
- tracks intent durably (write-ahead log),
- classifies reversibility and locates the pivot,
- creates and executes compensating actions in reverse completion order,
- survives process crashes and reconciles unknown outcomes,
- enforces idempotency,
- produces a tamper-evident audit chain and an evidence/reliability report.

The runtime transaction engine is deterministic. LLM components are optional and
advisory only; they are never the authority for whether a side effect occurred.
"""

__version__ = "0.1.0"
BUILD_VERSION = "0.1.0"

__all__ = ["__version__", "BUILD_VERSION"]
