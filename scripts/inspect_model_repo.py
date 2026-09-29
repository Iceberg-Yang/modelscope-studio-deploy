#!/usr/bin/env python3
"""Inspect a local ModelScope model snapshot and emit evidence-oriented JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0"
WEIGHT_SUFFIXES = {".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".sh"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".cache"}
MAX_TEXT_BYTES = 4 * 1024 * 1024
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|passwd|cookie|private[_-]?key)\s*[:=]\s*(['\"])([^'\"\n]{6,})\1"
)
URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@", re.IGNORECASE)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{6,}")
TOKEN_SHAPE_RE = re.compile(r"\b(?:sk|hf|ms)-[A-Za-z0-9_-]{8,}\b")

RULES: list[dict[str, Any]] = [
    {"id": "remote_code", "pattern": r"trust_remote_code\s*=\s*True|['\"]auto_map['\"]\s*:", "category": "model_source", "severity": "high", "message": "发现 remote code 或 auto_map 信号", "paths": ["models", "compatibility.model_source"]},
    {"id": "hf_reference", "pattern": r"\b(?:hf_hub_download|huggingface_hub|HfApi)\b|https?://huggingface\.co/", "category": "model_source", "severity": "medium", "message": "模型仓库中包含 Hugging Face 来源或下载信号", "paths": ["external_dependencies", "compatibility.model_source"]},
    {"id": "github_reference", "pattern": r"git\+https?://|https?://(?:www\.)?github\.com/", "category": "network", "severity": "low", "message": "模型资料中包含 GitHub 参考或依赖", "paths": ["reference_implementations", "external_dependencies"]},
    {"id": "fp8_signal", "pattern": r"\b(?:fp8|float8_e[45]m[23](?:fnuz|fn)?)\b", "category": "precision", "severity": "medium", "message": "发现 FP8/float8 精度信号", "paths": ["models", "compatibility.precision"]},
    {"id": "fp4_blackwell_signal", "pattern": r"(?:\bnvfp4\b|\bfp4\b|blackwell|sm_?100|sm_?120)", "category": "gpu_architecture", "severity": "high", "message": "发现 Blackwell、FP4 或特定 SM 信号", "paths": ["compatibility.gpu_architecture", "compatibility.precision"]},
    {"id": "attention_backend", "pattern": r"\b(?:flash_attn|flash-attn|FlashAttention|xformers)\b", "category": "cuda_kernels", "severity": "medium", "message": "发现专用 attention backend", "paths": ["compatibility.cuda_kernels"]},
    {"id": "custom_cuda", "pattern": r"CUDAExtension|cpp_extension|load_inline\s*\(|\.cu\b", "category": "cuda_kernels", "severity": "high", "message": "发现自定义 CUDA extension 信号", "paths": ["compatibility.cuda_kernels"]},
    {"id": "compile_signal", "pattern": r"\b(?:aoti|aot_inductor|torch\.compile|torch\.export|torch\._inductor)\b", "category": "framework", "severity": "medium", "message": "发现编译、export 或 AoTI 信号", "paths": ["compatibility.framework", "resource_requirements.startup"]},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def git_revision(root: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            yield path


def parse_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def walk_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key == key:
                found.append(child)
            found.extend(walk_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_values(child, key))
    return found


def parse_frontmatter(text: str | None) -> dict[str, str]:
    if not text or not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[3:end].splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", line)
        if match and match.group(2):
            result[match.group(1)] = match.group(2).strip().strip("'\"")
    return result


def finding_id(rule_id: str, rel: str, line: int) -> str:
    digest = hashlib.sha1(f"{rule_id}:{rel}:{line}".encode()).hexdigest()[:10]
    return f"{rule_id}-{digest}"


def make_finding(rule: dict[str, Any], rel: str, line: int, excerpt: str) -> dict[str, Any]:
    excerpt = SECRET_VALUE_RE.sub(lambda m: m.group(0).replace(m.group(2), "<redacted>"), excerpt)
    excerpt = URL_CREDENTIAL_RE.sub(r"\1<redacted>@", excerpt)
    excerpt = BEARER_RE.sub("Bearer <redacted>", excerpt)
    excerpt = TOKEN_SHAPE_RE.sub("<redacted-token>", excerpt)
    return {
        "id": finding_id(rule["id"], rel, line),
        "rule_id": rule["id"],
        "category": rule["category"],
        "severity": rule["severity"],
        "kind": "observed",
        "message": rule["message"],
        "locator": {"path": rel, "line": line},
        "excerpt": " ".join(excerpt.strip().split())[:240],
        "assessment_paths": rule["paths"],
    }


def inspect(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    weight_files: list[dict[str, Any]] = []
    configs: dict[str, Any] = {}
    architectures: set[str] = set()
    dtypes: set[str] = set()
    model_types: set[str] = set()
    components: set[str] = set()
    quantization: list[Any] = []
    remote_sources: set[str] = set()
    official_links: set[str] = set()
    readme_text: str | None = None
    text_files_scanned = 0

    repo_id_re = re.compile(r"(?<![\w.-])([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.\-/]*)(?![\w.-])")
    link_re = re.compile(r"https?://[^\s)>'\"]+")

    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in WEIGHT_SUFFIXES:
            size = path.stat().st_size
            weight_files.append({"path": rel, "format": suffix.lstrip("."), "bytes": size, "gib": round(size / (1024 ** 3), 4)})
            continue
        if suffix == ".json" and path.stat().st_size <= MAX_TEXT_BYTES:
            data = parse_json(path)
            if data is not None:
                configs[rel] = data
                for value in walk_values(data, "architectures"):
                    if isinstance(value, list):
                        architectures.update(str(item) for item in value)
                for key in ("torch_dtype", "dtype"):
                    for value in walk_values(data, key):
                        if isinstance(value, str):
                            dtypes.add(value)
                for value in walk_values(data, "model_type"):
                    if isinstance(value, str):
                        model_types.add(value)
                quantization.extend(walk_values(data, "quantization_config"))
                for value in walk_values(data, "_name_or_path") + walk_values(data, "repo_id"):
                    if isinstance(value, str) and repo_id_re.fullmatch(value):
                        remote_sources.add(value)
                if path.name in {"model_index.json", "modular_model_index.json"} and isinstance(data, dict):
                    components.update(key for key in data if not key.startswith("_") and key not in {"metadata"})
        if path.stat().st_size > MAX_TEXT_BYTES or suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text_files_scanned += 1
        if path.name.lower().startswith("readme") and readme_text is None:
            readme_text = text
            for link in link_re.findall(text):
                if any(domain in link for domain in ("github.com", "modelscope.cn", "huggingface.co/spaces")):
                    official_links.add(link.rstrip(".,"))
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                if re.search(rule["pattern"], line, flags=re.IGNORECASE):
                    findings.append(make_finding(rule, rel, line_no, line))

    config_root = configs.get("config.json")
    if isinstance(config_root, dict):
        if "auto_map" in config_root:
            remote_sources.update(str(value) for value in config_root["auto_map"].values() if isinstance(value, str))

    frontmatter = parse_frontmatter(readme_text)
    license_value: str | None = frontmatter.get("license")
    if not license_value:
        license_files = [path.name for path in root.iterdir() if path.is_file() and path.name.lower().startswith("license")]
        license_value = license_files[0] if license_files else None

    total_weight_bytes = sum(item["bytes"] for item in weight_files)
    unknowns: list[dict[str, str]] = []
    if not architectures and not model_types:
        unknowns.append({"id": "architecture-not-found", "question": "模型架构和 model_type 是什么？", "assessment_path": "models"})
    if not dtypes:
        unknowns.append({"id": "dtype-not-found", "question": "官方推荐加载精度是什么？", "assessment_path": "models"})
    if not license_value:
        unknowns.append({"id": "license-not-found", "question": "模型许可证及公开 Demo 限制是什么？", "assessment_path": "compatibility.license"})
    if readme_text is None:
        unknowns.append({"id": "model-card-not-found", "question": "官方 Model Card、示例和推荐工作负载在哪里？", "assessment_path": "workload.acceptance_envelope"})
    if not weight_files:
        unknowns.append({"id": "weights-not-found", "question": "权重是否尚未下载，或使用了未识别的格式？", "assessment_path": "resource_requirements.model_storage_gb"})

    counts = Counter(item["severity"] for item in findings)
    return {
        "schema_version": "1.0",
        "report_type": "model_repo_inspection",
        "generated_at": utc_now(),
        "tool": {"name": "inspect_model_repo.py", "version": VERSION},
        "target": {"path": str(root.resolve()), "revision": git_revision(root)},
        "facts": {
            "model_card_frontmatter": frontmatter,
            "license": license_value,
            "architectures": sorted(architectures),
            "model_types": sorted(model_types),
            "dtypes": sorted(dtypes),
            "components": sorted(components),
            "quantization_configs": quantization,
            "weight_files": weight_files,
            "total_weight_bytes": total_weight_bytes,
            "total_weight_gib": round(total_weight_bytes / (1024 ** 3), 4),
            "remote_source_candidates": sorted(remote_sources),
            "official_reference_candidates": sorted(official_links),
            "config_files": sorted(configs),
        },
        "findings": findings,
        "unknowns": unknowns,
        "summary": {"files_scanned": text_files_scanned, "weight_files": len(weight_files), "findings": len(findings), "finding_counts": dict(sorted(counts.items()))},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="local ModelScope model snapshot")
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args()
    if not args.path.is_dir():
        parser.error(f"not a directory: {args.path}")
    payload = json.dumps(inspect(args.path.resolve()), ensure_ascii=False, indent=None if args.compact else 2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
