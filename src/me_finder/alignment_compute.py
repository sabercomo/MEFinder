"""Out-of-process seam for the alignment *compute* phase.

``generate_alignment`` has three phases: the main process **prepares** inputs
(DB reads → :class:`AlignmentPreparation`), a **compute** phase turns those
texts into semantic links (embeddings + monotonic alignment — the only step
that needs NumPy / ONNX Runtime / fastembed), and the main process **publishes**
the result back into the official database inside its write coordination.

This module owns the compute seam only. It never touches the database, never
re-parses source files, and imports NumPy nowhere: the objects that cross the
seam (:class:`SemanticLink`, :class:`HeadingAnchor`, :class:`FolioBoundaryCandidate`,
:class:`AlignmentThresholds`) are plain dataclasses of ints/floats/strings, so
the main process can build a request and consume a result without the compute
stack installed.

Two runners share one call signature (the same one as
``align_segment_sequences``), so ``generate_alignment`` can call either
transparently:

* the default in-process callable (:func:`run_in_process`) — unchanged
  behaviour, used as the parity baseline; and
* :class:`SubprocessAlignmentComputeRunner`, which runs the compute in a
  separate process that owns the NumPy/ONNX/fastembed stack and returns a
  result the NumPy-free main process deserializes.

The protocol is intentionally minimal (a versioned request file + a versioned
result file + line-delimited control messages on stdout, diagnostics on
stderr). It is not a general IPC framework.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Sequence, Tuple

from .alignment_anchors import HeadingAnchor
from .edition_folio_anchors import FolioBoundaryCandidate
from .embedding_models import AlignmentThresholds, DEFAULT_EMBEDDING_MODEL_ID
from .semantic_alignment import (
    ALIGNMENT_REGION_VERSION,
    EMBEDDING_RUNTIME_VERSION,
    SEMANTIC_ALIGNMENT_VERSION,
    SemanticLink,
)
from .text_alignment import ALIGNMENT_ALGORITHM_VERSION


# Bump only on an incompatible wire change. A mismatch is a hard, explicit error
# — the runner never guesses across versions.
ALIGNMENT_COMPUTE_PROTOCOL = 1

# Test-only fault injection, honoured by the worker when this env var is set to
# one of the codes below. Never set in production paths.
SIMULATE_ENV = "MEFINDER_ALIGNMENT_COMPUTE_SIMULATE"


class AlignmentComputeError(RuntimeError):
    """A compute request failed. ``code`` names the failure class."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Failure codes (stable strings; surfaced to the coordinator).
COMPONENT_MISSING = "component_missing"
PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
WORKER_START_FAILED = "worker_start_failed"
WORKER_CRASHED = "worker_crashed"
COMPUTE_FAILED = "compute_failed"
CANCELLED = "cancelled"
RESULT_MISMATCH = "result_mismatch"


ComputeResult = Tuple[List[SemanticLink], List[HeadingAnchor]]


# --------------------------------------------------------------------------- #
# Serialization (pure Python, no NumPy).
# --------------------------------------------------------------------------- #
def _serialize_folio(candidate: FolioBoundaryCandidate) -> dict:
    data = asdict(candidate)
    # asdict turns the bbox tuple into a list; keep it explicit for clarity.
    data["target_bbox"] = list(candidate.target_bbox)
    return data


def _deserialize_folio(data: Mapping[str, object]) -> FolioBoundaryCandidate:
    bbox = tuple(float(v) for v in data["target_bbox"])  # type: ignore[arg-type]
    similarity = data.get("similarity")
    return FolioBoundaryCandidate(
        folio_number=int(data["folio_number"]),  # type: ignore[arg-type]
        pivot_segment_index=int(data["pivot_segment_index"]),  # type: ignore[arg-type]
        target_segment_index=int(data["target_segment_index"]),  # type: ignore[arg-type]
        target_pdf_page_index=int(data["target_pdf_page_index"]),  # type: ignore[arg-type]
        target_bbox=bbox,  # type: ignore[arg-type]
        similarity=None if similarity is None else float(similarity),  # type: ignore[arg-type]
    )


def _serialize_link(link: SemanticLink) -> dict:
    return asdict(link)


def _deserialize_link(data: Mapping[str, object]) -> SemanticLink:
    return SemanticLink(
        source_start=int(data["source_start"]),  # type: ignore[arg-type]
        source_end=int(data["source_end"]),  # type: ignore[arg-type]
        target_start=int(data["target_start"]),  # type: ignore[arg-type]
        target_end=int(data["target_end"]),  # type: ignore[arg-type]
        cost=float(data["cost"]),  # type: ignore[arg-type]
        confidence=float(data["confidence"]),  # type: ignore[arg-type]
        review_status=str(data["review_status"]),
        anchor_key=str(data.get("anchor_key", "")),
    )


