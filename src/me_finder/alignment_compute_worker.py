"""Compute-side worker for the alignment compute seam.

Runs in a separate process that owns the NumPy / ONNX Runtime / fastembed
stack. It reads a versioned request file, runs only the *compute* phase
(``align_segment_sequences``), and writes a versioned result file.

Transport is **file-based**, deliberately: the worker never reads or writes the
process's inherited stdin/stdout/stderr for the protocol. In a windowed
(``console=False``) frozen app those handles are ``None`` on Windows, and a
stderr pipe can dead-lock a long run if it fills. So control messages
(``hello`` / ``progress`` / ``result`` / ``error``) are line-delimited JSON
appended to a **control file** whose path is passed in, and the worker redirects
its own ``sys.stdout``/``sys.stderr`` to the null device so library output can
never corrupt anything or block. Diagnostics (a traceback) are folded into the
``error`` message.

The worker never opens the official database, never re-parses source files and
never triggers OCR or a model download. Capability and algorithm/version
identity are reported and enforced from *this* process (the runtime that would
actually compute).

Usage:
    python -m src.me_finder.alignment_compute_worker --probe <control.ndjson>
    python -m src.me_finder.alignment_compute_worker <request.json> <result.json> <control.ndjson>
"""

from __future__ import annotations

import json
import os
import sys
import time
from importlib.util import find_spec
from pathlib import Path
from typing import IO

from .alignment_compute import (
    ALIGNMENT_COMPUTE_PROTOCOL,
    COMPONENT_MISSING,
    COMPUTE_FAILED,
    PROTOCOL_INCOMPATIBLE,
    SIMULATE_ENV,
    input_identity,
    model_identity,
    serialize_result,
)

_REQUIRED = ("numpy", "fastembed", "onnxruntime")


def _apply_bertalign_runtime_knobs(*, offline: bool = True) -> None:
    """Use the validated CPU thread configuration and explicit offline mode."""
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ["HF_HUB_OFFLINE"] = "1" if offline else "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "1" if offline else "0"



def _bertalign_capabilities() -> dict:
    from .bertalign_compute import BERTALIGN_REQUIRED

    return {name: find_spec(name) is not None for name in BERTALIGN_REQUIRED}


def _bertalign_main(argv: list[str], positional: list[str]) -> int:
    """Handle a ``--bertalign`` probe or compute request in this runtime."""

    from .bertalign_compute import (
        BERTALIGN_COMPUTE_PROTOCOL,
        BERTALIGN_REQUIRED,
        _bertalign_input_identity,
        bertalign_model_identity,
    )

    if any(flag in argv for flag in ("--verify", "--verify-model", "--download-bertalign-model")):
        from .bertalign_provisioning import verify_bertalign
        return verify_bertalign(argv, positional, _emit)

    if "--probe" in argv:
        control = _open_control(positional[0])
        _emit(
            control,
            type="hello",
            protocol=BERTALIGN_COMPUTE_PROTOCOL,
            capabilities=_bertalign_capabilities(),
            pid=os.getpid(),
        )
        return 0

    if len(positional) < 3:
        control = _open_control(positional[-1])
        _emit(control, type="error", code=COMPUTE_FAILED,
              message="bertalign worker 需要 <request> <result> <control> 三个路径参数。")
        return 2
    request_path, result_path, control_path = (
        Path(positional[0]), Path(positional[1]), positional[2]
    )
    control = _open_control(control_path)
    _emit(control, type="hello", protocol=BERTALIGN_COMPUTE_PROTOCOL,
          capabilities=_bertalign_capabilities(), pid=os.getpid())

    missing = [name for name in BERTALIGN_REQUIRED if not _bertalign_capabilities().get(name)]
    if missing:
        _emit(control, type="error", code=COMPONENT_MISSING,
              message="Bertalign 计算运行时缺少依赖：" + "、".join(missing))
        return 4
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _emit(control, type="error", code=COMPUTE_FAILED, message=f"无法读取请求：{exc}")
        return 2
    if request.get("protocol") != BERTALIGN_COMPUTE_PROTOCOL:
        _emit(control, type="error", code=PROTOCOL_INCOMPATIBLE,
              message="Bertalign 请求协议不兼容。")
        return 2
    own = bertalign_model_identity()
    if request.get("identity") != own:
        _emit(control, type="error", code=PROTOCOL_INCOMPATIBLE,
              message=f"Bertalign 身份不匹配：{request.get('identity')} != {own}")
        return 5
    try:
        return _run_bertalign(request, result_path, control, _bertalign_input_identity, own)
    except Exception as exc:  # noqa: BLE001 - report any compute failure clearly
        import traceback

        _emit(control, type="error", code=COMPUTE_FAILED,
              message=str(exc) + "\n" + traceback.format_exc()[-1500:])
        return 1


