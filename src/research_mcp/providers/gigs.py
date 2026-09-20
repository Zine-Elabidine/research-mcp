"""Freelance gigs — the strongest "money is already moving" signal.

The same job posted over and over on a freelance marketplace is a product
waiting to exist: somebody is paying a human, repeatedly, to do something
manual. Unlike a forum post, a gig carries a budget and a bid count, so it
answers both "do they pay" and "how much" without asking anyone.

Upwork is closed -- 403 on the RSS feed, the search API and the public page
(checked 2026-09-20), consistent with every other platform this month.
Freelancer.com still serves an open, unauthenticated project search, so that
is what this uses. RemoteOK and We Work Remotely are also open if a
rote-work-hiring signal is wanted later.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .base import STRUCTURED, Provider, ProviderError, Result

API = "https://www.freelancer.com/api/projects/0.1/projects/active/"


class Gigs(Provider):
    name = "gigs"
    source_class = STRUCTURED

    def available(self) -> bool:
        return True  # no key

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,
        until: str | None = None,
        min_bids: int | None = None,
    ) -> list[Result]:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True,
            headers={"User-Agent": "research-mcp/0.1"},
        ) as client:
            try:
                r = await client.get(API, params={
                    "query": query, "limit": min(limit, 50),
                    "job_details": "true", "full_description": "true",
                })
                r.raise_for_status()
                projects = r.json().get("result", {}).get("projects", [])
            except httpx.HTTPError as e:
                raise ProviderError(f"gigs/freelancer: {e}") from e

        # Freelancer's `query` is loose -- a search for "invoice automation"
        # returned "Campus Relocation & Enrollment Support". Rank locally on
        # title+description+skills and drop the non-matches, same as Reddit.
        out: list[Result] = []
        for p in projects:
            ts = p.get("time_submitted") or p.get("submitdate")
            dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            if dt and _out_of_range(dt, since, until):
                continue
            bids = (p.get("bid_stats") or {}).get("bid_count")
            if min_bids is not None and (bids or 0) < min_bids:
                continue

            b = p.get("budget") or {}
            cur = (p.get("currency") or {}).get("code", "")
            budget = (f"{b.get('minimum')}-{b.get('maximum')} {cur}"
                      if b.get("minimum") is not None else "")
            skills = [j.get("name") for j in (p.get("jobs") or []) if j.get("name")]

            hay = " ".join([
                p.get("title", ""), p.get("description") or "",
                " ".join(skills),
            ]).lower()
            rel = _relevance(hay, query)
            if rel == 0:
                continue

            out.append(
                Result(
                    source=self.name,
                    source_class=self.source_class,
                    title=p.get("title", ""),
                    url=f"https://www.freelancer.com/projects/{p.get('seo_url','')}",
                    text=p.get("description") or p.get("preview_description") or "",
                    published_at=dt,
                    # Bid count is the demand proxy: many bidders on a boring
                    # repeated job means proven willingness to pay AND that
                    # freelancers find it worth their time.
                    score=bids,
                    query=query,
                    raw={"budget": budget, "pricing": p.get("type"),
                         "skills": skills[:6], "bids": bids, "_rel": rel},
                )
            )
        # Most query terms matched first, then most bids -- bids being the
        # demand proxy, not a popularity score.
        out.sort(key=lambda r: (-(r.raw.get("_rel") or 0), -(r.score or 0)))
        return out


def _relevance(hay: str, query: str) -> int:
    import re
    terms = [t for t in re.findall(r"[a-z0-9]{3,}", query.lower())
             if t not in {"the", "and", "for", "with"}]
    if not terms:
        return 1
    return sum(bool(re.search(rf"\b{re.escape(t)}\w{{0,2}}\b", hay)) for t in terms)


def _out_of_range(dt: datetime, since: str | None, until: str | None) -> bool:
    if since and dt < datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc):
        return True
    if until and dt > datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc):
        return True
    return False
