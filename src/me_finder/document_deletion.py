"""Transactional removal of one PDF from the local literature library."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .database import delete_source_from_database
from .pdf_import_service import load_import_config, save_import_config


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
        source = self._source_record(source_file_id)
        config = load_import_config(self.config_path)
        documents = [item for item in config.get("documents", []) if isinstance(item, dict)]
        document = next((item for item in documents if item.get("source_file_id") == source_file_id), None)
        original_config = json.loads(json.dumps(config, ensure_ascii=False))
        staged: List[Tuple[Path, Path]] = []
        cleanup_warnings: List[str] = []
        try:
            if delete_generated_artifacts:
                for path in self._owned_generated_paths(
                    source_file_id,
                    document,
                    documents,
                ):
                    self._stage(path, staged)
            if delete_internal_copy:
                source_path = self._source_path(source)
                if source_path is None or not self._is_within(source_path, self.root / "corpus" / "raw_pdf"):
                    raise ValueError("只允许删除应用 corpus/raw_pdf 内保存的 PDF 副本。")
                self._stage(source_path, staged)

            config["documents"] = [
                item for item in documents if item.get("source_file_id") != source_file_id
            ]
            save_import_config(self.config_path, config)
            database_result = delete_source_from_database(
                source_file_id,
                self.database_path,
                backup_existing=True,
            )
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
            "removed_from_config": document is not None,
            "original_pdf_preserved": not delete_internal_copy,
            "internal_copy_deleted": delete_internal_copy,
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
        temporary = path.with_name(f".{path.name}.removing-{uuid.uuid4().hex}")
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