def _run_bertalign(request, result_path, control, input_identity_fn, identity) -> int:
    from .bertalign_backend import BertalignParams, align_segments_bertalign

    import torch
    import faiss
    torch.set_num_threads(1)
    faiss.omp_set_num_threads(1)

    inputs = request["inputs"]
    params = BertalignParams(**inputs["params"])
    _emit(control, type="progress", task_id=request.get("task_id"), stage="compute-start")
    computed = align_segments_bertalign(
        list(inputs["source_texts"]),
        list(inputs["target_texts"]),
        model_dir=Path(inputs["model_dir"]),
        reviewed_body_ranges=inputs["reviewed_body_ranges"],
        source_language=inputs["source_language"],
        target_language=inputs["target_language"],
        params=params,
    )
    recomputed = input_identity_fn(inputs, identity)
    payload = serialize_result(
        task_id=request.get("task_id", ""), identity=recomputed, computed=computed
    )
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _emit(control, type="result", task_id=request.get("task_id"), input_identity=recomputed)
    return 0


def _capabilities() -> dict:
    """Report what this runtime can import. Runs in the compute process."""

    simulate = os.environ.get(SIMULATE_ENV)
    if simulate == "component_missing":
        return {name: False for name in _REQUIRED}
    return {name: find_spec(name) is not None for name in _REQUIRED}


def _emit(control: IO[str], **message: object) -> None:
    control.write(json.dumps(message, ensure_ascii=False) + "\n")
    control.flush()


def _run_compute(request: dict, result_path: Path, control: IO[str]) -> int:
    from .alignment_compute import run_in_process, _deserialize_folio
    from .embedding_models import AlignmentThresholds

    inputs = request["inputs"]
    identity = request["model_identity"]
    model_id = identity["embedding_model_id"]

    # Enforce version identity from *this* runtime: the request must expect the
    # exact algorithm/model versions this worker actually implements. Otherwise
    # a mismatched independent-component upgrade could pass off wrong-version
    # results as current.
    own = model_identity(model_id)
    if identity != own:
        mismatched = {
            k: (identity.get(k), own.get(k))
            for k in own
            if identity.get(k) != own.get(k)
        }
        _emit(
            control,
            type="error",
            code=PROTOCOL_INCOMPATIBLE,
            message=f"算法/模型版本不匹配：{mismatched}",
        )
        return 5

    thresholds = AlignmentThresholds(**inputs["thresholds"])
    folio_candidates = [_deserialize_folio(c) for c in inputs["folio_candidates"]]
    reusable = tuple(tuple(seq) for seq in inputs["reusable_sequences"])

    _emit(control, type="progress", task_id=request.get("task_id"), stage="compute-start")
    computed = run_in_process(
        list(inputs["source_texts"]),
        list(inputs["target_texts"]),
        cache_dir=Path(request["cache_dir"]),
        embedding_model_id=model_id,
        thresholds=thresholds,
        reusable_sequences=reusable,
        folio_candidates=folio_candidates,
        source_language=inputs["source_language"],
        target_language=inputs["target_language"],
        reviewed_body_ranges=inputs["reviewed_body_ranges"],
    )

    # Recompute the identity from what we actually received and computed on, so
    # the main process can refuse a stale/mismatched result.
    recomputed = input_identity(inputs, own)
    if os.environ.get(SIMULATE_ENV) == "tamper_identity":
        recomputed = "tampered-" + recomputed
    payload = serialize_result(
        task_id=request.get("task_id", ""), identity=recomputed, computed=computed
    )
    if os.environ.get(SIMULATE_ENV) == "tamper_protocol":
        payload["protocol"] = 999
    if os.environ.get(SIMULATE_ENV) == "tamper_task":
        payload["task_id"] = "not-the-task"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _emit(
        control,
        type="result",
        task_id=request.get("task_id"),
        input_identity=recomputed,
    )
    return 0


