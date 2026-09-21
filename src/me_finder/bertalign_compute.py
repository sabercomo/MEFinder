"""Out-of-process compute seam for the optional Bertalign backend.

Mirrors :mod:`alignment_compute` but for the Bertalign backend, whose compute
inputs differ (a local LaBSE model path + languages + upstream params instead of
an embedding-model id + thresholds + folio candidates). It deliberately reuses
the default seam's transport primitives — the file-based control protocol,
``_ControlTail``, ``_terminate`` (process recycling), the stable error codes, and
``serialize_result`` / ``deserialize_result`` (the crossing objects are the same
``SemanticLink`` dataclasses) — so cancellation, reaping and error reporting
behave identically. It never touches the database; the main process publishes
inside its own write coordination (see ``bertalign_alignment``).

The worker side lives in :mod:`alignment_compute_worker` under ``--bertalign``;
it owns the Bertalign runtime (torch / sentence-transformers / faiss / numba),
pins single-threaded OpenMP and an isolated numba cache, and never downloads.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Sequence, Tuple

from .alignment_compute import (
    CANCELLED,
    COMPONENT_MISSING,
    COMPUTE_FAILED,
    PROTOCOL_INCOMPATIBLE,
    RESULT_MISMATCH,
    WORKER_CRASHED,
    WORKER_START_FAILED,
    AlignmentComputeError,
    _ControlTail,
    _repo_root,
    _rmtree,
    _terminate,
    deserialize_result,
)
from .bertalign_backend import (
    BERTALIGN_ALGORITHM,
    BERTALIGN_ALGORITHM_VERSION,
    BERTALIGN_MODEL_ID,
    BERTALIGN_MODEL_REVISION,
    BERTALIGN_UPSTREAM_COMMIT,
    BertalignParams,
)
from .semantic_alignment import SemanticLink

BERTALIGN_COMPUTE_PROTOCOL = 1
# The compute runtime must be able to import all of these.
BERTALIGN_REQUIRED = ("numpy", "torch", "sentence_transformers", "faiss", "numba")

ComputeResult = Tuple[List[SemanticLink], List]


def bertalign_worker_command() -> List[str]:
    """Launch command for the Bertalign compute worker.

    Frozen app: the bundled executable dispatches the ``bertalign-compute-worker``
    subcommand. Development: run the worker module with the current interpreter.
    The caller usually overrides this with the installed Bertalign runtime's
    Python via ``launch_command``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "alignment-compute-worker", "--bertalign"]
    return [sys.executable, "-m", "src.me_finder.alignment_compute_worker", "--bertalign"]


def bertalign_model_identity() -> Dict[str, str]:
    """Identity fields that must match between request and worker runtime."""
    return {
        "backend": BERTALIGN_ALGORITHM,
        "upstream_commit": BERTALIGN_UPSTREAM_COMMIT,
        "embedding_model_id": BERTALIGN_MODEL_ID,
        "model_revision": BERTALIGN_MODEL_REVISION,
        "algorithm_version": BERTALIGN_ALGORITHM_VERSION,
    }


def _params_dict(params: BertalignParams) -> Dict[str, object]:
    return {
        "max_align": params.max_align,
        "top_k": params.top_k,
        "win": params.win,
        "skip": params.skip,
        "margin": params.margin,
        "len_penalty": params.len_penalty,
    }


