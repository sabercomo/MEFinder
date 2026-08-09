"""Persistent, provider-neutral large-document parsing application layer."""

from .engine import LargeDocumentJobEngine
from .credential_pool import CredentialPool, CredentialPoolUnavailable
from .job_ledger import DocumentJob, JobLedger, SliceJob
from .mineru_accounts import MinerUAccountService, MinerUAccountSummary
from .merge import CoverageValidationError, validate_slice_coverage
from .slicing import PhysicalPDFSlicer, SliceDescriptor, SlicePlanner, SliceRange

__all__ = [
    "CoverageValidationError",
    "CredentialPool",
    "CredentialPoolUnavailable",
    "DocumentJob",
    "JobLedger",
    "LargeDocumentJobEngine",
    "MinerUAccountService",
    "MinerUAccountSummary",
    "PhysicalPDFSlicer",
    "SliceDescriptor",
    "SliceJob",
    "SlicePlanner",
    "SliceRange",
    "validate_slice_coverage",
]