def _deserialize_anchor(data: Mapping[str, object]) -> HeadingAnchor:
    return HeadingAnchor(
        source_index=int(data["source_index"]),  # type: ignore[arg-type]
        target_index=int(data["target_index"]),  # type: ignore[arg-type]
        key=str(data["key"]),
    )


def model_identity(embedding_model_id: str) -> Dict[str, str]:
    """Versions that must match between the request and the worker's runtime."""

    return {
        "embedding_model_id": embedding_model_id,
        "embedding_runtime_version": str(EMBEDDING_RUNTIME_VERSION),
        "semantic_alignment_version": str(SEMANTIC_ALIGNMENT_VERSION),
        "alignment_algorithm_version": str(ALIGNMENT_ALGORITHM_VERSION),
        "alignment_region_version": str(ALIGNMENT_REGION_VERSION),
    }


def _serialize_inputs(
    *,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    thresholds: AlignmentThresholds,
    reusable_sequences: Sequence[Sequence[str]],
    folio_candidates: Sequence[FolioBoundaryCandidate],
    source_language: str,
    target_language: str,
    reviewed_body_ranges: Dict[str, List[int]] | None,
) -> dict:
    return {
        "source_texts": list(source_texts),
        "target_texts": list(target_texts),
        "thresholds": asdict(thresholds),
        "reusable_sequences": [list(seq) for seq in reusable_sequences],
        "folio_candidates": [_serialize_folio(c) for c in folio_candidates],
        "source_language": source_language,
        "target_language": target_language,
        "reviewed_body_ranges": reviewed_body_ranges,
    }


