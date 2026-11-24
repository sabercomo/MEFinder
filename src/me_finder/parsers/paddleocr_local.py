"""Offline PaddleOCR provider for PP-StructureV3 and local layout services."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from ..components.install import stop_process
from ..import_resume import atomic_write_json
from .openai_compatible import VISION_TEST_IMAGE_BASE64
from .provider import (
    NormalizedBlock, NormalizedPage, NormalizedParseResult, ParserCredential,
    ParserPollResult, ParserProvider, ParserProviderError, ParserRequest,
    ParserSubmission, ProviderCapabilities,
)

PADDLE_PROVIDER_ID = "paddleocr-local"
PADDLE_PROVIDER_NAME = "本地 PaddleOCR"
PADDLE_PROFILE = "PP-StructureV3"
PADDLE_OCR_VERSION = "3.7.0"
PADDLE_VERSION = "3.3.1"
PADDLE_RUNTIME_LOCK = threading.Lock()
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
_RESPONSE_LIMIT = 48 * 1024 * 1024
_PROBE_IMAGE = base64.b64decode(VISION_TEST_IMAGE_BASE64)

# Run in the component's interpreter; never import Paddle into MEFinder.
PADDLE_WORKER = r"""
import json
import sys
from pathlib import Path
from paddleocr import PPStructureV3

pipeline = PPStructureV3(
    device="cpu", cpu_threads=2, enable_mkldnn=False,
    use_doc_orientation_classify=False, use_doc_unwarping=False,
    use_textline_orientation=False, use_seal_recognition=False,
    use_chart_recognition=False, use_region_detection=False,
    text_detection_model_name="PP-OCRv5_mobile_det",
    text_recognition_model_name="PP-OCRv5_mobile_rec",
    formula_recognition_model_name="PP-FormulaNet_plus-S",
)
pages = []
for item in pipeline.predict_iter(sys.argv[1]):
    result = item.json["res"]
    pages.append({
        "page_index": result.get("page_index"),
        "parsing_res_list": [
            {key: block[key] for key in ("block_label", "block_content")}
            for block in result["parsing_res_list"]
        ],
    })
Path(sys.argv[2]).write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
"""


def paddle_runtime_root(root: Path) -> Path:
    """Keep the optional inference environment separate from the app."""
    override = os.environ.get("ME_FINDER_PADDLEOCR_HOME", "").strip()
    return Path(override) if override else root / "runtime" / PADDLE_PROVIDER_ID


def paddle_cpu_env(runtime: Path) -> dict[str, str]:
    """Scope the Paddle model cache to the installed component."""
    return {
        **os.environ,
        "PADDLE_PDX_CACHE_HOME": str(runtime / "paddle-cache"),
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "PADDLE_PDX_MODEL_SOURCE": "BOS",
        "PYTHONUTF8": "1",
    }


def paddle_service_url(value: str) -> str:
    """Accept loopback or private LAN IP endpoints, without embedded secrets."""
    parsed = urlsplit(value.strip())
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host) if host and host != "localhost" else None
        private = host == "localhost" or address is not None and (
            address.is_loopback or any(address in network for network in _PRIVATE_NETWORKS)
        )
        port = parsed.port
    except ValueError:
        private = False
        port = None
    if (not private or parsed.scheme not in {"http", "https"} or parsed.username is not None
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in {"", "/layout-parsing"}
            or port == 0):
        raise ValueError("请填写本机或内网 IP 的 PaddleOCR 服务地址，例如 http://127.0.0.1:8080。")
    return urlunsplit((parsed.scheme, parsed.netloc, "/layout-parsing", "", ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward an import request to an address outside its configuration."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


@dataclass(frozen=True)
class PaddleOCRConfig:
    endpoint: str = ""
    enabled: bool = False


def read_paddle_config(root: Path) -> PaddleOCRConfig:
    """Read machine-local settings without probing or starting the service."""
    path = paddle_runtime_root(root) / "settings.json"
    if not path.is_file():
        return PaddleOCRConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    return PaddleOCRConfig(str(data["endpoint"]), data["enabled"] is True)


def save_paddle_config(root: Path, endpoint: str, enabled: bool) -> None:
    """Persist only a validated endpoint and explicit opt-in."""
    if endpoint:
        endpoint = paddle_service_url(endpoint)
    atomic_write_json(paddle_runtime_root(root) / "settings.json", {
        "endpoint": endpoint, "enabled": enabled,
    })


def paddle_cpu_python(root: Path) -> Path:
    """Return the installed interpreter's platform-specific path."""
    location = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return paddle_runtime_root(root) / "cpu" / "venv" / location


def paddle_cpu_installed(root: Path) -> bool:
    """Expose only runtimes with a completed inference receipt."""
    return paddle_cpu_python(root).is_file() and (
        paddle_runtime_root(root) / "cpu" / "receipt.json"
    ).is_file()


def paddle_available(root: Path) -> bool:
    """Check whether explicit PaddleOCR imports are enabled."""
    settings = read_paddle_config(root)
    return settings.enabled and bool(settings.endpoint or paddle_cpu_installed(root))


