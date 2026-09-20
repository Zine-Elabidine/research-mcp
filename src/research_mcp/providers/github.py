"""GitHub — what engineers build for themselves before anyone productises it.

The idea-search value here is not the code. It is that a developer scratching
their own itch, publicly, is a workaround artifact: someone had a problem bad
enough to spend a weekend on it, and star count is other people saying "me
too". A repo created recently that gained stars fast is a need that existing
products are not meeting.

Free and unauthenticated at 10 search requests/minute. Set GITHUB_TOKEN to
raise it to 30/min -- worth doing for any real sweep, not needed to start.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from .base import STRUCTURED, Provider, ProviderError, Result

API = "https://api.github.com"


class GitHub(Provider):
    name = "github"
    source_class = STRUCTURED

    def __init__(self) -> None:
        self._token = os.environ.get("GITHUB_TOKEN")

    def available(self) -> bool:
        return True  # works unauthenticated, just slower

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "research-mcp/0.1", "Accept": "application/vnd.github+json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,   # repo CREATED after this date
        until: str | None = None,
        min_stars: int | None = None,
    ) -> list[Result]:
        q = query
        if since:
            q += f" created:>{since}"
        if until:
            q += f" created:<{until}"
        if min_stars:
            q += f" stars:>={min_stars}"

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                r = await client.get(
                    f"{API}/search/repositories",
                    params={"q": q, "sort": "stars", "order": "desc",
                            "per_page": min(limit, 100)},
                    headers=self._headers(),
                )
                if r.status_code == 403 and "rate limit" in r.text.lower():
                    raise ProviderError(
                        "github rate limit (10/min unauthenticated) — set "
                        "GITHUB_TOKEN for 30/min"
                    )
                r.raise_for_status()
                items = r.json().get("items", [])
            except httpx.HTTPError as e:
                raise ProviderError(f"github: {e}") from e

        out: list[Result] = []
        for it in items:
            created = it.get("created_at")
            dt = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
            # Stars per month since creation: a 200-star repo from 2019 is
            # history, a 200-star repo from last month is a live need.
            velocity = None
            if dt and it.get("stargazers_count"):
                months = max((datetime.now(timezone.utc) - dt).days / 30.0, 0.5)
                velocity = round(it["stargazers_count"] / months, 1)
            out.append(
                Result(
                    source=self.name,
                    source_class=self.source_class,
                    title=it.get("full_name", ""),
                    url=it.get("html_url", ""),
                    text=it.get("description") or "",
                    author=(it.get("owner") or {}).get("login"),
                    published_at=dt,
                    score=it.get("stargazers_count"),
                    query=query,
                    raw={"stars_per_month": velocity,
                         "language": it.get("language"),
                         "forks": it.get("forks_count"),
                         "topics": it.get("topics") or []},
                )
            )
        return out
