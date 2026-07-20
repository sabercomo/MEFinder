"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Set

from .indexer import (
    DEFAULT_CORPUS_DIR,
    DEFAULT_DATABASE_PATH,
    DEFAULT_INDEX_PATH,
    DEFAULT_PARSED_PDF_DIR,
    DEFAULT_PDF_CONFIG_PATH,
    DEFAULT_PDF_CORPUS_DIR,
    build_index,
)
from .mineru_api import (
    DEFAULT_MINERU_AGENT_INPUT_DIR,
    DEFAULT_MINERU_AGENT_RESULT_DIR,
    DEFAULT_MINERU_CONFIG_PATH,
    DEFAULT_MINERU_MANIFEST_DIR,
    DEFAULT_MINERU_RESULT_DIR,
    DEFAULT_MINERU_STATE_DIR,
    MinerUError,
    download_agent_result,
    download_done_results,
    get_agent_task_status,
    get_batch_status,
    submit_agent_pdf,
    submit_local_pdf,
    submit_local_pdf_segments,
)
from .search import SearchEngine
from .web import serve


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="me_finder")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-index", help="build local corpus index")
    build.add_argument("--corpus", default=str(DEFAULT_CORPUS_DIR))
    build.add_argument("--index", default=str(DEFAULT_INDEX_PATH))
    build.add_argument("--database", default=str(DEFAULT_DATABASE_PATH), help="SQLite database path")
    build.add_argument("--include-pdf", action="store_true", help="include configured PDF documents")
    build.add_argument("--pdf-corpus", default=str(DEFAULT_PDF_CORPUS_DIR))
    build.add_argument("--pdf-config", default=str(DEFAULT_PDF_CONFIG_PATH))
    build.add_argument("--parsed-pdf-dir", default=str(DEFAULT_PARSED_PDF_DIR))
    build.add_argument("--pdf-limit", type=int, default=None)
    build.add_argument("--no-backup", action="store_true", help="do not back up an existing index before writing")
    build.add_argument("--export-json", action="store_true", help="also write the JSON index backup (data/index.json, ~300MB); SQLite is always built")

    search = sub.add_parser("search", help="search the local index")
    search.add_argument("query")
    search.add_argument("--mode", default="auto")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--source-type", default="all", choices=["all", "word", "pdf"])
    search.add_argument("--index", default=str(DEFAULT_DATABASE_PATH))

    web = sub.add_parser("serve", help="start local web interface")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--index", default=str(DEFAULT_DATABASE_PATH))

    mineru_submit = sub.add_parser("mineru-submit", help="submit one local PDF to MinerU")
    mineru_submit.add_argument("pdf")
    mineru_submit.add_argument("--config", default=str(DEFAULT_MINERU_CONFIG_PATH))
    mineru_submit.add_argument("--state-dir", default=str(DEFAULT_MINERU_STATE_DIR))
    mineru_submit.add_argument("--data-id", default=None)
    mineru_submit.add_argument("--page-ranges", default=None, help="MinerU page range, for example 1-20")
    mineru_submit.add_argument("--model-version", default="vlm")
    mineru_submit.add_argument("--language", default="ch")
    mineru_submit.add_argument("--no-ocr", action="store_false", dest="is_ocr")
    mineru_submit.add_argument("--disable-table", action="store_false", dest="enable_table")
    mineru_submit.add_argument("--disable-formula", action="store_false", dest="enable_formula")
    mineru_submit.set_defaults(is_ocr=True, enable_table=True, enable_formula=True)

    mineru_segments = sub.add_parser("mineru-submit-segments", help="submit one PDF in <=200 page MinerU segments")
    mineru_segments.add_argument("pdf")
    mineru_segments.add_argument("--config", default=str(DEFAULT_MINERU_CONFIG_PATH))
    mineru_segments.add_argument("--state-dir", default=str(DEFAULT_MINERU_STATE_DIR))
    mineru_segments.add_argument("--manifest-dir", default=str(DEFAULT_MINERU_MANIFEST_DIR))
    mineru_segments.add_argument("--result-dir", default=str(DEFAULT_MINERU_RESULT_DIR))
    mineru_segments.add_argument("--data-id-prefix", default=None)
    mineru_segments.add_argument("--segment-size", type=int, default=200)
    mineru_segments.add_argument("--start-page", type=int, default=1)
    mineru_segments.add_argument("--end-page", type=int, default=None)
    mineru_segments.add_argument("--model-version", default="vlm")
    mineru_segments.add_argument("--language", default="ch")
    mineru_segments.add_argument("--no-ocr", action="store_false", dest="is_ocr")
    mineru_segments.add_argument("--disable-table", action="store_false", dest="enable_table")
    mineru_segments.add_argument("--disable-formula", action="store_false", dest="enable_formula")
    mineru_segments.add_argument("--wait", action="store_true", help="poll until submitted segments finish")
    mineru_segments.add_argument("--download", action="store_true", help="download completed segment results while waiting")
    mineru_segments.add_argument("--poll-seconds", type=int, default=30)
    mineru_segments.add_argument("--timeout-minutes", type=int, default=90)
    mineru_segments.set_defaults(is_ocr=True, enable_table=True, enable_formula=True)

    mineru_status = sub.add_parser("mineru-status", help="check a MinerU batch")
    mineru_status.add_argument("batch_id")
    mineru_status.add_argument("--config", default=str(DEFAULT_MINERU_CONFIG_PATH))
    mineru_status.add_argument("--state-dir", default=str(DEFAULT_MINERU_STATE_DIR))
    mineru_status.add_argument("--json", action="store_true", help="print raw MinerU response")

    mineru_download = sub.add_parser("mineru-download", help="download completed MinerU results")
    mineru_download.add_argument("batch_id")
    mineru_download.add_argument("--config", default=str(DEFAULT_MINERU_CONFIG_PATH))
    mineru_download.add_argument("--state-dir", default=str(DEFAULT_MINERU_STATE_DIR))
    mineru_download.add_argument("--result-dir", default=str(DEFAULT_MINERU_RESULT_DIR))

    agent_submit = sub.add_parser("mineru-agent-submit", help="submit one small PDF to MinerU Agent API")
    agent_submit.add_argument("pdf")
    agent_submit.add_argument("--state-dir", default=str(DEFAULT_MINERU_STATE_DIR))
    agent_submit.add_argument("--input-dir", default=str(DEFAULT_MINERU_AGENT_INPUT_DIR))
    agent_submit.add_argument("--data-id", default=None)
    agent_submit.add_argument("--page-range", default=None, help="simple range like 1-20")
    agent_submit.add_argument("--language", default="ch")
    agent_submit.add_argument("--api-base", default="https://mineru.net")
    agent_submit.add_argument("--no-ocr", action="store_false", dest="is_ocr")
    agent_submit.add_argument("--disable-table", action="store_false", dest="enable_table")
    agent_submit.add_argument("--disable-formula", action="store_false", dest="enable_formula")
    agent_submit.set_defaults(is_ocr=True, enable_table=True, enable_formula=True)

    agent_status = sub.add_parser("mineru-agent-status", help="check a MinerU Agent task")
    agent_status.add_argument("task_id")
    agent_status.add_argument("--state-dir", default=str(DEFAULT_MINERU_STATE_DIR))
    agent_status.add_argument("--api-base", default="https://mineru.net")
    agent_status.add_argument("--json", action="store_true", help="print raw MinerU response")

    agent_download = sub.add_parser("mineru-agent-download", help="download a completed MinerU Agent markdown")
    agent_download.add_argument("task_id")
    agent_download.add_argument("--state-dir", default=str(DEFAULT_MINERU_STATE_DIR))
    agent_download.add_argument("--result-dir", default=str(DEFAULT_MINERU_AGENT_RESULT_DIR))
    agent_download.add_argument("--api-base", default="https://mineru.net")

    args = parser.parse_args()
    if args.command == "build-index":
        index = build_index(
            Path(args.corpus),
            Path(args.index),
            include_pdf=args.include_pdf,
            pdf_corpus_dir=Path(args.pdf_corpus),
            pdf_config_path=Path(args.pdf_config),
            parsed_pdf_dir=Path(args.parsed_pdf_dir),
            database_path=Path(args.database),
            pdf_limit=args.pdf_limit,
            backup_existing=not args.no_backup,
            export_json=args.export_json,
        )
        meta = index["metadata"]
        built = f"{args.index} and {args.database}" if args.export_json else args.database
        print(
            f"Built {built}: {meta['source_count']} source files, "
            f"{meta['paragraph_count']} paragraphs, {meta['eligible_paragraph_count']} searchable."
        )
    elif args.command == "search":
        engine = SearchEngine(Path(args.index))
        result = engine.search(args.query, args.mode, args.limit, args.source_type)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "serve":
        serve(args.host, args.port, Path(args.index))
    elif args.command == "mineru-submit":
        try:
            state = submit_local_pdf(
                Path(args.pdf),
                config_path=Path(args.config),
                state_dir=Path(args.state_dir),
                data_id=args.data_id,
                page_ranges=args.page_ranges,
                model_version=args.model_version,
                language=args.language,
                is_ocr=args.is_ocr,
                enable_table=args.enable_table,
                enable_formula=args.enable_formula,
            )
        except MinerUError as exc:
            raise SystemExit(f"MinerU submit failed: {exc}") from exc
        print("MinerU task submitted.")
        print(f"batch_id: {state['batch_id']}")
        print(f"data_id: {state['data_id']}")
        if state.get("page_ranges"):
            print(f"page_ranges: {state['page_ranges']}")
        print(f"state_file: {Path(args.state_dir) / (str(state['batch_id']) + '.json')}")
    elif args.command == "mineru-submit-segments":
        try:
            manifest = submit_local_pdf_segments(
                Path(args.pdf),
                config_path=Path(args.config),
                state_dir=Path(args.state_dir),
                manifest_dir=Path(args.manifest_dir),
                data_id_prefix=args.data_id_prefix,
                segment_size=args.segment_size,
                start_page=args.start_page,
                end_page=args.end_page,
                model_version=args.model_version,
                language=args.language,
                is_ocr=args.is_ocr,
                enable_table=args.enable_table,
                enable_formula=args.enable_formula,
            )
        except MinerUError as exc:
            raise SystemExit(f"MinerU segmented submit failed: {exc}") from exc
        print("MinerU segmented tasks prepared.")
        print(f"manifest: {manifest['manifest_path']}")
        for segment in manifest.get("segments", []):
            if not isinstance(segment, dict):
                continue
            line = f"{segment.get('page_ranges')}: {segment.get('status')}"
            if segment.get("batch_id"):
                line += f" batch_id={segment['batch_id']}"
            if segment.get("result_dir"):
                line += f" result_dir={segment['result_dir']}"
            print(line)
        if args.wait:
            wait_for_mineru_segments(
                manifest,
                config_path=Path(args.config),
                state_dir=Path(args.state_dir),
                result_dir=Path(args.result_dir),
                poll_seconds=max(5, args.poll_seconds),
                timeout_minutes=max(1, args.timeout_minutes),
                download=args.download,
            )
    elif args.command == "mineru-status":
        try:
            result = get_batch_status(args.batch_id, config_path=Path(args.config), state_dir=Path(args.state_dir))
        except MinerUError as exc:
            raise SystemExit(f"MinerU status failed: {exc}") from exc
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_mineru_status(result)
    elif args.command == "mineru-download":
        try:
            paths = download_done_results(
                args.batch_id,
                config_path=Path(args.config),
                state_dir=Path(args.state_dir),
                result_dir=Path(args.result_dir),
            )
        except MinerUError as exc:
            raise SystemExit(f"MinerU download failed: {exc}") from exc
        print("Downloaded MinerU result:")
        for path in paths:
            print(path)
    elif args.command == "mineru-agent-submit":
        try:
            state = submit_agent_pdf(
                Path(args.pdf),
                state_dir=Path(args.state_dir),
                input_dir=Path(args.input_dir),
                data_id=args.data_id,
                page_range=args.page_range,
                language=args.language,
                is_ocr=args.is_ocr,
                enable_table=args.enable_table,
                enable_formula=args.enable_formula,
                api_base=args.api_base,
            )
        except MinerUError as exc:
            raise SystemExit(f"MinerU Agent submit failed: {exc}") from exc
        print("MinerU Agent task submitted.")
        print(f"task_id: {state['task_id']}")
        print(f"data_id: {state['data_id']}")
        print(f"uploaded_pdf: {state['uploaded_pdf_path']}")
        print(f"state_file: {Path(args.state_dir) / ('agent-' + str(state['task_id']) + '.json')}")
    elif args.command == "mineru-agent-status":
        try:
            result = get_agent_task_status(args.task_id, state_dir=Path(args.state_dir), api_base=args.api_base)
        except MinerUError as exc:
            raise SystemExit(f"MinerU Agent status failed: {exc}") from exc
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_mineru_agent_status(result)
    elif args.command == "mineru-agent-download":
        try:
            path = download_agent_result(
                args.task_id,
                state_dir=Path(args.state_dir),
                result_dir=Path(args.result_dir),
                api_base=args.api_base,
            )
        except MinerUError as exc:
            raise SystemExit(f"MinerU Agent download failed: {exc}") from exc
        print("Downloaded MinerU Agent markdown:")
        print(path)


