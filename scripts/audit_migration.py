#!/usr/bin/env python3
"""Audit a migrated ModelScope Studio project and emit evidence-oriented JSON."""

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
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".sh", ".js", ".jsx", ".ts", ".tsx"}
SPECIAL_TEXT_FILES = {"Dockerfile", "Makefile", "requirements.txt", "packages.txt", ".gitignore"}
MAX_FILE_BYTES = 2 * 1024 * 1024

RULES: list[dict[str, Any]] = [
    {"id": "remaining_zero_gpu", "pattern": r"@\s*spaces\.GPU\b|spaces\.GPU\s*\(", "category": "runtime_semantics", "severity": "high", "message": "迁移后仍存在 ZeroGPU 调度调用", "paths": ["compatibility.runtime_semantics"]},
    {"id": "remaining_spaces_import", "pattern": r"^\s*(?:from\s+spaces\s+import|import\s+spaces\b)", "category": "runtime_semantics", "severity": "medium", "message": "迁移后仍导入 Hugging Face spaces 包", "paths": ["external_dependencies", "compatibility.runtime_semantics"]},
    {"id": "remaining_hf_download", "pattern": r"\b(?:hf_hub_download|snapshot_download|huggingface_hub|HfApi)\b|https?://huggingface\.co/", "category": "model_source", "severity": "high", "message": "迁移后仍存在 Hugging Face 下载或运行时来源", "paths": ["external_dependencies", "compatibility.model_source", "compatibility.network"]},
    {"id": "github_runtime", "pattern": r"(?:git\s+clone\s+https?://|subprocess\S*\([^\n]*(?:git|github)|os\.system\([^\n]*(?:git|github))", "category": "network", "severity": "high", "message": "发现启动或运行期间执行 Git/GitHub 操作的信号", "paths": ["external_dependencies", "compatibility.network"]},
    {"id": "github_dependency", "pattern": r"git\+https?://|https?://(?:www\.)?github\.com/", "category": "network", "severity": "medium", "message": "发现 GitHub 依赖或链接；确认其阶段和可复现性", "paths": ["external_dependencies", "compatibility.network"]},
    {"id": "remote_code", "pattern": r"trust_remote_code\s*=\s*True|auto_map", "category": "model_source", "severity": "high", "message": "发现动态 remote code 信号", "paths": ["models", "compatibility.model_source"]},
    {"id": "nonpersistent_cache", "pattern": r"(?:cache_dir|HF_HOME|TORCH_HOME|MODELSCOPE_CACHE|TRANSFORMERS_CACHE)\s*[=:]\s*['\"](?:/tmp|\./|\.cache|~/?\.cache)", "category": "storage", "severity": "medium", "message": "缓存可能未使用 /mnt/workspace 持久化目录", "paths": ["compatibility.storage", "resource_requirements.disk_gib"]},
    {"id": "wrong_port", "pattern": r"(?:EXPOSE\s+|port\s*=\s*|--port\s+)(?:8080|8000|5000)\b", "category": "runtime_semantics", "severity": "high", "message": "发现可能不符合创空间要求的监听端口", "paths": ["compatibility.runtime_semantics"]},
    {"id": "loopback_host", "pattern": r"(?:host\s*=\s*|--host\s+|server_name\s*=\s*)['\"]?(?:127\.0\.0\.1|localhost)", "category": "runtime_semantics", "severity": "high", "message": "服务可能只监听 loopback 地址", "paths": ["compatibility.runtime_semantics"]},
    {"id": "cached_examples", "pattern": r"cache_examples\s*=\s*True|run_on_click\s*=\s*True", "category": "runtime_semantics", "severity": "medium", "message": "示例可能在启动或页面交互中自动触发推理", "paths": ["compatibility.runtime_semantics", "workload.concurrency"]},
    {"id": "queue_config", "pattern": r"\.queue\s*\(|default_concurrency_limit|max_threads|concurrency_limit", "category": "runtime_semantics", "severity": "info", "message": "发现队列或并发配置；需核对是否与模型可变缓存兼容", "paths": ["workload.concurrency", "compatibility.runtime_semantics"]},
    {"id": "gpu_specific_signal", "pattern": r"(?:\bnvfp4\b|\bfp4\b|blackwell|sm_?100|sm_?120|\baoti\b|aot_inductor|torch\._inductor|flash_attn|xformers)", "category": "gpu_architecture", "severity": "medium", "message": "迁移代码中仍存在 GPU 架构或专用 kernel 信号", "paths": ["compatibility.gpu_architecture", "compatibility.cuda_kernels"]},
]

SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|passwd|cookie|private[_-]?key)\b\s*[:=]\s*(['\"])([^'\"\n]{6,})\2"
)
ENV_READ_RE = re.compile(r"(?:os\.(?:getenv|environ\.get)|process\.env|\$\{?[A-Z][A-Z0-9_]*\}?)")
URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@", re.IGNORECASE)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{6,}")
TOKEN_SHAPE_RE = re.compile(r"\b(?:sk|hf|ms)-[A-Za-z0-9_-]{8,}\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def git_revision(root: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def iter_text_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.stat().st_size <= MAX_FILE_BYTES and (path.name in SPECIAL_TEXT_FILES or path.suffix.lower() in TEXT_SUFFIXES):
            yield path


def finding_id(rule_id: str, rel: str, line: int) -> str:
    digest = hashlib.sha1(f"{rule_id}:{rel}:{line}".encode()).hexdigest()[:10]
    return f"{rule_id}-{digest}"


def compact(text: str) -> str:
    text = SECRET_ASSIGN_RE.sub(lambda m: m.group(0).replace(m.group(3), "<redacted>"), text)
    text = URL_CREDENTIAL_RE.sub(r"\1<redacted>@", text)
    text = BEARER_RE.sub("Bearer <redacted>", text)
    text = TOKEN_SHAPE_RE.sub("<redacted-token>", text)
    return " ".join(text.strip().split())[:240]


def make_finding(rule: dict[str, Any], rel: str, line: int, excerpt: str) -> dict[str, Any]:
    return {
        "id": finding_id(rule["id"], rel, line),
        "rule_id": rule["id"],
        "category": rule["category"],
        "severity": rule["severity"],
        "kind": "observed",
        "message": rule["message"],
        "locator": {"path": rel, "line": line},
        "excerpt": compact(excerpt),
        "assessment_paths": rule["paths"],
    }


def audit(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    files_scanned = 0
    has_workspace_cache = False
    has_queue_signal = False
    has_license = False
    has_readme = False
    dependency_files: list[str] = []

    for path in iter_text_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files_scanned += 1
        rel = path.relative_to(root).as_posix()
        lower_name = path.name.lower()
        has_license = has_license or lower_name.startswith("license")
        has_readme = has_readme or lower_name.startswith("readme")
        has_workspace_cache = has_workspace_cache or "/mnt/workspace" in text
        if path.name in {"requirements.txt", "pyproject.toml", "environment.yml", "package.json", "packages.txt"}:
            dependency_files.append(rel)
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                if re.search(rule["pattern"], line, flags=re.IGNORECASE):
                    findings.append(make_finding(rule, rel, line_no, line))
                    has_queue_signal = has_queue_signal or rule["id"] == "queue_config"
            secret = SECRET_ASSIGN_RE.search(line)
            if secret and not ENV_READ_RE.search(line):
                rule = {
                    "id": "possible_hardcoded_secret",
                    "category": "security",
                    "severity": "high",
                    "message": f"发现疑似硬编码秘密变量：{secret.group(1)}",
                    "paths": ["secrets"],
                }
                findings.append(make_finding(rule, rel, line_no, f"{secret.group(1)} = <redacted>"))

    unknowns: list[dict[str, str]] = []
    if not has_workspace_cache:
        unknowns.append({"id": "persistent-cache-not-observed", "question": "大型模型是否使用 /mnt/workspace 持久化缓存？", "assessment_path": "compatibility.storage"})
    if not has_queue_signal:
        unknowns.append({"id": "concurrency-not-observed", "question": "创空间队列和最大并发如何限制？", "assessment_path": "workload.concurrency"})
    if not has_license:
        unknowns.append({"id": "license-file-not-observed", "question": "模型、上游 Space 与迁移代码的许可证如何展示？", "assessment_path": "compatibility.license"})
    if not has_readme:
        unknowns.append({"id": "attribution-not-observed", "question": "README 是否包含上游署名、用途限制和非官方说明？", "assessment_path": "reuse.attribution_required"})

    counts = Counter(item["severity"] for item in findings)
    return {
        "schema_version": "1.0",
        "report_type": "migration_audit",
        "generated_at": utc_now(),
        "tool": {"name": "audit_migration.py", "version": VERSION},
        "target": {"path": str(root.resolve()), "revision": git_revision(root)},
        "facts": {
            "dependency_files": sorted(dependency_files),
            "persistent_workspace_reference_observed": has_workspace_cache,
            "queue_configuration_observed": has_queue_signal,
            "readme_observed": has_readme,
            "license_file_observed": has_license,
        },
        "findings": findings,
        "unknowns": unknowns,
        "summary": {"files_scanned": files_scanned, "findings": len(findings), "finding_counts": dict(sorted(counts.items()))},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="migrated Studio project directory")
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args()
    if not args.path.is_dir():
        parser.error(f"not a directory: {args.path}")
    payload = json.dumps(audit(args.path.resolve()), ensure_ascii=False, indent=None if args.compact else 2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
