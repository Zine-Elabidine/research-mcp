"""Tavily -- the consensus/narrative layer.

1,000 credits/month free, no card. Returns clean source-backed snippets rather
than raw HTML, and supports include_domains/exclude_domains, topic
(general|news) and date ranges.

Treated deliberately as ONE voice among several, never the anchor. Advanced
depth costs more than one credit per call, so a fanned-out pass burns the free
tier faster than the headline number suggests -- roughly 20-30 real research
tasks a month. Keep depth "basic" unless the question warrants otherwise.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from .base import CONSENSUS, Provider, ProviderError, Result

URL = "https://api.tavily.com/search"


class Tavily(Provider):
    name = "tavily"
    source_class = CONSENSUS

    def __init__(self) -> None:
        self._key = os.environ.get("TAVILY_API_KEY")

    def available(self) -> bool:
        return bool(self._key)

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,
        until: str | None = None,
        domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        depth: str = "basic",     # "basic" (1 credit) | "advanced" (more)
        topic: str = "general",   # "general" | "news"
    ) -> list[Result]:
        payload: dict[str, object] = {
            "api_key": self._key,
            "query": query,
            "search_depth": depth,
            "topic": topic,
            "max_results": min(limit, 20),
        }
        if domains:
            payload["include_domains"] = domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains
        if since:
            payload["start_date"] = since
        if until:
            payload["end_date"] = until

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                r = await client.post(URL, json=payload)
                r.raise_for_status()
                d = r.json()
            except httpx.HTTPError as e:
                raise ProviderError(f"tavily: {e}") from e

        # Tavily SILENTLY DROPS include_domains when a query matches nothing on
        # the requested domain, returning unrelated off-domain results with no
        # flag. Observed 2026-09-19: "runna app injury complaints" honoured
        # ["reddit.com"]; the same question phrased longer came back with
        # youtube.com and a Maroon 5 video. A constraint that silently stops
        # applying is worse than one that errors -- the list still looks
        # plausible. Enforce it here and report what was discarded.
        dropped = 0
        out: list[Result] = []
        for item in d.get("results", []):
            if domains and not _host_matches(item.get("url", ""), domains):
                dropped += 1
                continue
            out.append(
                Result(
                    source=self.name,
                    source_class=self.source_class,
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    text=item.get("content", ""),
                    published_at=_parse(item.get("published_date")),
                    query=query,
                    raw={"relevance": item.get("score")},
                )
            )
        if dropped:
            # Surfaced through the provider stats rather than swallowed, so a
            # thin result set is legible instead of mysterious.
            raise _PartialResults(out, dropped, domains or [])
        return out


class _PartialResults(Exception):
    """Carries results plus the count silently dropped by a server-side filter
    that did not apply. The router unwraps this -- it is signal, not failure."""

    def __init__(self, results: list[Result], dropped: int, domains: list[str]) -> None:
        self.results = results
        self.dropped = dropped
        self.domains = domains
        super().__init__(
            f"{dropped} result(s) ignored include_domains={domains} and were discarded"
        )


def _host_matches(url: str, domains: list[str]) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return any(host == d.lower().removeprefix("www.") or host.endswith("." + d.lower().removeprefix("www."))
               for d in domains)


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
