"""The single HTTP route registry for the local API.

Domain assembly functions (``http_routes``) keep returning plain ``{path:
handler}`` maps; a handler that needs transport policy is wrapped with
:func:`route` at its declaration so the policy lives next to the path.  The
HTTP composition root merges every map into one :class:`RouteTable`, and the
transport (``web_http``) only asks that table how to read and dispatch a
request.  Sets such as "raw-body POST paths" or "data-root-mutating POST
paths" are derived from the declarations instead of being maintained by hand.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

BodyKind = Literal["json", "raw"]
Method = Literal["GET", "POST"]
RouteMap = dict[str, Callable[..., tuple[int, object]]]
RoutePair = tuple[RouteMap, RouteMap]


@dataclass(frozen=True)
class RoutePolicy:
    """Transport policy attached to one handler at its declaration."""

    body: BodyKind = "json"
    mutates_data_root: bool = False
    max_body_bytes: int | None = None
    oversize_error: str = ""
    drain_oversize: bool = False
    keep_blank_query: bool = False


class _DeclaredHandler:
    """A handler plus its policy; still callable like the bare handler."""

    __slots__ = ("handler", "policy")

    def __init__(self, handler: Callable[..., tuple[int, object]], policy: RoutePolicy):
        self.handler = handler
        self.policy = policy

    def __call__(self, *args, **kwargs):
        return self.handler(*args, **kwargs)


def route(handler: Callable[..., tuple[int, object]], **policy) -> _DeclaredHandler:
    """Declare transport policy for ``handler`` where its path is declared."""

    return _DeclaredHandler(handler, RoutePolicy(**policy))


@dataclass(frozen=True)
class Route:
    method: Method
    path: str
    handler: Callable[..., tuple[int, object]]
    policy: RoutePolicy = RoutePolicy()

    @property
    def body(self) -> BodyKind:
        return self.policy.body

    @property
    def mutates_data_root(self) -> bool:
        return self.policy.mutates_data_root


def _unwrap(handler) -> tuple[Callable[..., tuple[int, object]], RoutePolicy]:
    if isinstance(handler, _DeclaredHandler):
        return handler.handler, handler.policy
    return handler, RoutePolicy()


def _no_params(handler: Callable[[], tuple[int, object]]):
    return lambda _params: handler()


class RouteTable:
    """Immutable ``(method, path) -> Route`` lookup with derived policy sets."""

    def __init__(self, routes: Iterable[Route]) -> None:
        table: dict[tuple[str, str], Route] = {}
        for item in routes:
            key = (item.method, item.path)
            if key in table:
                raise ValueError(f"duplicate HTTP route: {item.method} {item.path}")
            if item.method == "GET" and item.policy.body != "json":
                raise ValueError(f"GET route cannot declare a body: {item.path}")
            table[key] = item
        self._routes = table

    @classmethod
    def from_maps(
        cls,
        *,
        get_maps: Iterable[Mapping[str, object]] = (),
        parameterless_get_maps: Iterable[Mapping[str, object]] = (),
        post_maps: Iterable[Mapping[str, object]] = (),
    ) -> "RouteTable":
        """Merge assembly maps; parameterless GET handlers take no query."""

        routes: list[Route] = []
        for maps, method, adapt in (
            (get_maps, "GET", None),
            (parameterless_get_maps, "GET", _no_params),
            (post_maps, "POST", None),
        ):
            for mapping in maps:
                for path, declared in mapping.items():
                    handler, policy = _unwrap(declared)
                    if adapt is not None:
                        handler = adapt(handler)
                    routes.append(Route(method, path, handler, policy))
        return cls(routes)

    def get(self, method: str, path: str) -> Route | None:
        return self._routes.get((method, path))

    def paths(self, method: str) -> frozenset[str]:
        return frozenset(path for (m, path) in self._routes if m == method)

    @property
    def raw_body_post_paths(self) -> frozenset[str]:
        return frozenset(
            r.path for r in self._routes.values() if r.method == "POST" and r.body == "raw"
        )

    @property
    def data_root_mutating_post_paths(self) -> frozenset[str]:
        return frozenset(
            r.path
            for r in self._routes.values()
            if r.method == "POST" and r.mutates_data_root
        )
