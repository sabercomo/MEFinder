"""Stable application service for resumable large-document parsing."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from ..document_export import export_document_zip
from ..import_resume import options_fingerprint
from ..parser_provider import (
    NormalizedParseResult,
    ParserProvider,
    ParserProviderError,
    ParserRequest,
    ParserSubmission,
    ParserTaskStatus,
)
from .io_utils import sha256_file
from .credential_pool import CredentialPool, CredentialPoolUnavailable
from .job_ledger import DocumentJob, JobLedger, SliceJob
from .merge import (
    CoverageValidationError,
    iter_normalized_pages,
    merge_normalized_result_files,
    write_normalized_result,
)
from .publisher import AtomicPublisher
from .slicing import (
    PhysicalPDFSlicer,
    SlicePlanner,
    SliceRange,
    original_file_descriptor,
)


PageCounter = Callable[[Path], int]


def pymupdf_page_count(path: Path) -> int:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise RuntimeError("PyMuPDF is required to plan large PDF jobs") from exc
    document = fitz.open(str(path))
    try:
        return len(document)
    finally:
        document.close()


class LargeDocumentJobEngine:
    """Coordinate physical slices without knowing provider HTTP details."""

    def __init__(
        self,
        *,
        ledger: JobLedger,
        provider: ParserProvider,
        work_dir: Path,
        planner: Optional[SlicePlanner] = None,
        slicer: Optional[PhysicalPDFSlicer] = None,
        page_counter: PageCounter = pymupdf_page_count,
        max_attempts: int = 3,
        publisher: Optional[AtomicPublisher] = None,
        credential_pool: Optional[CredentialPool] = None,
    ) -> None:
        self.ledger = ledger
        self.provider = provider
        self.work_dir = Path(work_dir)
        self.planner = planner or SlicePlanner()
        self.slicer = slicer or PhysicalPDFSlicer()
        self.page_counter = page_counter
        self.max_attempts = max(1, int(max_attempts))
        self.publisher = publisher or AtomicPublisher()
        self.credential_pool = credential_pool

    def prepare(
        self,
        *,
        source_path: Path,
        source_file_id: str,
        document_id: str,
        model: Optional[str] = None,
        options: Optional[Mapping[str, object]] = None,
    ) -> DocumentJob:
        source = Path(source_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        source_digest = sha256_file(source)
        total_pages = int(self.page_counter(source))
        if total_pages < 1:
            raise ValueError("PDF has no pages")
        capabilities = self.provider.capabilities()
        parse_options: Dict[str, object] = {
            **dict(options or {}),
            "model": model,
            "provider_id": self.provider.provider_id,
            "capability_max_pages": capabilities.max_pages_per_file,
            "capability_max_bytes": capabilities.max_bytes_per_file,
        }
        fingerprint = options_fingerprint(parse_options)
        resumable = self.ledger.find_resumable_job(
            source_file_id=source_file_id,
            source_sha256=source_digest,
            provider_id=self.provider.provider_id,
            options_fingerprint=fingerprint,
        )
        if resumable is not None:
            self._repair_local_artifacts(resumable)
            return self.ledger.get_document_job(resumable.id)

        job = self.ledger.create_document_job(
            source_file_id=source_file_id,
            document_id=document_id,
            source_path=source,
            source_sha256=source_digest,
            provider_id=self.provider.provider_id,
            parser_model=model,
            options_fingerprint=fingerprint,
            total_pages=total_pages,
        )
        try:
            requires_physical_slices = bool(
                (
                    capabilities.max_pages_per_file is not None
                    and total_pages > capabilities.max_pages_per_file
                )
                or (
                    capabilities.max_bytes_per_file is not None
                    and source.stat().st_size > capabilities.max_bytes_per_file
                )
            )
            if requires_physical_slices:
                ranges = self.planner.plan(
                    total_pages=total_pages,
                    total_bytes=source.stat().st_size,
                    capabilities=capabilities,
                )
                descriptors = self.slicer.create_slices(
                    source,
                    ranges,
                    self._job_dir(job.id) / "slices",
                    max_bytes_per_file=capabilities.max_bytes_per_file,
                )
            else:
                descriptors = [
                    original_file_descriptor(source, total_pages=total_pages)
                ]
            self.ledger.add_slices(job.id, descriptors, self.provider.provider_id)
        except Exception as exc:
            self.ledger.update_document(
                job.id,
                status="permanent_failure",
                error_summary=str(exc)[:2000],
            )
            raise
        return self.ledger.get_document_job(job.id)

    def run_once(self, job_id: str) -> DocumentJob:
        """Advance every slice once; repeated calls resume polling/retries."""

        job = self.ledger.get_document_job(job_id)
        source = Path(job.source_path)
        if not source.is_file() or sha256_file(source) != job.source_sha256:
            return self.ledger.update_document(
                job.id,
                status="permanent_failure",
                error_summary="source file changed; old parser job will not be resumed",
            )
        self._repair_local_artifacts(job)
        self.ledger.update_document(job.id, status="running", error_summary=None)
        for slice_job in self.ledger.list_slice_jobs(job.id):
            if slice_job.status == "completed":
                continue
            if slice_job.status in {"permanent_failure", "cancelled"}:
                continue
            try:
                self._advance_slice(job, slice_job)
            except CredentialPoolUnavailable as exc:
                self.ledger.update_slice(
                    slice_job.id,
                    status="waiting",
                    last_error=str(exc)[:2000],
                )
            except ParserProviderError as exc:
                self._record_provider_failure(slice_job, exc)
            except Exception as exc:
                self.ledger.update_slice(
                    slice_job.id,
                    status="retryable_failure"
                    if slice_job.attempt_count < self.max_attempts
                    else "permanent_failure",
                    last_error=str(exc)[:2000],
                )
        job = self.ledger.refresh_progress(job.id)
        slices = self.ledger.list_slice_jobs(job.id)
        if any(item.status == "permanent_failure" for item in slices):
            return self.ledger.update_document(
                job.id,
                status="permanent_failure",
                error_summary="one or more slices failed permanently",
            )
        if all(item.status == "completed" for item in slices):
            try:
                merge_normalized_result_files(
                    slices,
                    self._merged_path(job.id),
                    total_pages=job.total_pages,
                )
            except CoverageValidationError as exc:
                return self.ledger.update_document(
                    job.id,
                    status="permanent_failure",
                    error_summary=f"merge validation failed ({exc.code}): {exc}",
                )
            return self.ledger.update_document(
                job.id,
                status="validated",
                error_summary=None,
            )
        if any(item.status in {"submitted", "waiting"} for item in slices):
            status = "waiting"
        elif any(item.status == "retryable_failure" for item in slices):
            status = "retryable_failure"
        else:
            status = "running"
        return self.ledger.update_document(job.id, status=status)

    def publish(
        self,
        job_id: str,
        *,
        manifest: Mapping[str, object],
        destination: Path,
        publish_index: Optional[Callable[[], None]] = None,
    ) -> DocumentJob:
        job = self.ledger.get_document_job(job_id)
        requested_destination = Path(destination).resolve()
        if (
            job.status == "published"
            and job.published_export_path
            and Path(job.published_export_path).resolve() == requested_destination
            and requested_destination.is_file()
        ):
            # A resumed caller may not know whether the final atomic switch
            # completed before its process exited.  The ledger plus final file
            # make publishing the same destination idempotent.
            return job
        if job.status not in {"validated", "published"}:
            raise RuntimeError("document must pass merge validation before publish")
        merged_path = self._merged_path(job.id)
        if not merged_path.is_file():
            raise RuntimeError("validated merged result is missing")
        candidate = self._job_dir(job.id) / "document.mefinder.zip"
        export_document_zip(candidate, manifest, iter_normalized_pages(merged_path))
        self.ledger.update_document(job.id, publish_status="publishing")
        try:
            final_path = self.publisher.publish(
                candidate,
                Path(destination),
                publish_index=publish_index,
            )
        except Exception as exc:
            self.ledger.update_document(
                job.id,
                publish_status="failed",
                error_summary=str(exc)[:2000],
            )
            raise
        return self.ledger.update_document(
            job.id,
            status="published",
            publish_status="published",
            published_export_path=str(final_path.resolve()),
            error_summary=None,
        )

    def _advance_slice(self, job: DocumentJob, slice_job: SliceJob) -> None:
        request = self._parser_request(job, slice_job)
        if slice_job.remote_task_id:
            credential = (
                self.credential_pool.credential_for_affinity(slice_job.credential_id)
                if self.credential_pool is not None
                else None
            )
            poll = self.provider.poll(
                slice_job.remote_task_id,
                credential=credential,
            )
            if poll.status == ParserTaskStatus.COMPLETED:
                submission = ParserSubmission(
                    provider_id=self.provider.provider_id,
                    remote_task_id=slice_job.remote_task_id,
                    status=ParserTaskStatus.COMPLETED,
                )
                self._complete_slice(
                    slice_job,
                    request,
                    submission,
                    credential=credential,
                )
                if self.credential_pool is not None:
                    self.credential_pool.finish_remote(slice_job.credential_id)
            elif poll.status in {
                ParserTaskStatus.PERMANENT_FAILURE,
                ParserTaskStatus.CANCELLED,
            }:
                self.ledger.update_slice(
                    slice_job.id,
                    status=(
                        "cancelled"
                        if poll.status == ParserTaskStatus.CANCELLED
                        else "permanent_failure"
                    ),
                    last_error=poll.message,
                )
                if self.credential_pool is not None:
                    self.credential_pool.finish_remote(slice_job.credential_id)
            else:
                self.ledger.update_slice(
                    slice_job.id,
                    status="waiting",
                    last_error=None,
                )
            return

        if slice_job.attempt_count >= self.max_attempts:
            self.ledger.update_slice(
                slice_job.id,
                status="permanent_failure",
                last_error=slice_job.last_error or "maximum submit attempts reached",
            )
            return
        current_attempt = slice_job.attempt_count + 1
        lease = (
            self.credential_pool.acquire()
            if self.credential_pool is not None
            else None
        )
        self.ledger.update_slice(
            slice_job.id,
            status="running",
            attempt_count=current_attempt,
            credential_id=(
                lease.credential.credential_id if lease is not None else None
            ),
            last_error=None,
        )
        try:
            submission = self.provider.submit(
                request,
                credential=lease.credential if lease is not None else None,
            )
        except ParserProviderError as exc:
            if self.credential_pool is not None and lease is not None:
                self.credential_pool.record_error(
                    lease.credential.credential_id, exc
                )
                self.credential_pool.release_unsubmitted(lease)
            raise
        except Exception:
            if self.credential_pool is not None and lease is not None:
                self.credential_pool.release_unsubmitted(lease)
            raise
        if submission.status == ParserTaskStatus.COMPLETED:
            try:
                self._complete_slice(
                    slice_job,
                    request,
                    submission,
                    credential=lease.credential if lease is not None else None,
                )
            finally:
                if self.credential_pool is not None and lease is not None:
                    self.credential_pool.finish_remote(
                        lease.credential.credential_id
                    )
            return
        if not submission.remote_task_id:
            raise ParserProviderError(
                "asynchronous provider returned no remote task id",
                provider_id=self.provider.provider_id,
            )
        self.ledger.update_slice(
            slice_job.id,
            status="submitted",
            remote_task_id=submission.remote_task_id,
            credential_id=(
                lease.credential.credential_id if lease is not None else None
            ),
            last_error=None,
        )

    def _complete_slice(
        self,
        slice_job: SliceJob,
        request: ParserRequest,
        submission: ParserSubmission,
        *,
        credential=None,
    ) -> None:
        raw = self.provider.fetch_result(
            submission,
            request,
            credential=credential,
        )
        normalized = self.provider.normalize_result(raw, request)
        expected = list(range(slice_job.page_start, slice_job.page_end + 1))
        actual = [page.physical_pdf_page for page in normalized.pages]
        if actual != expected:
            raise ParserProviderError(
                f"normalized slice coverage {actual[:3]}... does not match "
                f"{slice_job.page_start}-{slice_job.page_end}",
                provider_id=self.provider.provider_id,
            )
        result_path = self._job_dir(slice_job.document_job_id) / "results" / (
            f"slice-{slice_job.page_start:06d}-{slice_job.page_end:06d}.ndjson"
        )
        result_sha = write_normalized_result(result_path, normalized)
        self.ledger.update_slice(
            slice_job.id,
            status="completed",
            result_path=str(result_path.resolve()),
            result_sha256=result_sha,
            last_error=None,
        )

    def _record_provider_failure(
        self, slice_job: SliceJob, exc: ParserProviderError
    ) -> None:
        updates: Dict[str, object] = {"last_error": str(exc)[:2000]}
        if exc.remote_task_missing:
            # Only an explicit upstream "task missing" classification permits a
            # new submission.  Network ambiguity keeps remote-task affinity.
            updates["remote_task_id"] = None
            if self.credential_pool is not None:
                self.credential_pool.finish_remote(slice_job.credential_id)
        if self.credential_pool is not None:
            self.credential_pool.record_error(slice_job.credential_id, exc)
        can_retry = exc.retryable and slice_job.attempt_count < self.max_attempts
        updates["status"] = "retryable_failure" if can_retry else "permanent_failure"
        self.ledger.update_slice(slice_job.id, **updates)

    def _repair_local_artifacts(self, job: DocumentJob) -> None:
        source = Path(job.source_path)
        capabilities = self.provider.capabilities()
        for item in self.ledger.list_slice_jobs(job.id):
            slice_path = Path(item.slice_path)
            valid_slice = bool(
                slice_path.is_file()
                and slice_path.stat().st_size == item.size_bytes
                and sha256_file(slice_path) == item.slice_sha256
            )
            if not valid_slice:
                if slice_path.resolve() == source.resolve():
                    raise RuntimeError("source PDF changed while repairing job")
                slice_path.unlink(missing_ok=True)
                regenerated = self.slicer.create_slices(
                    source,
                    [SliceRange(item.page_start, item.page_end, item.size_bytes)],
                    slice_path.parent,
                    max_bytes_per_file=capabilities.max_bytes_per_file,
                )
                if len(regenerated) != 1:
                    raise RuntimeError(
                        "provider byte capability changed; prepare a new parser job"
                    )
                descriptor = regenerated[0]
                self.ledger.update_slice(
                    item.id,
                    slice_path=str(descriptor.path.resolve()),
                    slice_sha256=descriptor.sha256,
                    size_bytes=descriptor.size_bytes,
                )
            if item.status == "completed":
                result_path = Path(item.result_path or "")
                valid_result = bool(
                    item.result_path
                    and result_path.is_file()
                    and item.result_sha256
                    and sha256_file(result_path) == item.result_sha256
                )
                if not valid_result:
                    self.ledger.update_slice(
                        item.id,
                        status="submitted" if item.remote_task_id else "queued",
                        result_path=None,
                        result_sha256=None,
                        last_error="normalized result was missing or corrupt",
                    )

    def _parser_request(self, job: DocumentJob, slice_job: SliceJob) -> ParserRequest:
        return ParserRequest(
            source_path=Path(slice_job.slice_path),
            source_sha256=slice_job.slice_sha256,
            document_id=(
                f"{job.document_id}-p{slice_job.page_start:06d}-"
                f"{slice_job.page_end:06d}"
            ),
            page_start=1,
            page_end=slice_job.page_count,
            global_page_offset=slice_job.global_page_offset,
            output_dir=self._job_dir(job.id) / "provider" / slice_job.id,
            model=job.parser_model,
            options={},
        )

    def _job_dir(self, job_id: str) -> Path:
        return self.work_dir / job_id

    def _merged_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "merged.pages.ndjson"