def print_mineru_status(result: Dict[str, object]) -> None:
    data = result.get("data", {})
    if not isinstance(data, dict):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"batch_id: {data.get('batch_id', '')}")
    items = data.get("extract_result") or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        print("No file status returned yet.")
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        fields: List[str] = []
        for key in ("file_name", "data_id", "state", "err_msg"):
            if item.get(key):
                fields.append(f"{key}: {item[key]}")
        if item.get("full_zip_url"):
            fields.append("result_zip: ready")
        print(" | ".join(fields) if fields else json.dumps(item, ensure_ascii=False))


def wait_for_mineru_segments(
    manifest: Dict[str, object],
    *,
    config_path: Path,
    state_dir: Path,
    result_dir: Path,
    poll_seconds: int,
    timeout_minutes: int,
    download: bool,
) -> None:
    pending: Dict[str, Dict[str, object]] = {}
    completed = 0
    for segment in manifest.get("segments", []):
        if not isinstance(segment, dict):
            continue
        if segment.get("status") == "skipped_existing_result":
            completed += 1
            continue
        batch_id = segment.get("batch_id")
        if batch_id:
            pending[str(batch_id)] = segment
    deadline = time.time() + timeout_minutes * 60
    downloaded: Set[str] = set()
    while pending and time.time() < deadline:
        for batch_id, segment in list(pending.items()):
            result = get_batch_status(batch_id, config_path=config_path, state_dir=state_dir)
            item = first_extract_result(result)
            state = str(item.get("state") or "unknown")
            progress = item.get("extract_progress") or {}
            progress_text = ""
            if isinstance(progress, dict) and progress.get("total_pages"):
                progress_text = f" {progress.get('extracted_pages', 0)}/{progress.get('total_pages')}"
            print(f"{segment.get('page_ranges')}: {state}{progress_text}")
            if state == "done":
                completed += 1
                if download and batch_id not in downloaded:
                    paths = download_done_results(
                        batch_id,
                        config_path=config_path,
                        state_dir=state_dir,
                        result_dir=result_dir,
                    )
                    for path in paths:
                        print(f"downloaded: {path}")
                    downloaded.add(batch_id)
                pending.pop(batch_id, None)
            elif state == "failed":
                print(f"failed: {segment.get('page_ranges')} {item.get('err_msg') or ''}")
                pending.pop(batch_id, None)
        if pending:
            time.sleep(poll_seconds)
    if pending:
        print("Timed out before all MinerU segments finished.")
        for segment in pending.values():
            print(f"still pending: {segment.get('page_ranges')} batch_id={segment.get('batch_id')}")
    else:
        print(f"All MinerU segments finished. Completed segments: {completed}")


def first_extract_result(result: Dict[str, object]) -> Dict[str, object]:
    data = result.get("data", {})
    if not isinstance(data, dict):
        return {}
    items = data.get("extract_result") or []
    if isinstance(items, dict):
        return items
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def print_mineru_agent_status(result: Dict[str, object]) -> None:
    data = result.get("data", {})
    if not isinstance(data, dict):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    fields: List[str] = []
    for key in ("task_id", "state", "status", "err_msg", "msg"):
        if data.get(key):
            fields.append(f"{key}: {data[key]}")
    if data.get("markdown_url") or data.get("md_url") or data.get("download_url"):
        fields.append("markdown: ready")
    print(" | ".join(fields) if fields else json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
