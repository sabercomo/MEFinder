"""Persisted algorithm identities and readable version sets."""
from .bertalign_backend import BERTALIGN_ALGORITHM, BERTALIGN_ALGORITHM_VERSION

ALIGNMENT_ALGORITHM = "chapter-anchored-semantic-dp"
ALIGNMENT_ALGORITHM_VERSION = "22"
# v22 changes anchor selection, not stored span semantics. Existing v21 results
# remain readable; generation only reuses runs of the current version.
READABLE_ALIGNMENT_VERSIONS = frozenset({"21", ALIGNMENT_ALGORITHM_VERSION})
RESTORABLE_ALIGNMENT_VERSIONS = frozenset(
    {"16", "17", "18", "19", "20", "21", ALIGNMENT_ALGORITHM_VERSION}
)
# Per-backend readable versions: the optional Bertalign backend is a separate
# algorithm identity (its runs never mix with the default backend's), but its
# links/members use the same schema, so the reader route accepts it too.
_READABLE_BY_ALGORITHM = {
    ALIGNMENT_ALGORITHM: READABLE_ALIGNMENT_VERSIONS,
    BERTALIGN_ALGORITHM: frozenset({BERTALIGN_ALGORITHM_VERSION}),
}


def _route_run_is_readable(algorithm: object, version: object) -> bool:
    return str(version) in _READABLE_BY_ALGORITHM.get(str(algorithm), frozenset())


# Backend selection is request-scoped: consumers resolve runs for one backend,
# defaulting to the default (FastEmbed) backend so a newer Bertalign run never
# hijacks the default reader/overview. HTTP controllers resolve the saved
# backend preference when the request does not specify one.
DEFAULT_ALIGNMENT_BACKEND = "default"
_BACKEND_ALGORITHMS = {
    DEFAULT_ALIGNMENT_BACKEND: ALIGNMENT_ALGORITHM,
    "bertalign": BERTALIGN_ALGORITHM,
}


def backend_algorithm(backend: object) -> str:
    """Map a backend name to its stored ``algorithm`` string (default backend
    when unknown/empty)."""
    return _BACKEND_ALGORITHMS.get(
        str(backend or DEFAULT_ALIGNMENT_BACKEND), ALIGNMENT_ALGORITHM
    )
