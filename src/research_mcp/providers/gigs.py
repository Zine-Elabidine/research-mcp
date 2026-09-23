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

import asyncio
import math
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
        terms = _terms(query)
        # Freelancer ORs the words of `query` and does not sort by relevance.
        # "invoice" alone matches 33 active projects, nearly all about
        # invoices; "invoice automation for small businesses" matches 4,612,
        # and the first 50 were logo rebuilds and Amazon SEO. So search each
        # content word on its own and merge. Each response's total_count also
        # says how specific that word is, which weights the local match below.
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True,
            headers={"User-Agent": "research-mcp/0.1"},
        ) as client:
            async def get(q: str | None, n: int) -> dict:
                params = {"limit": n, "job_details": "true", "full_description": "true"}
                if q:
                    params["query"] = q
                r = await client.get(API, params=params)
                r.raise_for_status()
                return r.json().get("result", {})

            try:
                pages = await asyncio.gather(
                    get(None, 1),  # total active projects, the IDF denominator
                    *(get(t, 50) for t in terms or [query]),
                )
            except httpx.HTTPError as e:
                raise ProviderError(f"gigs/freelancer: {e}") from e

        total = pages[0].get("total_count") or 1
        weights = {t: math.log((total + 1) / ((pg.get("total_count") or 0) + 1))
                   for t, pg in zip(terms, pages[1:])}
        projects = list({p["id"]: p for pg in pages[1:]
                         for p in pg.get("projects", [])}.values())

        # Rank locally on title+description+skills. A gig must carry enough of
        # the question's weight to stay: one rare word ("reconcile", 22 of
        # 5,670 projects) is enough, common ones ("every", "product") are not,
        # however many of them match.
        need = min(_ENOUGH, 0.5 * sum(weights.values()))
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

            title = (p.get("title") or "").lower()
            hay = " ".join([p.get("description") or "", " ".join(skills)]).lower()
            rel = _relevance(title, hay, weights)
            if weights and rel < need:
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
                         "skills": skills[:6], "bids": bids, "_rel": round(rel, 2)},
                )
            )
        # Best match first, then most bids -- bids being the demand proxy, not
        # a popularity score.
        out.sort(key=lambda r: (-(r.raw.get("_rel") or 0), -(r.score or 0)))
        return out


# log(5670/38): a word found in under ~0.7% of active projects carries a gig
# on its own.
_ENOUGH = 5.0
_MAX_TERMS = 6
_STOP = {"the", "and", "for", "with", "are", "was", "how", "why", "who", "what",
         "that", "this", "from", "into", "their", "they", "them", "want",
         "need", "needs", "does", "doing", "have", "has", "can", "any", "all",
         "our", "your", "you"}


def _terms(query: str) -> list[str]:
    """Content words of the query, capped. Longer words first when capping --
    a rough stand-in for specificity before total_count is known."""
    import re
    terms = list(dict.fromkeys(
        t for t in re.findall(r"[a-z0-9]{3,}", query.lower()) if t not in _STOP))
    if len(terms) > _MAX_TERMS:
        keep = set(sorted(terms, key=len, reverse=True)[:_MAX_TERMS])
        terms = [t for t in terms if t in keep]
    return terms


def _relevance(title: str, body: str, weights: dict[str, float]) -> float:
    """Sum of the weights of the query words found; a title hit counts double,
    since the title is what the job IS. Words are matched on a crude stem so
    "reconcile" finds "reconciliation" and "invoice" finds "invoicing"."""
    import re
    score = 0.0
    for t, w in weights.items():
        stem = t[:max(5, len(t) - 3)]
        pat = rf"\b{re.escape(stem)}\w*"
        if re.search(pat, title):
            score += 2 * w
        elif re.search(pat, body):
            score += w
    return score


def _out_of_range(dt: datetime, since: str | None, until: str | None) -> bool:
    if since and dt < datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc):
        return True
    if until and dt > datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc):
        return True
    return False