def request_paddle_pages(endpoint: str, content: bytes, *, file_type: int, timeout: float) -> object:
    """Send a PDF slice (or a synthetic test image) to the official service."""
    request = urllib.request.Request(
        paddle_service_url(endpoint),
        data=json.dumps({
            "file": base64.b64encode(content).decode("ascii"),
            "fileType": file_type,
            "visualize": False,
            "returnMarkdownImages": False,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(_RESPONSE_LIMIT + 1)
        if len(data) > _RESPONSE_LIMIT:
            raise ValueError("response too large")
        payload = json.loads(data)
        if payload.get("errorCode") != 0:
            raise ValueError("invalid service result")
        return [item["prunedResult"] for item in payload["result"]["layoutParsingResults"]]
    except urllib.error.HTTPError as exc:
        raise ParserProviderError(
            f"PaddleOCR 服务返回 HTTP {exc.code}，请检查 /layout-parsing 地址。",
            provider_id=PADDLE_PROVIDER_ID,
        ) from exc
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        raise ParserProviderError(
            f"无法读取 PaddleOCR 版面解析结果：{exc}", provider_id=PADDLE_PROVIDER_ID,
        ) from exc


def test_paddle_endpoint(endpoint: str) -> None:
    """Use a synthetic image, never a user document, to validate the service."""
    pages = request_paddle_pages(endpoint, _PROBE_IMAGE, file_type=1, timeout=30)
    normalize_paddle_pages(pages, 1, 0)


def normalize_paddle_pages(raw: object, count: int, offset: int) -> tuple[NormalizedPage, ...]:
    """Require the full ordered slice and preserve physical page anchors."""
    if not isinstance(raw, list) or len(raw) != count:
        raise ParserProviderError("PaddleOCR 返回页数不符，未写入索引。", provider_id=PADDLE_PROVIDER_ID)
    # The official HTTP service removes page_index; its array retains input order.
    indexed = any(isinstance(page, dict) and page.get("page_index") is not None for page in raw)
    pages = []
    for index, page in enumerate(raw):
        if not isinstance(page, dict) or not isinstance(page.get("parsing_res_list"), list) or (
            indexed and (type(page.get("page_index")) is not int or page["page_index"] != index)
        ):
            raise ParserProviderError("PaddleOCR 返回的页序或版面结构无效。", provider_id=PADDLE_PROVIDER_ID)
        blocks = []
        for item in page["parsing_res_list"]:
            if not isinstance(item, dict) or not isinstance(item.get("block_content"), str):
                raise ParserProviderError("PaddleOCR 返回了无效的文本块。", provider_id=PADDLE_PROVIDER_ID)
            label = item.get("block_label")
            text = item["block_content"].strip()
            if not text or label in {"image", "chart", "seal"}:
                continue
            if label == "formula" and not text.startswith(("$", r"\[", r"\(")):
                text = "$$\n" + text + "\n$$"
            blocks.append(NormalizedBlock(text=text, block_type="text", reading_order=len(blocks)))
        pages.append(NormalizedPage(
            physical_pdf_page=offset + index + 1,
            text="\n\n".join(item.text for item in blocks),
            blocks=tuple(blocks),
        ))
    return tuple(pages)


class PaddleOCRProvider(ParserProvider):
    """Use PaddleOCR's own runtime or its native layout service, not chat."""

    provider_id = PADDLE_PROVIDER_ID

    def __init__(self, *, endpoint: str = "", root: Path | None = None,
                 cancel_requested: Callable[[], bool] | None = None) -> None:
        self.endpoint = endpoint
        self.root = root
        self.cancel_requested = cancel_requested

    def capabilities(self) -> ProviderCapabilities:
        # Serving processes at most ten PDF pages by default.
        return ProviderCapabilities(8, 32 * 1024 * 1024, supports_async_jobs=False)

    def submit(self, request: ParserRequest, *,
               credential: ParserCredential | None = None) -> ParserSubmission:
        request = self.prepare(request)
        if self.endpoint:
            result = request_paddle_pages(
                self.endpoint, request.source_path.read_bytes(), file_type=0, timeout=120,
            )
        else:
            assert self.root is not None and request.output_dir is not None
            request.output_dir.mkdir(parents=True, exist_ok=True)
            output = request.output_dir / "paddle-pages.json"
            output.unlink(missing_ok=True)
            with (request.output_dir / "paddle.log").open("wb") as log:
                process = subprocess.Popen(
                    [str(paddle_cpu_python(self.root)), "-c", PADDLE_WORKER, str(request.source_path), str(output)],
                    env=paddle_cpu_env(paddle_runtime_root(self.root) / "cpu"),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    for _ in range(1200):
                        if self.cancel_requested and self.cancel_requested():
                            raise ParserProviderError("本地 PaddleOCR 已取消。", provider_id=self.provider_id)
                        try:
                            status = process.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            continue
                        if status != 0:
                            raise ParserProviderError(
                                "本地 PaddleOCR 进程失败，请查看解析任务的 paddle.log。",
                                provider_id=self.provider_id,
                            )
                        break
                    else:
                        raise ParserProviderError("本地 PaddleOCR 解析超时。", provider_id=self.provider_id)
                finally:
                    stop_process(process)
            result = json.loads(output.read_text(encoding="utf-8"))
        return ParserSubmission(self.provider_id, None, "completed", raw_result=result)

    def poll(self, remote_task_id: str, *, credential: ParserCredential | None = None) -> ParserPollResult:
        raise NotImplementedError("PaddleOCR does not return asynchronous task IDs")

    def fetch_result(self, submission: ParserSubmission, request: ParserRequest, *,
                     credential: ParserCredential | None = None) -> object:
        return submission.raw_result

    def normalize_result(self, raw_result: object, request: ParserRequest) -> NormalizedParseResult:
        return NormalizedParseResult(
            self.provider_id, request.model,
            normalize_paddle_pages(raw_result, request.page_count, request.global_page_offset),
        )
