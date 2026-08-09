"""Offline torture planning plus an opt-in manual large-document runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from ..document_export import document_manifest
from ..mineru_api import resolve_mineru_config_path
from ..mineru_local_provider import MinerULocalConfig, MinerULocalProvider
from ..mineru_provider import MinerUCloudProvider
from ..parser_provider import ProviderCapabilities
from ..qwen_ocr_provider import QwenOCRConfig, QwenOCRProvider
from .credential_pool import CredentialPool
from .engine import LargeDocumentJobEngine, pymupdf_page_count
from .job_ledger import JobLedger
from .merge import validate_slice_coverage
from .slicing import SlicePlanner


GIB = 1024**3


@dataclass(frozen=True)
class DryRunSlice:
    page_start: int
    page_end: int
    page_count: int
    estimated_bytes: int
    credential_id: Optional[str]


@dataclass(frozen=True)
class DryRunReport:
    provider: str
    total_pages: int
    source_bytes: int
    capabilities: Dict[str, object]
    slice_count: int
    slices: Sequence[DryRunSlice]
    estimated_upload_bytes: int
    estimated_temp_disk_bytes: int
    pages_by_credential: Dict[str, int]
    unassigned_pages: int
    budget_insufficient: bool
    coverage_complete: bool
    coverage_first_page: int
    coverage_last_page: int

    def to_dict(self) -> Dict[str, object]:
        value = asdict(self)
        value["slices"] = [asdict(item) for item in self.slices]
        return value


def build_dry_run_report(
    *,
    provider_id: str,
    total_pages: int,
    source_bytes: int,
    capabilities: ProviderCapabilities,
    credential_specs: Sequence[Mapping[str, object]] = (),
) -> DryRunReport:
    ranges = SlicePlanner().plan(
        total_pages=total_pages,
        total_bytes=source_bytes,
        capabilities=capabilities,
    )
    ordered = validate_slice_coverage(ranges, total_pages)
    page_counts = [item.page_count for item in ranges]
    assignments, pages_by_credential, unassigned = _plan_credentials(
        page_counts, credential_specs
    )
    if not credential_specs:
        assignments = ["provider-default"] * len(ranges)
        pages_by_credential = {"provider-default": total_pages}
        unassigned = 0
    slices = [
        DryRunSlice(
            page_start=item.page_start,
            page_end=item.page_end,
            page_count=item.page_count,
            estimated_bytes=item.estimated_bytes,
            credential_id=assignments[index],
        )
        for index, item in enumerate(ranges)
    ]
    # Physical slices approximately duplicate the source once.  Reserve another
    # 15% (minimum 64 MiB) for normalized results, manifests, and partial files.
    temp_overhead = max(64 * 1024 * 1024, int(source_bytes * 0.15))
    return DryRunReport(
        provider=provider_id,
        total_pages=total_pages,
        source_bytes=source_bytes,
        capabilities={
            "max_pages_per_file": capabilities.max_pages_per_file,
            "max_bytes_per_file": capabilities.max_bytes_per_file,
            "max_concurrency": capabilities.max_concurrency,
            "supports_async_jobs": capabilities.supports_async_jobs,
            "supports_stream_upload": capabilities.supports_stream_upload,
        },
        slice_count=len(slices),
        slices=slices,
        estimated_upload_bytes=source_bytes,
        estimated_temp_disk_bytes=source_bytes + temp_overhead,
        pages_by_credential=pages_by_credential,
        unassigned_pages=unassigned,
        budget_insufficient=unassigned > 0,
        coverage_complete=ordered[0][0] == 1 and ordered[-1][1] == total_pages,
        coverage_first_page=ordered[0][0],
        coverage_last_page=ordered[-1][1],
    )


def _plan_credentials(
    page_counts: Sequence[int], specs: Sequence[Mapping[str, object]]
) -> tuple[List[Optional[str]], Dict[str, int], int]:
    planned = {str(item["id"]): 0 for item in specs}
    budgets = {
        str(item["id"]): (
            int(item["daily_page_budget"])
            if item.get("daily_page_budget") is not None
            else None
        )
        for item in specs
    }
    enabled = {
        str(item["id"])
        for item in specs
        if bool(item.get("enabled", True))
    }
    assignments: List[Optional[str]] = []
    unassigned = 0
    for raw_pages in page_counts:
        pages = int(raw_pages)
        candidates = [
            credential_id
            for credential_id in enabled
            if budgets[credential_id] is None
            or planned[credential_id] + pages <= budgets[credential_id]
        ]
        if not candidates:
            assignments.append(None)
            unassigned += pages
            continue
        selected = min(candidates, key=lambda key: (planned[key], key))
        assignments.append(selected)
        planned[selected] += pages
    return assignments, {key: planned[key] for key in sorted(planned)}, unassigned


def load_credential_specs(path: Optional[Path]) -> List[Dict[str, object]]:
    if path is None:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    raw_items = payload.get("credentials") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raise ValueError("credential config requires a credentials list")
    specs = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("secret_ref"):
            raise ValueError("each credential requires id and secret_ref")
        if any(key in raw for key in ("token", "secret", "api_key")):
            raise ValueError("credential files must contain references, not plaintext secrets")
        specs.append(dict(raw))
    return specs


def streaming_memory_probe(
    *,
    logical_input_bytes: int = 2 * GIB,
    sampled_bytes: int = 32 * 1024 * 1024,
    chunk_size: int = 1024 * 1024,
) -> Dict[str, int]:
    """Hash a generated sample and report peak extra memory for a GB-scale model."""

    remaining = max(0, int(sampled_bytes))
    digest = hashlib.sha256()
    tracemalloc.start()
    while remaining:
        size = min(chunk_size, remaining)
        digest.update(b"\0" * size)
        remaining -= size
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "logical_input_bytes": int(logical_input_bytes),
        "sampled_bytes": int(sampled_bytes),
        "chunk_size": int(chunk_size),
        "peak_extra_bytes": int(peak),
        "digest_prefix": int(digest.hexdigest()[:12], 16),
    }


def _capabilities_from_args(args) -> ProviderCapabilities:
    if args.provider == "qwen-ocr":
        default_pages, default_bytes, asynchronous, streaming = (
            50,
            100 * 1024 * 1024,
            False,
            False,
        )
    elif args.provider == "mineru-cloud":
        default_pages, default_bytes, asynchronous, streaming = 200, None, True, True
    elif args.provider == "mineru-local":
        default_pages, default_bytes, asynchronous, streaming = None, None, True, True
    else:
        default_pages, default_bytes, asynchronous, streaming = 200, None, False, True
    return ProviderCapabilities(
        max_pages_per_file=args.max_pages or default_pages,
        max_bytes_per_file=args.max_bytes or default_bytes,
        max_concurrency=args.max_concurrency,
        supports_async_jobs=asynchronous,
        supports_stream_upload=streaming,
    )


def _secret_resolver(reference: str) -> str:
    if not reference.startswith("env:"):
        raise ValueError("manual runner currently resolves only env:NAME secrets")
    name = reference[4:]
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"environment variable {name} is unset")
    return value


def _build_provider(args):
    if args.provider == "mineru-cloud":
        return MinerUCloudProvider(
            config_path=Path(args.mineru_config),
            max_pages_per_file=args.max_pages or 200,
            max_bytes_per_file=args.max_bytes,
            max_concurrency=args.max_concurrency,
        )
    if args.provider == "mineru-local":
        return MinerULocalProvider(
            MinerULocalConfig(
                endpoint=args.local_endpoint,
                max_pages_per_file=args.max_pages,
                max_bytes_per_file=args.max_bytes,
                max_concurrency=args.max_concurrency,
            )
        )
    if args.provider == "qwen-ocr":
        key = os.environ.get(args.qwen_key_env, "")
        if not key:
            raise ValueError(f"environment variable {args.qwen_key_env} is unset")
        return QwenOCRProvider(
            QwenOCRConfig(
                api_base=args.qwen_api_base,
                api_key=key,
                model=args.qwen_model,
                max_pages_per_file=args.max_pages or 50,
                max_bytes_per_file=args.max_bytes or 100 * 1024 * 1024,
                max_concurrency=args.max_concurrency,
            )
        )
    raise ValueError("synthetic provider is dry-run only")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MEFinder large-document torture dry-run/manual runner"
    )
    parser.add_argument("--pdf", type=Path)
    parser.add_argument(
        "--provider",
        choices=("synthetic", "mineru-cloud", "mineru-local", "qwen-ocr"),
        default="synthetic",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument(
        "--execute",
        action="store_true",
        help="explicitly allow real provider calls (may incur cost)",
    )
    parser.add_argument("--synthetic-pages", type=int, default=7000)
    parser.add_argument("--synthetic-bytes", type=int, default=2 * GIB)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--mineru-config", default=str(resolve_mineru_config_path()))
    parser.add_argument("--local-endpoint", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--qwen-api-base",
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    parser.add_argument("--qwen-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--qwen-model", default="qwen3.5-ocr")
    parser.add_argument("--ledger", type=Path, default=Path("data/parser_jobs.sqlite3"))
    parser.add_argument("--work-dir", type=Path, default=Path("corpus/processed/parser_jobs"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--benchmark-output", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    specs = load_credential_specs(args.credentials)
    if args.pdf is not None:
        pdf = args.pdf.expanduser().resolve()
        if not pdf.is_file():
            raise SystemExit(f"PDF not found: {pdf}")
        total_pages = pymupdf_page_count(pdf)
        source_bytes = pdf.stat().st_size
    else:
        pdf = None
        total_pages = args.synthetic_pages
        source_bytes = args.synthetic_bytes
    capabilities = _capabilities_from_args(args)
    report = build_dry_run_report(
        provider_id=args.provider,
        total_pages=total_pages,
        source_bytes=source_bytes,
        capabilities=capabilities,
        credential_specs=specs,
    )
    if args.dry_run:
        output = report.to_dict()
        output["memory_probe"] = streaming_memory_probe()
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if pdf is None or args.provider == "synthetic":
        raise SystemExit("--execute requires --pdf and a real provider")

    started = time.monotonic()
    provider = _build_provider(args)
    ledger = JobLedger(args.ledger)
    pool = None
    if specs:
        for item in specs:
            ledger.upsert_credential(
                credential_id=str(item["id"]),
                provider_id=provider.provider_id,
                display_name=str(item.get("display_name") or item["id"]),
                secret_ref=str(item["secret_ref"]),
                enabled=bool(item.get("enabled", True)),
                daily_page_budget=(
                    int(item["daily_page_budget"])
                    if item.get("daily_page_budget") is not None
                    else None
                ),
                max_concurrency_override=(
                    int(item["max_concurrency"])
                    if item.get("max_concurrency") is not None
                    else None
                ),
            )
        pool = CredentialPool(
            ledger=ledger,
            provider_id=provider.provider_id,
            secret_resolver=_secret_resolver,
            provider_max_concurrency=provider.capabilities().max_concurrency,
        )
        pool.reconcile_in_flight()
    engine = LargeDocumentJobEngine(
        ledger=ledger,
        provider=provider,
        work_dir=args.work_dir,
        credential_pool=pool,
    )
    source_sha = hashlib.sha256()
    with pdf.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            source_sha.update(chunk)
    digest = source_sha.hexdigest()
    source_file_id = f"pdf-{digest[:16]}"
    job = engine.prepare(
        source_path=pdf,
        source_file_id=source_file_id,
        document_id=source_file_id.upper(),
        model=getattr(getattr(provider, "config", None), "model", None),
    )
    failures = []
    while job.status not in {"validated", "permanent_failure", "cancelled"}:
        job = engine.run_once(job.id)
        if job.status == "waiting":
            time.sleep(max(0.1, args.poll_seconds))
    if job.status != "validated":
        failures.append(job.error_summary or job.status)
    else:
        destination = args.output or pdf.with_suffix(".mefinder.zip")
        manifest = document_manifest(
            document={"document_id": job.document_id, "source_file_id": source_file_id},
            source_sha256=digest,
            source_file={"file_name": pdf.name, "size_bytes": pdf.stat().st_size},
            parser_provider=provider.provider_id,
            parser_model=job.parser_model,
            page_count=job.total_pages,
        )
        job = engine.publish(job.id, manifest=manifest, destination=destination)
    metrics = {
        "provider": provider.provider_id,
        "model": job.parser_model,
        "pages": total_pages,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "failures": failures,
        "estimated_cost": None,
        "actual_cost": None,
        "output_size": (
            Path(job.published_export_path).stat().st_size
            if job.published_export_path
            else None
        ),
        "warning_count": 0,
        "job_id": job.id,
        "status": job.status,
    }
    if args.benchmark_output:
        args.benchmark_output.parent.mkdir(parents=True, exist_ok=True)
        args.benchmark_output.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