def _open_control(path: str) -> IO[str]:
    return open(path, "a", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Never depend on inherited std streams: a windowed frozen app has them as
    # None, and library output must never block or corrupt the protocol. Point
    # them at the null device for the worker's whole lifetime.
    try:
        _null = open(os.devnull, "w")
        sys.stdout = _null
        sys.stderr = _null
    except OSError:
        pass

    positional = [a for a in argv if not a.startswith("--")]
    if not positional:
        return 2  # no control file to report to
    simulate = os.environ.get(SIMULATE_ENV)

    if "--bertalign" in argv:
        # Optional Bertalign backend: pin runtime knobs before any heavy import.
        os.environ.setdefault("NUMBA_CACHE_DIR", str(Path(positional[-1]).parent / "numba-cache"))
        _apply_bertalign_runtime_knobs(offline="--download-bertalign-model" not in argv)
        return _bertalign_main(argv, positional)

    if "--download-model" in argv:
        # Download and load one embedding model *in this isolated runtime*, so a
        # main process without the numeric stack can still complete the model
        # download + probe closure. positional: [model_id, cache_dir, control].
        control = _open_control(positional[2])
        model_id, cache_dir = positional[0], positional[1]
        try:
            from .managed_embedding_models import download_embedding_model

            download_embedding_model(model_id, Path(cache_dir))
        except BaseException as exc:  # noqa: BLE001 - report any download/load failure
            import traceback

            _emit(control, type="error", code=COMPUTE_FAILED,
                  message=str(exc) + "\n" + traceback.format_exc()[-1500:])
            return 1
        _emit(control, type="result", model_id=model_id)
        return 0

    if "--verify" in argv:
        # Install-time validation: actually LOAD the numeric stack in this
        # isolated interpreter, not merely resolve it. ``find_spec`` can succeed
        # for a package whose import raises (broken wheel, ABI mismatch); only a
        # real import + a trivial op proves the runtime can compute.
        control = _open_control(positional[0])
        try:
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401
            import fastembed  # noqa: F401

            numpy.zeros(1) + 0  # force real initialization, not lazy resolution
        except BaseException as exc:  # noqa: BLE001 - any load failure fails validation
            _emit(control, type="error", code=COMPONENT_MISSING,
                  message=f"独立运行时无法加载计算依赖：{exc}")
            return 4
        _emit(control, type="hello", protocol=ALIGNMENT_COMPUTE_PROTOCOL,
              capabilities={name: True for name in _REQUIRED}, pid=os.getpid())
        return 0

    if any(flag in argv for flag in ("--verify", "--verify-model", "--download-bertalign-model")):
        from .bertalign_provisioning import verify_bertalign
        return verify_bertalign(argv, positional, _emit)

    if "--probe" in argv:
        control = _open_control(positional[0])
        if simulate == "noisy":
            # Emulate a library dumping a large diagnostic to the OS std fds
            # before the protocol is sent. A pipe would back-pressure and dead-
            # lock; DEVNULL std streams make it harmless.
            blob = b"x" * (1 << 20)
            for fd in (1, 2):
                try:
                    os.write(fd, blob)
                except OSError:
                    pass
        protocol = 999 if simulate == "probe_protocol" else ALIGNMENT_COMPUTE_PROTOCOL
        _emit(control, type="hello", protocol=protocol, capabilities=_capabilities(), pid=os.getpid())
        return 0

    if len(positional) < 3:
        control = _open_control(positional[-1])
        _emit(control, type="error", code=COMPUTE_FAILED,
              message="worker 需要 <request> <result> <control> 三个路径参数。")
        return 2
    request_path, result_path, control_path = (Path(positional[0]), Path(positional[1]), positional[2])
    control = _open_control(control_path)

    _emit(control, type="hello", protocol=ALIGNMENT_COMPUTE_PROTOCOL, capabilities=_capabilities(), pid=os.getpid())

    # Fault injection for acceptance tests (never set in production).
    if simulate == "crash":
        _emit(control, type="progress", stage="about-to-crash")
        return 3
    if simulate == "hang":
        _emit(control, type="progress", stage="hanging")
        while True:
            time.sleep(0.05)

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _emit(control, type="error", code=COMPUTE_FAILED, message=f"无法读取请求：{exc}")
        return 2

    if simulate == "tamper_identity" or simulate == "tamper_protocol" or simulate == "tamper_task":
        # Emit a result with a broken field, without running the real compute.
        payload = {
            "protocol": 999 if simulate == "tamper_protocol" else ALIGNMENT_COMPUTE_PROTOCOL,
            "task_id": "not-the-task" if simulate == "tamper_task" else request.get("task_id", ""),
            "input_identity": ("tampered-" if simulate == "tamper_identity" else "")
            + str(request.get("input_identity", "")),
            "links": [],
            "anchors": [],
        }
        result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _emit(control, type="result", task_id=payload["task_id"], input_identity=payload["input_identity"])
        return 0

    if request.get("protocol") != ALIGNMENT_COMPUTE_PROTOCOL:
        _emit(control, type="error", code=PROTOCOL_INCOMPATIBLE,
              message=f"请求协议不兼容（request={request.get('protocol')!r}, expected={ALIGNMENT_COMPUTE_PROTOCOL}）。")
        return 2

    missing = [name for name in _REQUIRED if not _capabilities().get(name)]
    if missing:
        _emit(control, type="error", code=COMPONENT_MISSING,
              message="对齐计算运行时缺少依赖：" + "、".join(missing))
        return 4

    try:
        return _run_compute(request, result_path, control)
    except Exception as exc:  # noqa: BLE001 - report any compute failure clearly
        import traceback

        _emit(control, type="error", code=COMPUTE_FAILED,
              message=str(exc) + "\n" + traceback.format_exc()[-1500:])
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
