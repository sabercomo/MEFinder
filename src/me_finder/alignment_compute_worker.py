"""Compute-side worker for the alignment compute seam.

Runs in a separate process that owns the NumPy / ONNX Runtime / fastembed
stack. It reads a versioned request file, runs only the *compute* phase
(``align_segment_sequences``), and writes a versioned result file. Control
messages (``hello`` / ``progress`` / ``result`` / ``error``) are line-delimited
JSON on **stdout**; everything else — logging, library warnings — goes to
**stderr**, so the control channel is never polluted.

The worker never opens the official database, never re-parses source files and
never triggers OCR or a model download. It only turns texts into semantic
links. Capability is reported from *this* process (the runtime that would
actually compute), not inferred by the caller.

Usage:
    python -m src.me_finder.alignment_compute_worker --probe
    python -m src.me_finder.alignment_compute_worker <request.json> <result.json>
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
    deserialize_result,  # noqa: F401 (kept for symmetry / potential reuse)
    input_identity,
    serialize_result,
)

_REQUIRED = ("numpy", "fastembed", "onnxruntime")


def _capabilities() -> dict:
    """Report what this runtime can import. Runs in the compute process."""

    simulate = os.environ.get(SIMULATE_ENV)
    if simulate == "component_missing":
        return {name: False for name in _REQUIRED}
    return {name: find_spec(name) is not None for name in _REQUIRED}


def _emit(stream: IO[str], **message: object) -> None:
    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    stream.flush()


def _run_compute(request: dict, result_path: Path, control: IO[str]) -> int:
    from .alignment_compute import run_in_process
    from .edition_folio_anchors import FolioBoundaryCandidate  # noqa: F401
    from .embedding_models import AlignmentThresholds
    from .alignment_compute import _deserialize_folio  # local helper reuse

    inputs = request["inputs"]
    identity = request["model_identity"]
    thresholds = AlignmentThresholds(**inputs["thresholds"])
    folio_candidates = [_deserialize_folio(c) for c in inputs["folio_candidates"]]
    reusable = tuple(tuple(seq) for seq in inputs["reusable_sequences"])

    _emit(control, type="progress", task_id=request.get("task_id"), stage="compute-start")
    computed = run_in_process(
        list(inputs["source_texts"]),
        list(inputs["target_texts"]),
        cache_dir=Path(request["cache_dir"]),
        embedding_model_id=identity["embedding_model_id"],
        thresholds=thresholds,
        reusable_sequences=reusable,
        folio_candidates=folio_candidates,
        source_language=inputs["source_language"],
        target_language=inputs["target_language"],
        reviewed_body_ranges=inputs["reviewed_body_ranges"],
    )

    # Recompute the identity from what we actually received and computed on, so
    # the main process can refuse a stale/mismatched result.
    recomputed = input_identity(inputs, identity)
    if os.environ.get(SIMULATE_ENV) == "tamper_identity":
        recomputed = "tampered-" + recomputed
    payload = serialize_result(
        task_id=request.get("task_id", ""), identity=recomputed, computed=computed
    )
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _emit(
        control,
        type="result",
        task_id=request.get("task_id"),
        input_identity=recomputed,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Separate the control channel from diagnostics: capture the real stdout for
    # control messages, then point sys.stdout at stderr so any library print()
    # during compute cannot corrupt the protocol stream.
    control: IO[str] = sys.stdout
    sys.stdout = sys.stderr

    simulate = os.environ.get(SIMULATE_ENV)

    # Probe mode: announce capabilities and exit. Used by the runner instead of
    # a main-process find_spec check.
    if "--probe" in argv:
        protocol = 999 if simulate == "probe_protocol" else ALIGNMENT_COMPUTE_PROTOCOL
        _emit(control, type="hello", protocol=protocol, capabilities=_capabilities(), pid=os.getpid())
        return 0

    _emit(control, type="hello", protocol=ALIGNMENT_COMPUTE_PROTOCOL, capabilities=_capabilities(), pid=os.getpid())

    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) < 2:
        _emit(control, type="error", code=COMPUTE_FAILED, message="worker 需要 <request> <result> 两个路径参数。")
        return 2
    request_path, result_path = Path(positional[0]), Path(positional[1])

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

    if simulate == "tamper_identity":
        # Emit a result whose identity does not match the request, without
        # running the real compute. The runner must refuse to publish it.
        result_path.write_text(
            json.dumps(
                {
                    "protocol": ALIGNMENT_COMPUTE_PROTOCOL,
                    "task_id": request.get("task_id", ""),
                    "input_identity": "tampered-" + str(request.get("input_identity", "")),
                    "links": [],
                    "anchors": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _emit(
            control,
            type="result",
            task_id=request.get("task_id"),
            input_identity="tampered-" + str(request.get("input_identity", "")),
        )
        return 0

    if request.get("protocol") != ALIGNMENT_COMPUTE_PROTOCOL:
        _emit(
            control,
            type="error",
            code=PROTOCOL_INCOMPATIBLE,
            message=f"请求协议不兼容（request={request.get('protocol')!r}, expected={ALIGNMENT_COMPUTE_PROTOCOL}）。",
        )
        return 2

    missing = [name for name in _REQUIRED if not _capabilities().get(name)]
    if missing:
        _emit(
            control,
            type="error",
            code=COMPONENT_MISSING,
            message="对齐计算运行时缺少依赖：" + "、".join(missing),
        )
        return 4

    try:
        return _run_compute(request, result_path, control)
    except Exception as exc:  # noqa: BLE001 - report any compute failure clearly
        import traceback

        traceback.print_exc()  # to stderr (diagnostics)
        _emit(control, type="error", code=COMPUTE_FAILED, message=str(exc))
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
