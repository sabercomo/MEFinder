"""Assemble alignment, translation comparison, and reader controllers."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .app_context import AppContext
from .application.text_alignment_coordinator import TextAlignmentCoordinator
from .import_assembly import ImportAssembly
from .translation_work_controller import TranslationWorkController
from .structured_reader import get_document_citation, get_document_window
from .structured_reader_controller import StructuredReaderController
from .alignment_body_range import read_body_range_segments, read_pair_body_ranges
from .text_alignment import list_alignment_targets, locate_alignment
from .text_alignment_controller import TextAlignmentController
from .preferences import read_preferences, resolve_preferences_path


@dataclass(frozen=True)
class AlignmentAssembly:
    text_alignment_controller: TextAlignmentController
    translation_work_controller: TranslationWorkController
    structured_reader_controller: StructuredReaderController


def assemble_alignment(context: AppContext, imports: ImportAssembly) -> AlignmentAssembly:
    """Wire alignment and reader controllers with late-bound data callbacks."""

    index_runtime = imports.index_runtime
    durable_operations = imports.durable_operations
    root = context.paths.runtime_root
    text_alignment_coordinator = TextAlignmentCoordinator(
        context.paths,
        index_runtime,
        durable_operations,
    )
    text_alignment_controller = TextAlignmentController(
        text_alignment_coordinator,
        index_runtime.run_when_ready,
        list_targets=(
            lambda *args, **kwargs: list_alignment_targets(*args, **kwargs)
        ),
        locate=(
            lambda *args, **kwargs: locate_alignment(*args, **kwargs)
        ),
        read_body_ranges=(
            lambda *args, **kwargs: read_pair_body_ranges(*args, **kwargs)
        ),
        read_body_range_segments=(
            lambda *args, **kwargs: read_body_range_segments(*args, **kwargs)
        ),
        log_exception=lambda message: logging.exception(message),
    )
    translation_work_controller = TranslationWorkController(
        index_runtime.run_when_ready,
        active_model_id=lambda: str(
            read_preferences(resolve_preferences_path(root))[
                "alignment_embedding_model_id"
            ]
        ),
        log_exception=lambda message: logging.exception(message),
    )
    structured_reader_controller = StructuredReaderController(
        index_runtime.run_when_ready,
        get_window=(
            lambda *args, **kwargs: get_document_window(*args, **kwargs)
        ),
        get_citation=(
            lambda *args, **kwargs: get_document_citation(*args, **kwargs)
        ),
        log_exception=lambda message: logging.exception(message),
    )

    return AlignmentAssembly(
        text_alignment_controller=text_alignment_controller,
        translation_work_controller=translation_work_controller,
        structured_reader_controller=structured_reader_controller,
    )

