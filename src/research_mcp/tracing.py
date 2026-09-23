"""Langfuse tracing, off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set.

One trace per tool call, one child observation per provider query. The point is
the question the corpus cannot answer: which source was slow, which errored,
which came back empty, for THIS question -- a fan-out that quietly lost a
source class reads as thin evidence rather than as a failure.

Inputs and outputs are summaries (counts, top titles), never full result
bodies. The corpus already keeps every row; the trace only needs to explain
the pass.
"""

from __future__ import annotations

import functools
import os
from contextlib import contextmanager
from typing import Any, Iterator

_client = None


def enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _get():
    global _client
    if _client is None:
        from langfuse import get_client
        _client = get_client()  # reads LANGFUSE_* from the environment
    return _client


class _Noop:
    def update(self, **_: Any) -> None:
        pass


@contextmanager
def observe(name: str, as_type: str = "span", **kw: Any) -> Iterator[Any]:
    """Start an observation nested under whatever is current. A no-op object
    with the same `update()` when tracing is off, so call sites stay plain."""
    if not enabled():
        yield _Noop()
        return
    with _get().start_as_current_observation(name=name, as_type=as_type, **kw) as obs:
        yield obs


def summarise(results: list[Any], n: int = 5) -> dict[str, Any]:
    return {"count": len(results),
            "top": [getattr(r, "title", "")[:120] for r in results[:n]]}


def traced_tool(fn):
    """Wrap an MCP tool so each call is one trace. functools.wraps keeps the
    signature and annotations, which is what the MCP schema is built from."""
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        with observe(fn.__name__, as_type="tool",
                     input={k: v for k, v in kwargs.items() if v is not None}) as obs:
            out = await fn(*args, **kwargs)
            if isinstance(out, dict):
                obs.update(output={k: out[k] for k in
                                   ("queries_issued", "providers", "skipped",
                                    "count", "not_shown") if k in out})
            return out
    return wrapper
