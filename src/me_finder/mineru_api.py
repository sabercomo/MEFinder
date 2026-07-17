"""Small local client for MinerU API workflows.

This module intentionally avoids third-party HTTP dependencies so it can run in
the same local Python environment as the rest of the MVP.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


DEFAULT_MINERU_CONFIG_PATH = Path("config/mineru_api.local.json")
DEFAULT_MINERU_STATE_DIR = Path("corpus/processed/mineru/tasks")
DEFAULT_MINERU_RESULT_DIR = Path("corpus/processed/mineru/results")
DEFAULT_MINERU_MANIFEST_DIR = Path("corpus/processed/mineru/manifests")
DEFAULT_MINERU_AGENT_INPUT_DIR = Path("corpus/processed/mineru/agent_inputs")
DEFAULT_MINERU_AGENT_RESULT_DIR = Path("corpus/processed/mineru/agent_results")
DEFAULT_MINERU_API_BASE = "https://mineru.net"
AGENT_MAX_BYTES = 10 * 1024 * 1024
AGENT_MAX_PAGES = 20
MINERU_CONFIG_FIELDS = ("token", "access_key_id", "secret_access_key", "api_base", "expires_at")


def resolve_mineru_config_path(root: Optional[Path] = None) -> Path:
    """Return the app-specific config path, honoring the desktop override."""

    override = os.environ.get("ME_FINDER_MINERU_CONFIG", "").strip()
    if override:
        return Path(override)
    if root is not None:
        return Path(root) / DEFAULT_MINERU_CONFIG_PATH
    return DEFAULT_MINERU_CONFIG_PATH


class MinerUError(RuntimeError):
    pass


@dataclass
class MinerUConfig:
    token: str
    api_base: str = DEFAULT_MINERU_API_BASE
    use_env_proxy: bool = False


def _usable_secret(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and not text.startswith("PASTE_")


def read_mineru_config_data(path: Path = DEFAULT_MINERU_CONFIG_PATH) -> Dict[str, object]:
    """Read the local MinerU config without exposing it to the web UI."""

    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise MinerUError("MinerU config must contain a JSON object.")
    return data


def mineru_config_summary(path: Path = DEFAULT_MINERU_CONFIG_PATH) -> Dict[str, object]:
    """Return only safe-to-display status information for the local config."""

    data = read_mineru_config_data(path)
    token = data.get("token") or data.get("api_token") or data.get("bearer_token")
    secret = data.get("secret_access_key")
    configured = _usable_secret(token) or _usable_secret(secret)
    expires_at = str(data.get("expires_at") or "").strip()
    expiry = _expiry_summary(expires_at)
    return {
        "configured": configured,
        "has_token": _usable_secret(token),
        "has_access_key_id": _usable_secret(data.get("access_key_id")),
        "has_secret_access_key": _usable_secret(secret),
        "api_base": str(data.get("api_base") or DEFAULT_MINERU_API_BASE).rstrip("/"),
        "expires_at": expires_at,
        **expiry,
    }


def _expiry_summary(expires_at: str, today: Optional[date] = None) -> Dict[str, object]:
    value = str(expires_at or "").strip()
    if not value:
        return {
            "expiry_status": "unset",
            "expires_days_remaining": None,
            "expiry_label": "未设置到期时间",
        }
    try:
        expiry_date = date.fromisoformat(value)
    except ValueError:
        return {
            "expiry_status": "invalid",
            "expires_days_remaining": None,
            "expiry_label": "到期时间格式无效",
        }
    current = today or date.today()
    days = (expiry_date - current).days
    if days > 0:
        label = f"{value}（剩余 {days} 天）"
        status = "valid"
    elif days == 0:
        label = f"{value}（今天到期）"
        status = "expires_today"
    else:
        label = f"{value}（已过期 {abs(days)} 天）"
        status = "expired"
    return {
        "expiry_status": status,
        "expires_days_remaining": days,
        "expiry_label": label,
    }


def save_mineru_config(updates: Dict[str, object], path: Path = DEFAULT_MINERU_CONFIG_PATH) -> Dict[str, object]:
    """Merge user-entered credentials into the local config atomically.

    Empty credential fields intentionally preserve existing values so a user can
    rotate only the expiring Bearer Token from the desktop settings page.
    """

    if not isinstance(updates, dict):
        raise MinerUError("MinerU config update must be a JSON object.")
    data = read_mineru_config_data(path)
    for field in ("token", "access_key_id", "secret_access_key"):
        if field in updates and str(updates.get(field) or "").strip():
            value = str(updates[field]).strip()
            if len(value) > 2048:
                raise MinerUError(f"{field} is too long.")
            data[field] = value

    api_base = str(updates.get("api_base") or "").strip().rstrip("/")
    if api_base:
        parsed = urlparse(api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MinerUError("API 地址必须是以 http:// 或 https:// 开头的网址。")
        data["api_base"] = api_base
    elif "api_base" not in data:
        data["api_base"] = DEFAULT_MINERU_API_BASE

    if "expires_at" in updates:
        expires_at = str(updates.get("expires_at") or "").strip()
        if expires_at:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires_at):
                raise MinerUError("到期日期请使用 YYYY-MM-DD 格式。")
        data["expires_at"] = expires_at

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return mineru_config_summary(path)


def load_mineru_config(path: Path = DEFAULT_MINERU_CONFIG_PATH) -> MinerUConfig:
    path = Path(path)
    if not path.exists():
        raise MinerUError(f"MinerU config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    token = (
        data.get("token")
        or data.get("api_token")
        or data.get("bearer_token")
        or data.get("secret_access_key")
        or ""
    )
    if not token or "PASTE_" in str(token):
        raise MinerUError(
            "MinerU token is empty. Fill config/mineru_api.local.json first. "
            "If the API page gives a separate Token, add it as a \"token\" field."
        )
    api_base = str(data.get("api_base") or DEFAULT_MINERU_API_BASE).rstrip("/")
    return MinerUConfig(token=str(token).strip(), api_base=api_base, use_env_proxy=bool(data.get("use_env_proxy")))


class MinerUClient:
    def __init__(self, config: MinerUConfig) -> None:
        self.config = config
        if config.use_env_proxy:
            self.opener = urllib.request.build_opener()
        else:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def apply_upload_urls(
        self,
        files: List[Dict[str, object]],
        model_version: str = "vlm",
        language: str = "ch",
        enable_table: bool = True,
        enable_formula: bool = True,
    ) -> Dict[str, object]:
        payload = {
            "files": files,
            "model_version": model_version,
            "language": language,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
        }
        return self._json_request("POST", "/api/v4/file-urls/batch", payload)

    def upload_file(self, upload_url: str, path: Path) -> int:
        data = Path(path).read_bytes()
        request = urllib.request.Request(upload_url, data=data, headers={"Content-Type": ""}, method="PUT")
        try:
            with self.opener.open(request, timeout=180) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            raise MinerUError(f"Upload failed: HTTP {exc.code}") from exc

    def batch_status(self, batch_id: str) -> Dict[str, object]:
        return self._json_request("GET", f"/api/v4/extract-results/batch/{batch_id}", None)

    def download_url(self, url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, method="GET")
        try:
            with self.opener.open(request, timeout=300) as response:
                output_path.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise MinerUError(f"Download failed: HTTP {exc.code}") from exc

    def _json_request(self, method: str, endpoint: str, payload: Optional[Dict[str, object]]) -> Dict[str, object]:
        url = self.config.api_base + endpoint
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.token}",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise MinerUError(f"MinerU HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise MinerUError(f"MinerU network error: {exc.reason}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MinerUError(f"MinerU returned non-JSON response: {raw[:200]}") from exc
        if data.get("code") != 0:
            raise MinerUError(f"MinerU error {data.get('code')}: {data.get('msg')}")
        return data


class MinerUAgentClient:
    def __init__(self, api_base: str = DEFAULT_MINERU_API_BASE, use_env_proxy: bool = False) -> None:
        self.api_base = api_base.rstrip("/")
        if use_env_proxy:
            self.opener = urllib.request.build_opener()
        else:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def create_file_task(
        self,
        *,
        file_name: str,
        language: str = "ch",
        page_range: Optional[str] = None,
        is_ocr: bool = True,
        enable_table: bool = True,
        enable_formula: bool = True,
    ) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "file_name": file_name,
            "language": language,
            "is_ocr": is_ocr,
            "enable_table": enable_table,
            "enable_formula": enable_formula,
        }
        if page_range:
            payload["page_range"] = page_range
        return self._json_request("POST", "/api/v1/agent/parse/file", payload)

    def upload_file(self, upload_url: str, path: Path) -> int:
        data = Path(path).read_bytes()
        request = urllib.request.Request(upload_url, data=data, headers={"Content-Type": ""}, method="PUT")
        try:
            with self.opener.open(request, timeout=180) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise MinerUError(f"Agent upload failed: HTTP {exc.code}: {detail}") from exc

    def task_status(self, task_id: str) -> Dict[str, object]:
        return self._json_request("GET", f"/api/v1/agent/parse/{task_id}", None)

    def download_url(self, url: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, method="GET")
        try:
            with self.opener.open(request, timeout=300) as response:
                output_path.write_bytes(response.read())
        except urllib.error.HTTPError as exc:
            raise MinerUError(f"Agent download failed: HTTP {exc.code}") from exc

    def _json_request(self, method: str, endpoint: str, payload: Optional[Dict[str, object]]) -> Dict[str, object]:
        url = self.api_base + endpoint
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise MinerUError(f"MinerU Agent HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise MinerUError(f"MinerU Agent network error: {exc.reason}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MinerUError(f"MinerU Agent returned non-JSON response: {raw[:200]}") from exc
        if data.get("code") != 0:
            raise MinerUError(f"MinerU Agent error {data.get('code')}: {data.get('msg')}")
        return data


def submit_local_pdf(
    pdf_path: Path,
    *,
    config_path: Path = DEFAULT_MINERU_CONFIG_PATH,
    state_dir: Path = DEFAULT_MINERU_STATE_DIR,
    data_id: Optional[str] = None,
    page_ranges: Optional[str] = None,
    model_version: str = "vlm",
    language: str = "ch",
    is_ocr: bool = True,
    enable_table: bool = True,
    enable_formula: bool = True,
) -> Dict[str, object]:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise MinerUError(f"PDF not found: {pdf_path}")
    config = load_mineru_config(config_path)
    client = MinerUClient(config)
    file_spec: Dict[str, object] = {
        "name": pdf_path.name,
        "data_id": data_id or safe_data_id(pdf_path.stem),
        "is_ocr": is_ocr,
    }
    if page_ranges:
        file_spec["page_ranges"] = page_ranges
    response = client.apply_upload_urls(
        [file_spec],
        model_version=model_version,
        language=language,
        enable_table=enable_table,
        enable_formula=enable_formula,
    )
    data = response["data"]
    batch_id = str(data["batch_id"])
    urls = data.get("file_urls") or []
    if not urls:
        raise MinerUError("MinerU did not return an upload URL.")
    upload_url = extract_upload_url(urls[0])
    upload_status = client.upload_file(upload_url, pdf_path)
    state = {
        "batch_id": batch_id,
        "pdf_path": str(pdf_path),
        "file_name": pdf_path.name,
        "data_id": file_spec["data_id"],
        "page_ranges": page_ranges,
        "model_version": model_version,
        "language": language,
        "is_ocr": is_ocr,
        "upload_status": upload_status,
        "submitted_at": int(time.time()),
    }
    save_state(batch_id, state, Path(state_dir))
    return state


def submit_local_pdf_segments(
    pdf_path: Path,
    *,
    config_path: Path = DEFAULT_MINERU_CONFIG_PATH,
    state_dir: Path = DEFAULT_MINERU_STATE_DIR,
    manifest_dir: Path = DEFAULT_MINERU_MANIFEST_DIR,
    data_id_prefix: Optional[str] = None,
    segment_size: int = 200,
    start_page: int = 1,
    end_page: Optional[int] = None,
    model_version: str = "vlm",
    language: str = "ch",
    is_ocr: bool = True,
    enable_table: bool = True,
    enable_formula: bool = True,
) -> Dict[str, object]:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise MinerUError(f"PDF not found: {pdf_path}")
    if segment_size < 1 or segment_size > 200:
        raise MinerUError("segment_size must be between 1 and 200 for MinerU precision API.")
    total_pages = get_pdf_page_count(pdf_path)
    end_page = int(end_page or total_pages)
    if start_page < 1 or end_page < start_page or end_page > total_pages:
        raise MinerUError(f"Invalid page span {start_page}-{end_page}; PDF has {total_pages} pages.")
    prefix = safe_data_id(data_id_prefix or pdf_path.stem)
    width = max(3, len(str(total_pages)))
    manifest: Dict[str, object] = {
        "api": "precision",
        "pdf_path": str(pdf_path),
        "file_name": pdf_path.name,
        "data_id_prefix": prefix,
        "total_pages": total_pages,
        "segment_size": segment_size,
        "requested_page_start": start_page,
        "requested_page_end": end_page,
        "model_version": model_version,
        "language": language,
        "is_ocr": is_ocr,
        "enable_table": enable_table,
        "enable_formula": enable_formula,
        "submitted_at": int(time.time()),
        "segments": [],
    }
    for segment_start, segment_end in build_page_segments(start_page, end_page, segment_size):
        data_id = f"{prefix}-p{segment_start:0{width}d}-{segment_end:0{width}d}"
        result_dir = Path(DEFAULT_MINERU_RESULT_DIR) / data_id
        if result_dir.exists():
            segment_state = {
                "data_id": data_id,
                "page_ranges": f"{segment_start}-{segment_end}",
                "status": "skipped_existing_result",
                "result_dir": str(result_dir),
            }
        else:
            state = submit_local_pdf(
                pdf_path,
                config_path=config_path,
                state_dir=state_dir,
                data_id=data_id,
                page_ranges=f"{segment_start}-{segment_end}",
                model_version=model_version,
                language=language,
                is_ocr=is_ocr,
                enable_table=enable_table,
                enable_formula=enable_formula,
            )
            segment_state = {
                "data_id": data_id,
                "page_ranges": f"{segment_start}-{segment_end}",
                "status": "submitted",
                "batch_id": state["batch_id"],
                "state_file": str(Path(state_dir) / f"{state['batch_id']}.json"),
            }
        cast_segments = manifest["segments"]
        if isinstance(cast_segments, list):
            cast_segments.append(segment_state)
    manifest_path = save_segment_manifest(prefix, manifest, Path(manifest_dir))
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def get_batch_status(
    batch_id: str,
    *,
    config_path: Path = DEFAULT_MINERU_CONFIG_PATH,
    state_dir: Path = DEFAULT_MINERU_STATE_DIR,
) -> Dict[str, object]:
    client = MinerUClient(load_mineru_config(config_path))
    result = client.batch_status(batch_id)
    state = load_state(batch_id, Path(state_dir))
    state["last_status"] = result.get("data", {})
    state["checked_at"] = int(time.time())
    save_state(batch_id, state, Path(state_dir))
    return result


def download_done_results(
    batch_id: str,
    *,
    config_path: Path = DEFAULT_MINERU_CONFIG_PATH,
    state_dir: Path = DEFAULT_MINERU_STATE_DIR,
    result_dir: Path = DEFAULT_MINERU_RESULT_DIR,
) -> List[Path]:
    client = MinerUClient(load_mineru_config(config_path))
    result = client.batch_status(batch_id)
    data = result.get("data", {})
    extract_results = data.get("extract_result") or []
    if isinstance(extract_results, dict):
        extract_results = [extract_results]
    downloaded: List[Path] = []
    for item in extract_results:
        if item.get("state") != "done" or not item.get("full_zip_url"):
            continue
        label = safe_data_id(str(item.get("data_id") or item.get("file_name") or batch_id))
        out_dir = Path(result_dir) / label
        zip_path = out_dir / "mineru_result.zip"
        client.download_url(str(item["full_zip_url"]), zip_path)
        extract_zip(zip_path, out_dir)
        downloaded.append(out_dir)
    if not downloaded:
        raise MinerUError("No completed MinerU result is available for download yet.")
    state = load_state(batch_id, Path(state_dir))
    state["downloaded_result_dirs"] = [str(p) for p in downloaded]
    save_state(batch_id, state, Path(state_dir))
    return downloaded


def submit_agent_pdf(
    pdf_path: Path,
    *,
    state_dir: Path = DEFAULT_MINERU_STATE_DIR,
    input_dir: Path = DEFAULT_MINERU_AGENT_INPUT_DIR,
    data_id: Optional[str] = None,
    page_range: Optional[str] = None,
    language: str = "ch",
    is_ocr: bool = True,
    enable_table: bool = True,
    enable_formula: bool = True,
    api_base: str = DEFAULT_MINERU_API_BASE,
) -> Dict[str, object]:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise MinerUError(f"PDF not found: {pdf_path}")
    data_id = data_id or safe_data_id(f"{pdf_path.stem}-{page_range or 'all'}")
    upload_path, prepared = prepare_agent_pdf_input(pdf_path, Path(input_dir), data_id, page_range)
    client = MinerUAgentClient(api_base=api_base)
    api_page_range = None if prepared["split_pdf"] else page_range
    response = client.create_file_task(
        file_name=upload_path.name,
        language=language,
        page_range=api_page_range,
        is_ocr=is_ocr,
        enable_table=enable_table,
        enable_formula=enable_formula,
    )
    task_data = response.get("data") or {}
    if not isinstance(task_data, dict):
        raise MinerUError(f"Unexpected MinerU Agent task response: {response!r}")
    task_id = str(task_data.get("task_id") or task_data.get("id") or "")
    if not task_id:
        raise MinerUError(f"MinerU Agent did not return a task_id: {response!r}")
    upload_url = extract_upload_url(task_data)
    upload_status = client.upload_file(upload_url, upload_path)
    state = {
        "api": "agent",
        "task_id": task_id,
        "source_pdf_path": str(pdf_path),
        "uploaded_pdf_path": str(upload_path),
        "uploaded_file_name": upload_path.name,
        "data_id": data_id,
        "page_range": page_range,
        "api_page_range": api_page_range,
        "prepared_input": prepared,
        "language": language,
        "is_ocr": is_ocr,
        "enable_table": enable_table,
        "enable_formula": enable_formula,
        "upload_status": upload_status,
        "submitted_at": int(time.time()),
    }
    save_state(agent_state_key(task_id), state, Path(state_dir))
    return state


def get_agent_task_status(
    task_id: str,
    *,
    state_dir: Path = DEFAULT_MINERU_STATE_DIR,
    api_base: str = DEFAULT_MINERU_API_BASE,
) -> Dict[str, object]:
    client = MinerUAgentClient(api_base=api_base)
    result = client.task_status(task_id)
    state = load_state(agent_state_key(task_id), Path(state_dir))
    state["last_status"] = result.get("data", {})
    state["checked_at"] = int(time.time())
    save_state(agent_state_key(task_id), state, Path(state_dir))
    return result


def download_agent_result(
    task_id: str,
    *,
    state_dir: Path = DEFAULT_MINERU_STATE_DIR,
    result_dir: Path = DEFAULT_MINERU_AGENT_RESULT_DIR,
    api_base: str = DEFAULT_MINERU_API_BASE,
) -> Path:
    client = MinerUAgentClient(api_base=api_base)
    result = client.task_status(task_id)
    data = result.get("data") or {}
    if not isinstance(data, dict):
        raise MinerUError(f"Unexpected MinerU Agent status response: {result!r}")
    state_value = str(data.get("state") or data.get("status") or "").lower()
    markdown_url = find_agent_markdown_url(data)
    if not markdown_url:
        raise MinerUError(f"No completed markdown result is available yet. Current state: {state_value or 'unknown'}")
    state = load_state(agent_state_key(task_id), Path(state_dir))
    label = safe_data_id(str(state.get("data_id") or data.get("task_id") or task_id))
    out_dir = Path(result_dir) / label
    out_path = out_dir / "mineru_agent.md"
    client.download_url(markdown_url, out_path)
    (out_dir / "agent_status.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    state["downloaded_markdown"] = str(out_path)
    state["last_status"] = data
    state["downloaded_at"] = int(time.time())
    save_state(agent_state_key(task_id), state, Path(state_dir))
    return out_path


def build_page_segments(start_page: int, end_page: int, segment_size: int = 200) -> List[Tuple[int, int]]:
    segments: List[Tuple[int, int]] = []
    current = start_page
    while current <= end_page:
        segment_end = min(current + segment_size - 1, end_page)
        segments.append((current, segment_end))
        current = segment_end + 1
    return segments


def prepare_agent_pdf_input(
    pdf_path: Path,
    input_dir: Path,
    data_id: str,
    page_range: Optional[str],
) -> tuple[Path, Dict[str, object]]:
    page_span = parse_simple_page_range(page_range) if page_range else None
    if page_span:
        return split_pdf_for_agent(pdf_path, input_dir, data_id, page_span)
    page_count = get_pdf_page_count(pdf_path)
    size_bytes = pdf_path.stat().st_size
    if page_count > AGENT_MAX_PAGES:
        raise MinerUError(
            f"Agent API accepts at most {AGENT_MAX_PAGES} pages. "
            "Pass --page-range, for example --page-range 1-20."
        )
    if size_bytes > AGENT_MAX_BYTES:
        raise MinerUError(
            f"Agent API accepts files up to 10MB; this file is {size_bytes} bytes. "
            "Pass --page-range so the tool can create a smaller local PDF first."
        )
    return pdf_path, {
        "split_pdf": False,
        "original_page_start": 1,
        "original_page_end": page_count,
        "page_count": page_count,
        "size_bytes": size_bytes,
    }


def split_pdf_for_agent(
    pdf_path: Path,
    input_dir: Path,
    data_id: str,
    page_span: tuple[int, int],
) -> tuple[Path, Dict[str, object]]:
    start, end = page_span
    page_count = end - start + 1
    if page_count > AGENT_MAX_PAGES:
        raise MinerUError(f"Agent API accepts at most {AGENT_MAX_PAGES} pages; requested {page_count}.")
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise MinerUError("PyMuPDF is required to split a large PDF for the Agent API.") from exc
    input_dir.mkdir(parents=True, exist_ok=True)
    output_path = input_dir / f"{safe_data_id(data_id)}-pages-{start:04d}-{end:04d}.pdf"
    if output_path.exists():
        output_path = input_dir / f"{safe_data_id(data_id)}-pages-{start:04d}-{end:04d}-{int(time.time())}.pdf"
    src = fitz.open(str(pdf_path))
    dst = fitz.open()
    try:
        if end > len(src):
            raise MinerUError(f"Requested page range {start}-{end}, but the PDF has only {len(src)} pages.")
        dst.insert_pdf(src, from_page=start - 1, to_page=end - 1)
        dst.save(str(output_path), garbage=4, deflate=True)
    finally:
        dst.close()
        src.close()
    size_bytes = output_path.stat().st_size
    if size_bytes > AGENT_MAX_BYTES:
        raise MinerUError(
            f"The split PDF is still over 10MB ({size_bytes} bytes). "
            "Try a smaller --page-range, for example 1-10."
        )
    return output_path, {
        "split_pdf": True,
        "original_page_start": start,
        "original_page_end": end,
        "page_count": page_count,
        "size_bytes": size_bytes,
    }


def get_pdf_page_count(pdf_path: Path) -> int:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise MinerUError("PyMuPDF is required to check PDF page count for the Agent API.") from exc
    doc = fitz.open(str(pdf_path))
    try:
        return len(doc)
    finally:
        doc.close()


def parse_simple_page_range(value: Optional[str]) -> tuple[int, int]:
    if not value:
        raise MinerUError("Empty page range.")
    text = value.strip()
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
    elif re.fullmatch(r"\d+", text):
        start = end = int(text)
    else:
        raise MinerUError("Agent page range must be a simple range like 1-20.")
    if start < 1 or end < start:
        raise MinerUError(f"Invalid page range: {value}")
    return start, end


def find_agent_markdown_url(data: Dict[str, object]) -> Optional[str]:
    for key in ("markdown_url", "md_url", "result_url", "download_url", "full_md_url"):
        value = data.get(key)
        if value:
            return str(value)
    result = data.get("result")
    if isinstance(result, dict):
        return find_agent_markdown_url(result)
    return None


def agent_state_key(task_id: str) -> str:
    return f"agent-{safe_data_id(task_id)}"


def save_segment_manifest(prefix: str, manifest: Dict[str, object], manifest_dir: Path) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"segments-{safe_data_id(prefix)}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_state(batch_id: str, state: Dict[str, object], state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{batch_id}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(batch_id: str, state_dir: Path) -> Dict[str, object]:
    path = state_dir / f"{batch_id}.json"
    if not path.exists():
        return {"batch_id": batch_id}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def extract_zip(zip_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if output_dir.resolve() not in target.parents and target != output_dir.resolve():
                raise MinerUError(f"Unsafe zip member: {member.filename}")
            archive.extract(member, output_dir)


def extract_upload_url(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("file_url", "upload_url", "url"):
            if value.get(key):
                return str(value[key])
    raise MinerUError(f"Unexpected MinerU upload URL format: {value!r}")


def safe_data_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return value[:120] or f"mineru-{int(time.time())}"
