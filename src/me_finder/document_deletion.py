"""Transactional removal of one PDF from the local literature library."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .database import backup_database, delete_sources_from_database
from .pdf_import_service import locked_import_config, save_import_config


class DocumentDeletionService:
    """Remove source-owned records while keeping the original PDF by default."""

    def __init__(self, root: Path, database_path: Path) -> None:
        self.root = Path(root).resolve()
        self.database_path = Path(database_path).resolve()
        self.config_path = self.root / "config" / "pdf_imports.json"

    def remove(
        self,
        source_file_id: str,
        *,
        delete_generated_artifacts: bool = True,
        delete_internal_copy: bool = False,
    ) -> Dict[str, object]:
        source_file_id = str(source_file_id or "").strip()
        batch = self.remove_many(
            [source_file_id],
            delete_generated_artifacts=delete_generated_artifacts,
            internal_copy_ids=[source_file_id] if delete_internal_copy else [],
        )
        failure = next(
            (item for item in batch["failures"] if item["source_id"] == source_file_id),
            None,
        )
        if failure is not None:
            raise ValueError(failure["error"])
        return {
            "source_file_id": source_file_id,
            "deleted": batch["deleted"][source_file_id],
            "backup_path": batch["backup_path"],
            "source_count": batch["source_count"],
            "paragraph_count": batch["paragraph_count"],
            "eligible_paragraph_count": batch["eligible_paragraph_count"],
            "removed_from_config": source_file_id in batch["removed_from_config"],
            "original_pdf_preserved": not delete_internal_copy,
            "internal_copy_deleted": delete_internal_copy,
            "generated_artifacts_requested": delete_generated_artifacts,
            "staged_artifact_count": batch["staged_artifact_count"],
            "cleanup_warnings": batch["cleanup_warnings"],
        }

    def remove_many(
        self,
        source_file_ids: Sequence[str],
        *,
        delete_generated_artifacts: bool = True,
        internal_copy_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, object]:
        with locked_import_config(self.config_path) as config:
            return self._remove_many_locked(
                source_file_ids,
                delete_generated_artifacts=delete_generated_artifacts,
                internal_copy_ids=internal_copy_ids,
                config=config,
            )

    def _remove_many_locked(
        self,
        source_file_ids: Sequence[str],
        *,
        delete_generated_artifacts: bool,
        internal_copy_ids: Optional[Iterable[str]],
        config: Dict[str, object],
    ) -> Dict[str, object]:
        """Remove several PDFs with one config write and one database snapshot.

        Documents that fail validation are reported in ``failures`` and left
        untouched; the rest still go through as a single transaction.
        """

        requested: List[str] = []
        for value in source_file_ids:
            text = str(value or "").strip()
            if text and text not in requested:
                requested.append(text)
        internal_requested = {
            str(value or "").strip() for value in (internal_copy_ids or [])
        }

        documents = [item for item in config.get("documents", []) if isinstance(item, dict)]
        document_by_id = {
            str(item.get("source_file_id") or ""): item for item in documents
        }

        # 校验先做完再动手：批量删除不能因为其中一份不合格就整批回滚。
        targets: List[str] = []
        failures: List[Dict[str, str]] = []
        internal_paths: Dict[str, Path] = {}
        for source_file_id in requested:
            try:
                source = self._source_record(source_file_id)
                if source_file_id in internal_requested:
                    source_path = self._source_path(source)
                    if source_path is None or not self._is_within(
                        source_path, self.root / "corpus" / "raw_pdf"
                    ):
                        raise ValueError("只允许删除应用 corpus/raw_pdf 内保存的 PDF 副本。")
                    internal_paths[source_file_id] = source_path
            except ValueError as exc:
                failures.append({"source_id": source_file_id, "error": str(exc)})
                continue
            targets.append(source_file_id)

        if not targets:
            raise ValueError(
                failures[0]["error"] if failures else "没有可移除的文献。"
            )

        removing = set(targets)
        survivors = [
            item
            for item in documents
            if str(item.get("source_file_id") or "") not in removing
        ]
        original_config = json.loads(json.dumps(config, ensure_ascii=False))
        staged: List[Tuple[Path, Path]] = []
        cleanup_warnings: List[str] = []
        # Finish the potentially multi-GB snapshot before hiding any artifact
        # or changing the config.  A process exit during the copy therefore
        # leaves the live library completely untouched.
        backup_path = backup_database(self.database_path)
        try:
            if delete_generated_artifacts:
                for source_file_id in targets:
                    document = document_by_id.get(source_file_id)
                    # 只有仍被保留文献引用的产物才受保护；批内共享的可以一起清掉。
                    visible = survivors if document is None else [*survivors, document]
                    for path in self._owned_generated_paths(
                        source_file_id, document, visible
                    ):
                        self._stage(path, staged)
            for source_file_id in targets:
                source_path = internal_paths.get(source_file_id)
                if source_path is not None:
                    self._stage(source_path, staged)

            config["documents"] = survivors
            save_import_config(self.config_path, config)
            database_result = delete_sources_from_database(
                targets,
                self.database_path,
                backup_existing=False,
            )
            database_result["backup_path"] = str(backup_path)
        except Exception as original_error:
            rollback_errors: List[str] = []
            try:
                save_import_config(self.config_path, original_config)
            except Exception as exc:
                rollback_errors.append(f"恢复导入配置失败: {exc}")
            rollback_errors.extend(self._restore_staged(staged))
            if rollback_errors:
                details = "；".join(rollback_errors)
                raise RuntimeError(f"删除失败，且回滚未完全完成：{details}") from original_error
            raise

        for original, temporary in staged:
            try:
                if temporary.is_dir():
                    shutil.rmtree(temporary)
                elif temporary.exists():
                    temporary.unlink()
            except OSError as exc:
                cleanup_warnings.append(f"{original.name}: {exc}")
        return {
            **database_result,
            "removed_source_ids": targets,
            "failures": failures,
            "removed_from_config": [
                source_file_id
                for source_file_id in targets
                if source_file_id in document_by_id
            ],
            "internal_copies_deleted": sorted(internal_paths),
            "generated_artifacts_requested": delete_generated_artifacts,
            "staged_artifact_count": len(staged),
            "cleanup_warnings": cleanup_warnings,
        }

    def _source_record(self, source_file_id: str) -> Dict[str, object]:
        connection = sqlite3.connect(str(self.database_path))
        try:
            row = connection.execute(
                "SELECT source_type, payload_json FROM source_files WHERE source_file_id = ?",
                (source_file_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("文献不存在。")
        if str(row[0]) != "pdf":
            raise ValueError("当前只能从页码校准库移除 PDF 文献。")
        return json.loads(row[1])

    def _source_path(self, source: Mapping[str, object]) -> Optional[Path]:
        relative = str(source.get("relative_path") or "").strip()
        if not relative:
            return None
        path = (self.root / relative).resolve()
        if not self._is_within(path, self.root):
            return None
        return path

    def _owned_generated_paths(
        self,
        source_file_id: str,
        document: Optional[Mapping[str, object]],
        documents: Sequence[Mapping[str, object]],
    ) -> List[Path]:
        candidates: List[Path] = []
        document_id = (
            str(document.get("document_id") or "").strip()
            if document
            else ""
        )
        if document_id:
            candidates.append(self.root / "corpus" / "parsed" / "pdf" / f"{document_id}.json")
        current_manifest = self._manifest_path(document) if document else None
        other_manifests = {
            path
            for item in documents
            if item is not document
            for path in [self._manifest_path(item)]
            if path is not None
        }
        shared_results = self._manifest_result_paths(other_manifests)
        if current_manifest is not None and current_manifest not in other_manifests:
            candidates.extend(
                path for path in self._manifest_result_paths([current_manifest]) if path not in shared_results
            )
            candidates.append(current_manifest)
        for path in self._unattached_checkpoint_paths(source_file_id):
            resolved = Path(path).resolve()
            if resolved in other_manifests:
                continue
            if any(
                resolved == shared
                or resolved in shared.parents
                or shared in resolved.parents
                for shared in shared_results
            ):
                continue
            candidates.append(resolved)
        return self._unique_safe_paths(candidates)

    def _unattached_checkpoint_paths(self, source_file_id: str) -> List[Path]:
        """Collect deterministic B checkpoints even before config attachment."""

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source_file_id):
            return []
        processed = self.root / "corpus" / "processed"
        candidates: List[Path] = [
            processed
            / "mineru"
            / "manifests"
            / f"segments-{source_file_id}.json",
            processed
            / "vision"
            / "manifests"
            / f"vision-{source_file_id}.json",
            processed / "vision" / "results" / source_file_id,
        ]
        prefix_specs = (
            (
                processed / "mineru" / "manifests",
                f"segments-{source_file_id}.json.corrupt-",
            ),
            (
                processed / "vision" / "manifests" / "work",
                f"vision-{source_file_id}-",
            ),
            (
                processed / "vision" / "manifests",
                f"vision-{source_file_id}.json.corrupt-",
            ),
            (
                processed / "mineru" / "results",
                f"{source_file_id}-p",
            ),
        )
        for directory, name_prefix in prefix_specs:
            if not directory.is_dir():
                continue
            candidates.extend(
                child
                for child in directory.iterdir()
                if child.name.startswith(name_prefix)
            )

        for directory in (
            processed / "mineru" / "tasks",
            processed / "import_jobs",
        ):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    continue
                context = (
                    payload.get("context")
                    if isinstance(payload, Mapping)
                    and isinstance(payload.get("context"), Mapping)
                    else {}
                )
                if (
                    isinstance(payload, Mapping)
                    and (
                        str(payload.get("source_file_id") or "")
                        == source_file_id
                        or str(context.get("source_file_id") or "")
                        == source_file_id
                        or str(payload.get("data_id") or "").startswith(
                            f"{source_file_id}-p"
                        )
                    )
                ):
                    candidates.append(path)
        return candidates

    def _manifest_path(self, document: Mapping[str, object]) -> Optional[Path]:
        parser_results = (
            document.get("parser_results")
            if isinstance(document.get("parser_results"), Mapping)
            else document.get("mineru")
            if isinstance(document.get("mineru"), Mapping)
            else {}
        )
        value = str(parser_results.get("manifest") or "").strip()
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        return path if self._is_within(path, self.root) else None

    def _manifest_result_paths(self, manifests: Iterable[Path]) -> set[Path]:
        paths: set[Path] = set()
        for manifest in manifests:
            if not manifest.exists():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            for segment in data.get("segments", []):
                if not isinstance(segment, Mapping):
                    continue
                values = list(segment.get("result_dirs") or [])
                if segment.get("result_dir"):
                    values.append(segment.get("result_dir"))
                if segment.get("state_file"):
                    values.append(segment.get("state_file"))
                for value in values:
                    path = Path(str(value))
                    if not path.is_absolute():
                        path = self.root / path
                    path = path.resolve()
                    if self._is_within(path, self.root):
                        paths.add(path)
            for key in ("work_manifest", "manifest_path"):
                value = str(data.get(key) or "").strip()
                if not value:
                    continue
                path = Path(value)
                if not path.is_absolute():
                    path = self.root / path
                path = path.resolve()
                if self._is_within(path, self.root):
                    paths.add(path)
        return paths

    def _unique_safe_paths(self, candidates: Iterable[Path]) -> List[Path]:
        result: List[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            path = Path(candidate).resolve()
            if path in seen or not self._is_within(path, self.root) or path == self.root:
                continue
            seen.add(path)
            if path.exists():
                result.append(path)
        result.sort(key=lambda item: len(item.parts), reverse=True)
        return result

    @staticmethod
    def _stage(path: Path, staged: List[Tuple[Path, Path]]) -> None:
        path = Path(path)
        if not path.exists():
            return
        # Do not append the full source basename: a valid near-MAX_PATH file
        # would otherwise become too long exactly when the user deletes it.
        temporary = path.with_name(f".mefinder-removing-{uuid.uuid4().hex}.tmp")
        path.replace(temporary)
        staged.append((path, temporary))

    @staticmethod
    def _restore_staged(staged: Sequence[Tuple[Path, Path]]) -> List[str]:
        errors: List[str] = []
        for original, temporary in reversed(staged):
            try:
                if not temporary.exists():
                    continue
                if original.exists():
                    errors.append(f"恢复 {original.name} 失败: 目标路径已存在")
                    continue
                temporary.replace(original)
            except Exception as exc:
                errors.append(f"恢复 {original.name} 失败: {exc}")
        return errors

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False
