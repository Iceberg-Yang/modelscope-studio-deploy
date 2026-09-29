#!/usr/bin/env python3
"""Inspect a local Hugging Face Space repository and emit evidence-oriented JSON."""

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
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache", "dist", "build"}
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg",
    ".sh", ".dockerfile", ".js", ".jsx", ".ts", ".tsx",
}
SPECIAL_TEXT_FILES = {"Dockerfile", "Makefile", "requirements.txt", "packages.txt"}
MAX_FILE_BYTES = 2 * 1024 * 1024

RULES: list[dict[str, Any]] = [
    {"id": "zero_gpu_decorator", "pattern": r"@\s*spaces\.GPU\b|spaces\.GPU\s*\(", "category": "runtime_semantics", "severity": "high", "message": "发现 Hugging Face ZeroGPU 调度调用", "paths": ["source_runtime", "compatibility.runtime_semantics"]},
    {"id": "zero_gpu_size", "pattern": r"\bsize\s*=\s*['\"](?:large|xlarge)['\"]|\bzerogpu\b|\bzero-gpu\b", "category": "memory", "severity": "medium", "message": "发现 ZeroGPU 档位或声明；该信息只能作为资源事实", "paths": ["source_runtime", "compatibility.memory"]},
    {"id": "gpu_duration", "pattern": r"\bduration\s*=\s*[^,)]+", "category": "runtime_semantics", "severity": "low", "message": "发现动态 GPU duration 配置", "paths": ["source_runtime", "compatibility.runtime_semantics"]},
    {"id": "fp8_signal", "pattern": r"\b(?:fp8|float8_e[45]m[23](?:fnuz|fn)?)\b", "category": "precision", "severity": "medium", "message": "发现 FP8/float8 信号；需结合具体算子和回退路径判断", "paths": ["compatibility.precision"]},
    {"id": "fp4_blackwell_signal", "pattern": r"(?:\bnvfp4\b|\bfp4\b|blackwell|sm_?100|sm_?120|compute[_ ]capability\s*[>=]+\s*1[02])", "category": "gpu_architecture", "severity": "high", "message": "发现 Blackwell、FP4 或特定 SM 信号", "paths": ["compatibility.gpu_architecture", "compatibility.precision"]},
    {"id": "aoti_signal", "pattern": r"\b(?:aoti|aot_inductor|torch\._inductor|torch\.export)\b", "category": "cuda_kernels", "severity": "high", "message": "发现 AoTI/export 或预编译路径信号", "paths": ["compatibility.cuda_kernels", "compatibility.framework"]},
    {"id": "torch_compile", "pattern": r"torch\.compile\s*\(", "category": "framework", "severity": "medium", "message": "发现 torch.compile；需确认目标环境编译与启动成本", "paths": ["compatibility.framework", "resource_requirements.startup"]},
    {"id": "attention_backend", "pattern": r"\b(?:flash_attn|flash-attn|FlashAttention|xformers|scaled_dot_product_attention)\b", "category": "cuda_kernels", "severity": "medium", "message": "发现专用 attention backend", "paths": ["compatibility.cuda_kernels"]},
    {"id": "custom_cuda", "pattern": r"CUDAExtension|load_inline\s*\(|cpp_extension|\.cu\b", "category": "cuda_kernels", "severity": "high", "message": "发现自定义 CUDA extension 信号", "paths": ["compatibility.cuda_kernels"]},
    {"id": "remote_code", "pattern": r"trust_remote_code\s*=\s*True|auto_map", "category": "model_source", "severity": "high", "message": "发现动态 remote code 信号", "paths": ["models", "compatibility.model_source"]},
    {"id": "hf_runtime_dependency", "pattern": r"\b(?:hf_hub_download|snapshot_download|huggingface_hub|HfApi)\b|https?://huggingface\.co/", "category": "model_source", "severity": "medium", "message": "发现 Hugging Face 下载或运行时依赖", "paths": ["external_dependencies", "compatibility.model_source", "compatibility.network"]},
    {"id": "github_dependency", "pattern": r"(?:git\+https?://|git\s+clone\s+https?://|https?://(?:www\.)?github\.com/)", "category": "network", "severity": "medium", "message": "发现 GitHub 来源或运行时依赖", "paths": ["external_dependencies", "compatibility.network"]},
    {"id": "remote_model_index", "pattern": r"(?:model|modular_model)_index\.json|_class_name", "category": "model_source", "severity": "low", "message": "发现 pipeline/model index；需检查内部组件是否仍指向远程仓库", "paths": ["models", "external_dependencies"]},
    {"id": "cached_examples", "pattern": r"cache_examples\s*=\s*True|run_on_click\s*=\s*True", "category": "runtime_semantics", "severity": "medium", "message": "发现可能自动执行的示例配置", "paths": ["compatibility.runtime_semantics"]},
    {"id": "queue_concurrency", "pattern": r"\.queue\s*\(|default_concurrency_limit|max_threads|concurrency_limit", "category": "runtime_semantics", "severity": "low", "message": "发现队列或并发配置", "paths": ["workload.concurrency", "compatibility.runtime_semantics"]},
]

