"""Fan-out, merge, dedup.

This is the layer the whole tool exists for. Providers are commodities; the
value is in asking several source *classes* the same question and letting the
disagreements show. The failure this was built to prevent: 2026-09-19, a single
web-search pass returned SEO content farms all repeating "the AI wrapper era is
over", while one structured data point (Cal AI, $40M ARR, 7 people) said the
opposite. Consensus is not corroboration.

Three behaviours matter here:
  - concurrency: every provider queried at once, one slow source never serialises
  - graceful degradation: a dead provider is recorded and skipped, never fatal
  - interleaving: results round-robin across providers so no single source owns
    the top of the list, which is exactly how single-source bias creeps back in
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Sequence

from .providers.base import Provider, ProviderError, Result


@dataclass
class Pass:
    """One research pass: what was asked, what answered, what came back."""
    question: str
    queries: list[str]
    results: list[Result]
    providers: dict[str, Any] = field(default_factory=dict)   # name -> count | "error: ..."
    skipped: dict[str, str] = field(default_factory=dict)     # name -> why

    def to_model(self, max_text: int = 600, max_shown: int | None = None) -> dict[str, Any]:
        """What Claude sees. Deliberately narrower than what the corpus keeps:
        paid-for rows are all stored, but tokens are the real running cost of a
        research tool, so the model gets the top slice."""
        shown = self.results[:max_shown] if max_shown else self.results
        items = [r.to_model(max_text) for r in shown]
        for item, r in zip(items, shown):
            if extra := getattr(r, "_also_in", None):
                item["corroborated_by"] = sorted(extra)
        out: dict[str, Any] = {
            "question": self.question,
            "queries_issued": self.queries,
            "providers": self.providers,
            "count": len(items),
            "results": items,
        }
        if len(shown) < len(self.results):
            out["not_shown"] = len(self.results) - len(shown)
            out["note_storage"] = "all retrieved results are in the corpus"
        if self.skipped:
            out["skipped"] = self.skipped
        return out


async def _one(provider: Provider, query: str, limit: int, since: str | None,
               until: str | None, extra: dict[str, Any]) -> tuple[str, list[Result] | Exception]:
    try:
        kwargs = {k: v for k, v in extra.items() if k in _accepted(provider)}
        res = await provider.search(query, limit=limit, since=since, until=until, **kwargs)
        return provider.name, res
    except _partial() as pr:
        # A provider enforcing a constraint the upstream API ignored. The
        # surviving results are good; the drop count is worth reporting.
        return provider.name, pr
    except (ProviderError, Exception) as e:   # noqa: BLE001 - nothing may escape
        return provider.name, e


def _partial():
    """Exception types that carry usable results alongside a caveat."""
    from .providers.reddit import _Truncated
    from .providers.tavily import _PartialResults
    return (_PartialResults, _Truncated)


def _accepted(provider: Provider) -> set[str]:
    """Provider-specific kwargs (min_points, subreddits, ...) are opt-in; pass
    only what a given provider's signature actually declares."""
    import inspect
    try:
        return set(inspect.signature(provider.search).parameters)
    except (TypeError, ValueError):
        return set()


async def fan_out(
    providers: Sequence[Provider],
    question: str,
    *,
    queries: Sequence[str] | None = None,
    limit_per: int = 20,
    since: str | None = None,
    until: str | None = None,
    **extra: Any,
) -> Pass:
    """Run every available provider over every query, concurrently."""
    qs = list(queries) if queries else [question]

    live = [p for p in providers if p.available()]
    skipped = {p.name: "not configured (missing credentials)"
               for p in providers if not p.available()}

    jobs = [_one(p, q, limit_per, since, until, extra) for p in live for q in qs]
    raw = await asyncio.gather(*jobs) if jobs else []

    collected: dict[str, list[Result]] = {p.name: [] for p in live}
    stats: dict[str, Any] = {}
    for name, outcome in raw:
        if isinstance(outcome, _partial()):
            collected[name].extend(outcome.results)
            prev = stats.get(name)
            base = prev.get("kept", 0) if isinstance(prev, dict) else (prev or 0)
            entry: dict[str, Any] = {"kept": base + len(outcome.results)}
            if hasattr(outcome, "dropped"):
                entry["discarded_ignored_filter"] = outcome.dropped
                entry["note"] = f"upstream ignored include_domains={outcome.domains}; enforced locally"
            else:
                entry["not_searched"] = [f"r/{s} {k}" for s, k in outcome.skipped]
                entry["note"] = "request budget reached; coverage is partial"
            stats[name] = entry
            continue
        if isinstance(outcome, Exception):
            # One source failing must not fail the pass -- record and move on.
            stats[name] = f"error: {type(outcome).__name__}: {outcome}"
            continue
        collected[name].extend(outcome)
        stats[name] = stats.get(name, 0) + len(outcome)

    merged = _interleave(collected)
    return Pass(question=question, queries=qs, results=merged,
                providers=stats, skipped=skipped)


def _title_key(title: str) -> str:
    """Normalised title for near-duplicate detection. Empty for titles too
    short or generic to be a safe signal (X results titled '@handle')."""
    import re
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    t = " ".join(t.split())
    return t[:80] if len(t) >= 20 else ""


def _interleave(by_provider: dict[str, list[Result]]) -> list[Result]:
    """Round-robin across providers, deduping as we go.

    Round-robin rather than score-sorting on purpose: engagement scores are not
    comparable across platforms (an HN 500 and a Reddit 500 mean different
    things), and sorting by any one of them hands the top of the list to
    whichever source is chattiest.
    """
    # Dedup within each provider first, preserving its own ordering.
    # Two keys, because URL alone is not enough: syndicated articles reappear
    # under different URLs with an identical title, and a web provider queried
    # with several phrasings will happily return the same piece twice.
    for name, items in by_provider.items():
        keep, seen_fp, seen_title = [], set(), set()
        for r in items:
            tkey = _title_key(r.title)
            if r.fingerprint in seen_fp or (tkey and tkey in seen_title):
                continue
            seen_fp.add(r.fingerprint)
            if tkey:
                seen_title.add(tkey)
            keep.append(r)
        by_provider[name] = keep

    out: list[Result] = []
    index: dict[str, Result] = {}
    queues = [list(v) for v in by_provider.values() if v]
    while queues:
        for q in list(queues):
            if not q:
                queues.remove(q)
                continue
            r = q.pop(0)
            fp = r.fingerprint
            if (prev := index.get(fp)) is not None:
                # Same item from a second source == cross-source corroboration.
                also = getattr(prev, "_also_in", set())
                also.add(r.source)
                prev._also_in = also  # type: ignore[attr-defined]
                continue
            index[fp] = r
            out.append(r)
    return out