def input_identity(inputs: Mapping[str, object], identity: Mapping[str, str]) -> str:
    """Stable content hash of the compute inputs and model/algorithm versions.

    Both sides compute it; the runner refuses a result whose identity does not
    match the request it sent, so a stale or mismatched result is never
    consumed.
    """

    payload = json.dumps(
        {"inputs": inputs, "model_identity": identity},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_request(
    *,
    task_id: str,
    cache_dir: Path,
    embedding_model_id: str,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    thresholds: AlignmentThresholds,
    reusable_sequences: Sequence[Sequence[str]],
    folio_candidates: Sequence[FolioBoundaryCandidate],
    source_language: str,
    target_language: str,
    reviewed_body_ranges: Dict[str, List[int]] | None,
) -> dict:
    identity = model_identity(embedding_model_id)
    inputs = _serialize_inputs(
        source_texts=source_texts,
        target_texts=target_texts,
        thresholds=thresholds,
        reusable_sequences=reusable_sequences,
        folio_candidates=folio_candidates,
        source_language=source_language,
        target_language=target_language,
        reviewed_body_ranges=reviewed_body_ranges,
    )
    return {
        "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
        "task_id": task_id,
        "cache_dir": str(cache_dir),
        "model_identity": identity,
        "input_identity": input_identity(inputs, identity),
        "inputs": inputs,
    }


def deserialize_result(payload: Mapping[str, object]) -> ComputeResult:
    links = [_deserialize_link(row) for row in payload["links"]]  # type: ignore[arg-type]
    anchors = [_deserialize_anchor(row) for row in payload["anchors"]]  # type: ignore[arg-type]
    return links, anchors


def serialize_result(
    *, task_id: str, identity: str, computed: ComputeResult
) -> dict:
    links, anchors = computed
    return {
        "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
        "task_id": task_id,
        "input_identity": identity,
        "links": [_serialize_link(link) for link in links],
        "anchors": [asdict(anchor) for anchor in anchors],
    }


# --------------------------------------------------------------------------- #
# In-process runner (default; parity baseline).
# --------------------------------------------------------------------------- #
def run_in_process(
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    *,
    cache_dir: Path,
    embedding_provider=None,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    thresholds: AlignmentThresholds | None = None,
    reusable_sequences: Sequence[Sequence[str]] = (),
    folio_candidates: Sequence[FolioBoundaryCandidate] = (),
    source_language: str = "und",
    target_language: str = "und",
    reviewed_body_ranges: Dict[str, List[int]] | None = None,
) -> ComputeResult:
    """Run the compute phase in this process (imports NumPy lazily)."""

    from .text_alignment import align_segment_sequences

    return align_segment_sequences(
        source_texts,
        target_texts,
        cache_dir=cache_dir,
        embedding_provider=embedding_provider,
        embedding_model_id=embedding_model_id,
        thresholds=thresholds,
        reusable_sequences=reusable_sequences,
        folio_candidates=folio_candidates,
        source_language=source_language,
        target_language=target_language,
        reviewed_body_ranges=reviewed_body_ranges,
    )


# --------------------------------------------------------------------------- #
# Subprocess launch command.
# --------------------------------------------------------------------------- #
def default_worker_command() -> List[str]:
    """How to launch the compute worker for the current runtime.

    * Frozen app: the bundled executable dispatches the ``alignment-compute-worker``
      subcommand (see ``desktop.py``), so the worker reuses the app bundle's
      compute stack without a separate binary.
    * Development: run the worker module with the current interpreter.
    """

    if getattr(sys, "frozen", False):
        return [sys.executable, "alignment-compute-worker"]
    return [sys.executable, "-m", "src.me_finder.alignment_compute_worker"]


def _repo_root() -> Path:
    # src/me_finder/alignment_compute.py -> repo root is parents[2].
    return Path(__file__).resolve().parents[2]


class SubprocessAlignmentComputeRunner:
    """Run the compute phase in a separate process.

    The instance is callable with the same signature as
    ``align_segment_sequences`` so ``generate_alignment`` can use it as
    ``compute_runner``. It never touches the database; the main process keeps
    identity checks, write coordination and publication.
    """

    def __init__(
        self,
        *,
        task_id: str,
        launch_command: Sequence[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        poll_interval: float = 0.1,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self._task_id = task_id
        self._launch_command = list(launch_command) if launch_command else default_worker_command()
        self._cancel_check = cancel_check
        self._poll_interval = poll_interval
        self._env = dict(env) if env is not None else None
        self._cwd = cwd

    # -- process plumbing --------------------------------------------------- #
    def _spawn(self, extra_args: Sequence[str]) -> subprocess.Popen:
        env = dict(os.environ if self._env is None else self._env)
        cwd = self._cwd
        if not getattr(sys, "frozen", False) and self._cwd is None:
            # Dev: run from the repo root so ``src.me_finder`` is importable and
            # ensure it is on PYTHONPATH for the child.
            root = _repo_root()
            cwd = root
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(root), existing) if p)
        try:
            return subprocess.Popen(
                [*self._launch_command, *extra_args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                start_new_session=True,  # own process group → clean group kill
            )
        except OSError as exc:  # pragma: no cover - launch failure is rare
            raise AlignmentComputeError(
                WORKER_START_FAILED, f"无法启动对齐计算进程：{exc}"
            ) from exc

    @staticmethod
    def _close_streams(process: subprocess.Popen) -> None:
        for stream in (process.stdout, process.stderr, process.stdin):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    @classmethod
    def _kill(cls, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            cls._close_streams(process)
            return
        try:
            os.killpg(process.pid, __import__("signal").SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except OSError:
                cls._close_streams(process)
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, __import__("signal").SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
        finally:
            cls._close_streams(process)

    def _read_control_lines(self, process: subprocess.Popen):
        """Yield parsed stdout control messages; skip non-JSON diagnostics.

        Cancellation is polled while waiting; a cancel kills the process group
        and raises. The reader runs on a thread so the poll loop stays live.
        """

        line_queue: "queue.Queue[str | None]" = queue.Queue()

        def pump() -> None:
            try:
                assert process.stdout is not None
                for raw in process.stdout:
                    line_queue.put(raw)
            finally:
                line_queue.put(None)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        while True:
            if self._cancel_check is not None and self._cancel_check():
                self._kill(process)
                raise AlignmentComputeError(CANCELLED, "对齐计算已取消。")
            try:
                raw = line_queue.get(timeout=self._poll_interval)
            except queue.Empty:
                if process.poll() is not None:
                    # Process ended; drain whatever the reader captured.
                    try:
                        raw = line_queue.get(timeout=1.0)
                    except queue.Empty:
                        return
                else:
                    continue
            if raw is None:
                return
            text = raw.strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                # Stray stdout output is treated as a diagnostic, not control.
                continue

    def _stderr_tail(self, process: subprocess.Popen, limit: int = 2000) -> str:
        try:
            assert process.stderr is not None
            data = process.stderr.read() or ""
        except (OSError, ValueError):
            return ""
        return data[-limit:]

    # -- public API --------------------------------------------------------- #
    def probe(self) -> Dict[str, object]:
        """Ask the external worker to report its capabilities.

        This replaces a main-process ``find_spec`` check: the answer comes from
        the process that would actually run the compute. Raises
        :class:`AlignmentComputeError` when the worker cannot run or speaks an
        incompatible protocol.
        """

        process = self._spawn(["--probe"])
        try:
            for message in self._read_control_lines(process):
                kind = message.get("type")
                if kind == "hello":
                    protocol = message.get("protocol")
                    if protocol != ALIGNMENT_COMPUTE_PROTOCOL:
                        raise AlignmentComputeError(
                            PROTOCOL_INCOMPATIBLE,
                            f"对齐计算进程协议不兼容（worker={protocol!r}, "
                            f"expected={ALIGNMENT_COMPUTE_PROTOCOL}）。",
                        )
                    caps = dict(message.get("capabilities") or {})
                    missing = [
                        name
                        for name in ("numpy", "fastembed", "onnxruntime")
                        if not caps.get(name)
                    ]
                    if missing:
                        raise AlignmentComputeError(
                            COMPONENT_MISSING,
                            "对齐计算运行时缺少依赖：" + "、".join(missing),
                        )
                    return caps
                if kind == "error":
                    raise AlignmentComputeError(
                        str(message.get("code") or COMPONENT_MISSING),
                        str(message.get("message") or "对齐计算进程无法启动。"),
                    )
            raise AlignmentComputeError(
                WORKER_CRASHED,
                "对齐计算进程未返回能力应答。" + self._stderr_tail(process),
            )
        finally:
            self._kill(process)

    def __call__(
        self,
        source_texts: Sequence[str],
        target_texts: Sequence[str],
        *,
        cache_dir: Path,
        embedding_provider=None,
        embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
        thresholds: AlignmentThresholds | None = None,
        reusable_sequences: Sequence[Sequence[str]] = (),
        folio_candidates: Sequence[FolioBoundaryCandidate] = (),
        source_language: str = "und",
        target_language: str = "und",
        reviewed_body_ranges: Dict[str, List[int]] | None = None,
    ) -> ComputeResult:
        if embedding_provider is not None:
            # A callable provider cannot cross a process boundary. Production
            # never passes one; tests that inject a provider use the in-process
            # path. Fail loudly rather than silently running in-process.
            raise AlignmentComputeError(
                COMPUTE_FAILED,
                "子进程计算不支持自定义 embedding_provider。",
            )
        if thresholds is None:
            from .embedding_models import embedding_model_config

            thresholds = embedding_model_config(embedding_model_id).thresholds
        request = build_request(
            task_id=self._task_id,
            cache_dir=cache_dir,
            embedding_model_id=embedding_model_id,
            source_texts=source_texts,
            target_texts=target_texts,
            thresholds=thresholds,
            reusable_sequences=reusable_sequences,
            folio_candidates=folio_candidates,
            source_language=source_language,
            target_language=target_language,
            reviewed_body_ranges=reviewed_body_ranges,
        )
        work_dir = Path(tempfile.mkdtemp(prefix="mefinder-align-compute-"))
        request_path = work_dir / "request.json"
        result_path = work_dir / "result.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        process = self._spawn([str(request_path), str(result_path)])
        try:
            saw_hello = False
            for message in self._read_control_lines(process):
                kind = message.get("type")
                if kind == "hello":
                    saw_hello = True
                    if message.get("protocol") != ALIGNMENT_COMPUTE_PROTOCOL:
                        raise AlignmentComputeError(
                            PROTOCOL_INCOMPATIBLE,
                            "对齐计算进程协议不兼容。",
                        )
                elif kind == "progress":
                    continue
                elif kind == "error":
                    raise AlignmentComputeError(
                        str(message.get("code") or COMPUTE_FAILED),
                        str(message.get("message") or "对齐计算失败。"),
                    )
                elif kind == "result":
                    if message.get("input_identity") != request["input_identity"]:
                        raise AlignmentComputeError(
                            RESULT_MISMATCH,
                            "对齐计算结果与请求输入不匹配，拒绝发布。",
                        )
                    payload = json.loads(result_path.read_text(encoding="utf-8"))
                    if payload.get("input_identity") != request["input_identity"]:
                        raise AlignmentComputeError(
                            RESULT_MISMATCH,
                            "对齐计算结果文件与请求输入不匹配，拒绝发布。",
                        )
                    return deserialize_result(payload)
            # No result message. Classify by how the worker exited: a non-zero
            # exit (or one that never announced itself) is a crash; a clean exit
            # with no result is a compute failure. Neither publishes anything.
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                returncode = None
            if not saw_hello or (returncode is not None and returncode != 0):
                code = WORKER_CRASHED
            else:
                code = COMPUTE_FAILED
            raise AlignmentComputeError(
                code,
                f"对齐计算进程未返回结果(exit={returncode})。" + self._stderr_tail(process),
            )
        finally:
            self._kill(process)
            try:
                request_path.unlink(missing_ok=True)
                result_path.unlink(missing_ok=True)
                work_dir.rmdir()
            except OSError:
                pass