MODEL_CALL_RE = re.compile(
    r"(?:from_pretrained|hf_hub_download|snapshot_download)\s*\(\s*(?:repo_id\s*=\s*)?['\"]([\w.-]+/[\w.\-/]+)['\"]"
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|passwd|cookie|private[_-]?key)\s*[:=]\s*(['\"])([^'\"\n]{6,})\1"
)
URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@", re.IGNORECASE)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{6,}")
TOKEN_SHAPE_RE = re.compile(r"\b(?:sk|hf|ms)-[A-Za-z0-9_-]{8,}\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def git_revision(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def iter_text_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        if path.name in SPECIAL_TEXT_FILES or path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def redact(text: str) -> str:
    text = SECRET_VALUE_RE.sub(lambda m: m.group(0).replace(m.group(2), "<redacted>"), text)
    text = URL_CREDENTIAL_RE.sub(r"\1<redacted>@", text)
    text = BEARER_RE.sub("Bearer <redacted>", text)
    text = TOKEN_SHAPE_RE.sub("<redacted-token>", text)
    return " ".join(text.strip().split())[:240]


def finding_id(rule_id: str, rel_path: str, line: int) -> str:
    digest = hashlib.sha1(f"{rule_id}:{rel_path}:{line}".encode()).hexdigest()[:10]
    return f"{rule_id}-{digest}"


def add_finding(findings: list[dict[str, Any]], rule: dict[str, Any], rel_path: str, line: int, excerpt: str) -> None:
    findings.append({
        "id": finding_id(rule["id"], rel_path, line),
        "rule_id": rule["id"],
        "category": rule["category"],
        "severity": rule["severity"],
        "kind": "observed",
        "message": rule["message"],
        "locator": {"path": rel_path, "line": line},
        "excerpt": redact(excerpt),
        "assessment_paths": rule["paths"],
    })


def parse_frontmatter(readme: str | None) -> dict[str, Any]:
    if not readme or not readme.startswith("---"):
        return {}
    end = readme.find("\n---", 3)
    if end < 0:
        return {}
    result: dict[str, Any] = {}
    for raw in readme[3:end].splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", raw)
        if match and match.group(2):
            value = match.group(2).strip().strip("'\"")
            result[match.group(1)] = value
    return result


def inspect(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    model_candidates: set[str] = set()
    dependency_files: list[str] = []
    files_scanned = 0
    readme_text: str | None = None

    for path in iter_text_files(root):
        text = read_text(path)
        if text is None:
            continue
        files_scanned += 1
        rel = path.relative_to(root).as_posix()
        if path.name.lower().startswith("readme") and readme_text is None:
            readme_text = text
        if path.name in {"requirements.txt", "pyproject.toml", "environment.yml", "package.json", "packages.txt"}:
            dependency_files.append(rel)
        model_candidates.update(MODEL_CALL_RE.findall(text))
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                if re.search(rule["pattern"], line, flags=re.IGNORECASE):
                    add_finding(findings, rule, rel, line_no, line)

    frontmatter = parse_frontmatter(readme_text)
    license_value = frontmatter.get("license")
    if not license_value:
        license_files = [p.name for p in root.iterdir() if p.is_file() and p.name.lower().startswith("license")]
        license_value = license_files[0] if license_files else None

    unknowns: list[dict[str, str]] = []
    if not license_value:
        unknowns.append({"id": "license-not-found", "question": "Space 的许可证与迁移署名要求是什么？", "assessment_path": "compatibility.license"})
    if not frontmatter.get("sdk"):
        unknowns.append({"id": "sdk-not-declared", "question": "Space 使用的 SDK 与入口文件是什么？", "assessment_path": "source_runtime.sdk"})
    if not model_candidates and "models" not in frontmatter:
        unknowns.append({"id": "model-not-found", "question": "Space 实际加载哪些主模型与辅助组件？", "assessment_path": "models"})

    counts = Counter(item["severity"] for item in findings)
    return {
        "schema_version": "1.0",
        "report_type": "hf_space_inspection",
        "generated_at": utc_now(),
        "tool": {"name": "inspect_hf_space.py", "version": VERSION},
        "target": {"path": str(root.resolve()), "revision": git_revision(root)},
        "facts": {
            "readme_frontmatter": frontmatter,
            "sdk": frontmatter.get("sdk"),
            "entry_file": frontmatter.get("app_file"),
            "declared_hardware": frontmatter.get("hardware"),
            "license": license_value,
            "model_candidates": sorted(model_candidates),
            "dependency_files": sorted(dependency_files),
        },
        "findings": findings,
        "unknowns": unknowns,
        "summary": {"files_scanned": files_scanned, "findings": len(findings), "finding_counts": dict(sorted(counts.items()))},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="local Hugging Face Space repository")
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args()
    if not args.path.is_dir():
        parser.error(f"not a directory: {args.path}")
    report = inspect(args.path.resolve())
    payload = json.dumps(report, ensure_ascii=False, indent=None if args.compact else 2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
