"""Hacker News via the Algolia search API.

Free, no authentication, indexes the full HN history in near-real-time, and
supports date ranges and point thresholds. Zero friction -- this is the
provider that works before any key or approval exists, which is why it gets
built first.

Covers three distinct jobs: tech trend signal, launch reception (Show HN and
the comments under it), and hiring demand via the monthly "Who is hiring"
threads.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .base import COMMUNITY, Provider, ProviderError, Result

API = "https://hn.algolia.com/api/v1"


def _ts(date: str | None) -> int | None:
    if not date:
        return None
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


class HackerNews(Provider):
    name = "hn"
    source_class = COMMUNITY

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def available(self) -> bool:
        return True  # no credentials, ever

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,
        until: str | None = None,
        min_points: int | None = None,
        kind: str = "story",       # "story" | "comment" | "all"
        sort: str = "relevance",   # "relevance" | "date"
    ) -> list[Result]:
        tags = {"story": "story", "comment": "comment", "all": "(story,comment)"}.get(kind, "story")

        numeric = []
        if (a := _ts(since)) is not None:
            numeric.append(f"created_at_i>{a}")
        if (b := _ts(until)) is not None:
            numeric.append(f"created_at_i<{b}")
        if min_points is not None:
            numeric.append(f"points>={min_points}")

        params = {
            "query": query,
            "tags": tags,
            "hitsPerPage": min(limit, 100),
        }
        if numeric:
            params["numericFilters"] = ",".join(numeric)

        endpoint = "search_by_date" if sort == "date" else "search"

        client = self._client or httpx.AsyncClient(timeout=20)
        try:
            r = await client.get(f"{API}/{endpoint}", params=params)
            r.raise_for_status()
            hits = r.json().get("hits", [])
        except httpx.HTTPError as e:
            raise ProviderError(f"hn: {e}") from e
        finally:
            if self._client is None:
                await client.aclose()

        out: list[Result] = []
        for h in hits:
            # Comments carry no title of their own; inherit the story's.
            title = h.get("title") or h.get("story_title") or "(comment)"
            body = h.get("comment_text") or h.get("story_text") or ""
            url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
            created = h.get("created_at")
            out.append(
                Result(
                    source=self.name,
                    source_class=self.source_class,
                    title=title,
                    url=url,
                    text=_strip(body),
                    author=h.get("author"),
                    published_at=datetime.fromisoformat(created) if created else None,
                    score=h.get("points"),
                    comments=h.get("num_comments"),
                    query=query,
                    raw=h,
                )
            )
        return out


def _strip(html: str) -> str:
    """HN comment bodies come back as light HTML."""
    if not html:
        return ""
    import html as _h
    import re

    t = re.sub(r"<p>", "\n", html)
    t = re.sub(r"<[^>]+>", "", t)
    return _h.unescape(t).strip()