def _bertalign_input_identity(
    inputs: Mapping[str, object], identity: Mapping[str, str]
) -> str:
    payload = json.dumps(
        {"inputs": inputs, "identity": identity, "backend": BERTALIGN_ALGORITHM},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_bertalign_request(
    *,
    task_id: str,
    model_dir: Path,
    source_texts: Sequence[str],
    target_texts: Sequence[str],
    reviewed_body_ranges: Dict[str, List[int]] | None,
    source_language: str,
    target_language: str,
    params: BertalignParams,
) -> dict:
    identity = bertalign_model_identity()
    inputs = {
        "model_dir": str(model_dir),
        "source_texts": list(source_texts),
        "target_texts": list(target_texts),
        "reviewed_body_ranges": reviewed_body_ranges,
        "source_language": source_language,
        "target_language": target_language,
        "params": _params_dict(params),
    }
    return {
        "backend": BERTALIGN_ALGORITHM,
        "protocol": BERTALIGN_COMPUTE_PROTOCOL,
        "task_id": task_id,
        "identity": identity,
        "input_identity": _bertalign_input_identity(inputs, identity),
        "inputs": inputs,
    }


class BertalignSubprocessComputeRunner:
    """Drive the Bertalign compute in the isolated Bertalign runtime.

    Callable with the signature ``bertalign_alignment.generate_bertalign_alignment``
    expects for its ``compute_runner`` — ``(source_texts, target_texts, *,
    model_dir, reviewed_body_ranges, source_language, target_language, params)`` —
    returning ``(links, [])``.
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
        self._launch_command = (
            list(launch_command) if launch_command else bertalign_worker_command()
        )
        self._cancel_check = cancel_check
        self._poll_interval = poll_interval
        self._env = dict(env) if env is not None else None
        self._cwd = cwd

    def _spawn(self, extra_args: Sequence[str]) -> subprocess.Popen:
        env = dict(os.environ if self._env is None else self._env)
        cwd = self._cwd
        if not getattr(sys, "frozen", False) and self._cwd is None:
            root = _repo_root()
            cwd = root
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(root), existing) if p)
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(
                [*self._launch_command, *extra_args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(cwd) if cwd is not None else None,
                env={**env, "NUMBA_CACHE_DIR": str(Path(extra_args[-1]).parent / "numba-cache")},
                **kwargs,
            )
        except OSError as exc:  # pragma: no cover - launch failure is rare
            raise AlignmentComputeError(
                WORKER_START_FAILED, f"无法启动 Bertalign 计算进程：{exc}"
            ) from exc

    def _pump(self, process: subprocess.Popen, control_path: Path):
        tail = _ControlTail(control_path)
        while True:
            if self._cancel_check is not None and self._cancel_check():
                _terminate(process)
                raise AlignmentComputeError(CANCELLED, "Bertalign 计算已取消。")
            for message in tail.read_messages():
                yield message
            if process.poll() is not None:
                for message in tail.read_messages():
                    yield message
                return
            time.sleep(self._poll_interval)

    def probe(self) -> Dict[str, object]:
        work_dir = Path(tempfile.mkdtemp(prefix="mefinder-bertalign-probe-"))
        process = None
        try:
            control_path = work_dir / "control.ndjson"
            process = self._spawn(["--probe", str(control_path)])
            for message in self._pump(process, control_path):
                kind = message.get("type")
                if kind == "hello":
                    if message.get("protocol") != BERTALIGN_COMPUTE_PROTOCOL:
                        raise AlignmentComputeError(
                            PROTOCOL_INCOMPATIBLE, "Bertalign 计算进程协议不兼容。"
                        )
                    caps = dict(message.get("capabilities") or {})
                    missing = [n for n in BERTALIGN_REQUIRED if not caps.get(n)]
                    if missing:
                        raise AlignmentComputeError(
                            COMPONENT_MISSING,
                            "Bertalign 计算运行时缺少依赖：" + "、".join(missing),
                        )
                    return caps
                if kind == "error":
                    raise AlignmentComputeError(
                        str(message.get("code") or COMPONENT_MISSING),
                        str(message.get("message") or "Bertalign 计算进程无法启动。"),
                    )
            raise AlignmentComputeError(
                WORKER_CRASHED,
                f"Bertalign 计算进程未返回能力应答(exit={process.poll() if process else None})。",
            )
        finally:
            if process is not None:
                _terminate(process)
            _rmtree(work_dir)

    def __call__(
        self,
        source_texts: Sequence[str],
        target_texts: Sequence[str],
        *,
        model_dir: Path,
        reviewed_body_ranges: Dict[str, List[int]] | None = None,
        source_language: str = "und",
        target_language: str = "und",
        params: BertalignParams | None = None,
    ) -> ComputeResult:
        params = params or BertalignParams()
        request = build_bertalign_request(
            task_id=self._task_id,
            model_dir=model_dir,
            source_texts=source_texts,
            target_texts=target_texts,
            reviewed_body_ranges=reviewed_body_ranges,
            source_language=source_language,
            target_language=target_language,
            params=params,
        )
        work_dir = Path(tempfile.mkdtemp(prefix="mefinder-bertalign-compute-"))
        process = None
        try:
            request_path = work_dir / "request.json"
            result_path = work_dir / "result.json"
            control_path = work_dir / "control.ndjson"
            request_path.write_text(
                json.dumps(request, ensure_ascii=False), encoding="utf-8"
            )
            process = self._spawn([str(request_path), str(result_path), str(control_path)])
            saw_hello = False
            for message in self._pump(process, control_path):
                kind = message.get("type")
                if kind == "hello":
                    saw_hello = True
                    if message.get("protocol") != BERTALIGN_COMPUTE_PROTOCOL:
                        raise AlignmentComputeError(
                            PROTOCOL_INCOMPATIBLE, "Bertalign 计算进程协议不兼容。"
                        )
                elif kind == "progress":
                    continue
                elif kind == "error":
                    raise AlignmentComputeError(
                        str(message.get("code") or COMPUTE_FAILED),
                        str(message.get("message") or "Bertalign 计算失败。"),
                    )
                elif kind == "result":
                    return self._consume_result(message, request, result_path)
            returncode = process.poll()
            code = WORKER_CRASHED if (not saw_hello or returncode not in (0, None)) else COMPUTE_FAILED
            raise AlignmentComputeError(
                code, f"Bertalign 计算进程未返回结果(exit={returncode})。"
            )
        finally:
            if process is not None:
                _terminate(process)
            _rmtree(work_dir)

    def _consume_result(self, message, request, result_path: Path) -> ComputeResult:
        if message.get("input_identity") != request["input_identity"]:
            raise AlignmentComputeError(
                RESULT_MISMATCH, "Bertalign 计算结果与请求输入不匹配，拒绝发布。"
            )
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AlignmentComputeError(
                COMPUTE_FAILED, f"无法读取 Bertalign 计算结果：{exc}"
            ) from exc
        if payload.get("protocol") != BERTALIGN_COMPUTE_PROTOCOL:
            raise AlignmentComputeError(
                PROTOCOL_INCOMPATIBLE, "Bertalign 计算结果协议不兼容，拒绝发布。"
            )
        if str(payload.get("task_id")) != str(request["task_id"]):
            raise AlignmentComputeError(
                RESULT_MISMATCH, "Bertalign 计算结果任务标识不匹配，拒绝发布。"
            )
        if payload.get("input_identity") != request["input_identity"]:
            raise AlignmentComputeError(
                RESULT_MISMATCH, "Bertalign 计算结果文件与请求输入不匹配，拒绝发布。"
            )
        return deserialize_result(payload)
