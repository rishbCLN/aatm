"""Workflow parser + JSON Schema validation.

Malformed workflow input returns a structured :class:`WorkflowParseError` and the
CLI turns it into a non-zero exit code. It must never surface as a raw Python
traceback by default (spec section 7).

Security (spec section 29): we never ``eval`` YAML content, reject non-string
tool names, and only accept the documented structure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import yaml

try:  # jsonschema is a hard dependency, but degrade gracefully if missing.
    import jsonschema
    from jsonschema import Draft7Validator, RefResolver

    _HAS_JSONSCHEMA = True
except Exception:  # pragma: no cover
    _HAS_JSONSCHEMA = False

from ..config import AATMConfig, default_config


class WorkflowParseError(Exception):
    """Structured, user-readable workflow parse/validation error."""

    def __init__(self, message: str, errors: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "details": self.errors}


class ParsedWorkflow:
    """A parsed + validated workflow document (still raw dicts)."""

    def __init__(self, data: dict[str, Any], source_path: Optional[Path] = None) -> None:
        self.data = data
        self.source_path = source_path

    @property
    def workflow(self) -> dict[str, Any]:
        return self.data.get("workflow", {})

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.data.get("steps", [])

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self.data.get("tools", [])

    @property
    def variables(self) -> list[dict[str, Any]]:
        return self.data.get("variables", [])

    @property
    def failure_injection(self) -> dict[str, Any]:
        return self.data.get("failure_injection", {})


def load_yaml_safe(path: Path | str) -> dict[str, Any]:
    """Load YAML with the safe loader (no arbitrary Python object construction)."""
    p = Path(path)
    if not p.exists():
        raise WorkflowParseError(f"workflow file not found: {p}")
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowParseError("workflow root must be a mapping/object")
    return data


class WorkflowParser:
    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config

    def _schema(self) -> Optional[dict[str, Any]]:
        if self.config.schemas_dir is None:
            return None
        schema_path = Path(self.config.schemas_dir) / "workflow.schema.json"
        if not schema_path.exists():
            return None
        return json.loads(schema_path.read_text(encoding="utf-8"))

    def _local_schema_store(self) -> dict[str, dict[str, Any]]:
        """Preload every local schema so ``$ref`` resolves offline (no network).

        Each schema is registered under both its declared ``$id`` and its bare
        filename, so refs like ``tool.schema.json`` resolve locally.
        """
        store: dict[str, dict[str, Any]] = {}
        schemas_dir = self.config.schemas_dir
        if schemas_dir is None:
            return store
        base_uri = Path(schemas_dir).as_uri() + "/"
        for schema_file in Path(schemas_dir).glob("*.schema.json"):
            try:
                doc = json.loads(schema_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if "$id" in doc:
                store[doc["$id"]] = doc
            # filename-relative and absolute-file URIs
            store[schema_file.name] = doc
            store[base_uri + schema_file.name] = doc
        return store

    def validate(self, data: dict[str, Any]) -> None:
        """Validate against JSON Schema + semantic checks. Raises on failure."""
        errors: list[str] = []

        # JSON Schema validation (structural).
        if _HAS_JSONSCHEMA:
            schema = self._schema()
            if schema is not None:
                base_uri = Path(self.config.schemas_dir).as_uri() + "/"
                # Preload local schemas into the store => fully offline resolution.
                resolver = RefResolver(
                    base_uri=base_uri,
                    referrer=schema,
                    store=self._local_schema_store(),
                )
                validator = Draft7Validator(schema, resolver=resolver)
                for err in sorted(validator.iter_errors(data), key=lambda e: str(e.path)):
                    loc = "/".join(str(p) for p in err.path) or "<root>"
                    errors.append(f"{loc}: {err.message}")

        # Semantic checks that JSON Schema can't easily express.
        errors.extend(self._semantic_checks(data))

        # Format versioning: reject a workflow authored for a newer build.
        raw_version = data.get("schema_version", 1)
        try:
            from ..storage.migrations import SchemaVersionError, check_format_version

            check_format_version("workflow", int(raw_version))
        except SchemaVersionError as exc:
            errors.append(str(exc))
        except (TypeError, ValueError):
            errors.append(f"schema_version: must be an integer, got {raw_version!r}")

        if errors:
            raise WorkflowParseError("workflow validation failed", errors)

    def _semantic_checks(self, data: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        steps = data.get("steps", [])
        if not isinstance(steps, list) or not steps:
            errors.append("steps: must be a non-empty list")
            return errors

        seen_ids: set[str] = set()
        for i, step in enumerate(steps):
            sid = step.get("id")
            if not isinstance(sid, str) or not sid:
                errors.append(f"steps[{i}].id: must be a non-empty string")
                continue
            if sid in seen_ids:
                errors.append(f"steps[{i}].id: duplicate step id '{sid}'")
            seen_ids.add(sid)
            tool = step.get("tool")
            if not isinstance(tool, str) or not tool:
                errors.append(f"steps[{i}].tool: must be a non-empty string")
            # Reject anything that looks like a shell/eval injection attempt.
            if isinstance(tool, str) and any(c in tool for c in ("`", "$", ";", "|", "&&")):
                errors.append(f"steps[{i}].tool: illegal characters in tool name")

        # depends_on references must exist.
        for i, step in enumerate(steps):
            for dep in step.get("depends_on", []) or []:
                if dep not in seen_ids:
                    errors.append(
                        f"steps[{i}].depends_on: unknown step '{dep}'"
                    )
        return errors

    def parse(self, path: Path | str) -> ParsedWorkflow:
        data = load_yaml_safe(path)
        self.validate(data)
        return ParsedWorkflow(data, source_path=Path(path))

    def parse_dict(self, data: dict[str, Any]) -> ParsedWorkflow:
        self.validate(data)
        return ParsedWorkflow(data)
