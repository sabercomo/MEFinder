"""Persistent, provider-neutral large-document parsing application layer."""

from .engine import LargeDocumentJobEngine
from .credential_pool import CredentialPool, CredentialPoolUnavailable
from .job_ledger import CredentialPageAttribution, DocumentJob, JobLedger, SliceJob
from .mineru_accounts import (
    MinerUAccountService,
    MinerUAccountSummary,
    MinerUBookUsage,
    MinerUCredentialUsageStatistics,
    MinerUUsageStatistics,
)
from .merge import CoverageValidationError, validate_slice_coverage
from .slicing import PhysicalPDFSlicer, SliceDescriptor, SlicePlanner, SliceRange

__all__ = [
    "CoverageValidationError",
    "CredentialPool",
    "CredentialPoolUnavailable",
    "CredentialPageAttribution",
    "DocumentJob",
    "JobLedger",
    "LargeDocumentJobEngine",
    "MinerUAccountService",
    "MinerUAccountSummary",
    "MinerUBookUsage",
    "MinerUCredentialUsageStatistics",
    "MinerUUsageStatistics",
    "PhysicalPDFSlicer",
    "SliceDescriptor",
    "SliceJob",
    "SlicePlanner",
    "SliceRange",
    "validate_slice_coverage",
]
