#!/usr/bin/env python3
"""Validate ModelScope Studio assessment/verification YAML without mandatory pip deps."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    "studio_assessment": ROOT / "references" / "studio-assessment.schema.json",
    "studio_verification": ROOT / "references" / "studio-verification.schema.json",
}


def load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:
        pass

    ruby = shutil.which("ruby")
    if not ruby:
        raise RuntimeError("YAML parser unavailable: install PyYAML or provide Ruby with Psych")
    program = (
        "data = YAML.safe_load(File.read(ARGV[0]), [], [], true); "
        "STDOUT.write(JSON.generate(data))"
    )
    result = subprocess.run(
        [ruby, "-rjson", "-ryaml", "-e", program, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"cannot parse YAML: {result.stderr.strip()}")
    return json.loads(result.stdout)


def resolve_ref(schema_root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported schema reference: {ref}")
    node: Any = schema_root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def type_matches(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    return checks[expected](value)


def validate_schema(
    value: Any,
    schema: dict[str, Any],
    schema_root: dict[str, Any],
    path: str = "$",
) -> list[str]:
    if "$ref" in schema:
        return validate_schema(value, resolve_ref(schema_root, schema["$ref"]), schema_root, path)

    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']!r}")

    expected = schema.get("type")
    if expected:
        expected_types = expected if isinstance(expected, list) else [expected]
        if not any(type_matches(value, item) for item in expected_types):
            errors.append(f"{path}: expected type {expected!r}, got {type(value).__name__}")
            return errors

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                errors.extend(validate_schema(item, properties[key], schema_root, child_path))
            elif isinstance(additional, dict):
                errors.extend(validate_schema(item, additional, schema_root, child_path))
            elif additional is False:
                errors.append(f"{child_path}: unexpected property")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: expected at least {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, schema["items"], schema_root, f"{path}[{index}]"))

    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path}: string is shorter than {schema['minLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
    return errors


def collect_evidence_refs(value: Any, path: str = "$") -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key == "evidence_refs" and isinstance(item, list):
                refs.extend((child, ref) for ref in item if isinstance(ref, str))
            else:
                refs.extend(collect_evidence_refs(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            refs.extend(collect_evidence_refs(item, f"{path}[{index}]"))
    return refs


def semantic_assessment(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    evidence = data.get("evidence", [])
    ids = [item.get("id") for item in evidence if isinstance(item, dict)]
    duplicates = sorted({item for item in ids if item and ids.count(item) > 1})
    if duplicates:
        errors.append(f"$.evidence: duplicate ids: {duplicates}")
    known_ids = set(ids)
    for path, ref in collect_evidence_refs(data):
        if ref not in known_ids:
            errors.append(f"{path}: unknown evidence id {ref!r}")

    compatibility = data.get("compatibility", {})
    statuses = [item.get("status") for item in compatibility.values() if isinstance(item, dict)]
    unknowns = data.get("unknowns", [])
    critical_open = any(
        item.get("critical") is True and item.get("status") == "open"
        for item in unknowns
        if isinstance(item, dict)
    )
    decision = data.get("decision", {})
    recommendation = decision.get("recommendation")
    conditions = decision.get("conditions", [])

    if "blocked" in statuses and recommendation != "stop":
        errors.append("$.decision.recommendation: blocked compatibility requires 'stop'")
    if critical_open and recommendation not in {"needs_information", "stop"}:
        errors.append("$.decision.recommendation: open critical unknown requires 'needs_information' or 'stop'")
    if "adaptation_required" in statuses and recommendation == "proceed":
        errors.append("$.decision.recommendation: adaptations cannot result in unconditional 'proceed'")
    if recommendation == "conditional_proceed" and not conditions:
        errors.append("$.decision.conditions: conditional_proceed requires at least one condition")
    if recommendation != "conditional_proceed" and conditions:
        errors.append("$.decision.conditions: conditions are only valid for conditional_proceed")

    mode = data.get("request", {}).get("mode")
    source_kind = data.get("source", {}).get("kind")
    expected_sources = {
        "migrate_space": "huggingface_space",
        "build_from_model": "modelscope_model",
        "upgrade_existing_studio": "existing_modelscope_studio",
    }
    if mode in expected_sources and source_kind != expected_sources[mode]:
        errors.append(f"$.source.kind: mode {mode!r} expects {expected_sources[mode]!r}")
    return errors


def semantic_verification(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    verification_status = data.get("verification", {}).get("status")
    result_status = data.get("result", {}).get("status")
    checks = data.get("checks", []) + data.get("condition_results", [])
    failed = any(item.get("status") == "failed" for item in checks if isinstance(item, dict))
    pending = any(item.get("status") == "pending" for item in checks if isinstance(item, dict))
    if result_status == "passed" and (failed or pending):
        errors.append("$.result.status: cannot pass while a required check is failed or pending")
    if verification_status == "passed" and result_status != "passed":
        errors.append("$.verification.status: 'passed' requires result.status 'passed'")
    return errors


def validate_file(path: Path) -> list[str]:
    data = load_yaml(path)
    if not isinstance(data, dict):
        return ["$: document must be a mapping"]
    record_type = data.get("record_type")
    schema_path = SCHEMAS.get(record_type)
    if not schema_path:
        return [f"$.record_type: unsupported value {record_type!r}"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = validate_schema(data, schema, schema)
    if record_type == "studio_assessment":
        errors.extend(semantic_assessment(data))
    else:
        errors.extend(semantic_verification(data))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    failed = False
    for path in args.files:
        try:
            errors = validate_file(path)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            errors = [str(exc)]
        if errors:
            failed = True
            print(f"FAIL {path}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK   {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
