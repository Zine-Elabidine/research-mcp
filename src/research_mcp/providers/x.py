"""X (Twitter) via a third-party archive provider.

X is unreachable through search indexes -- login wall plus crawler blocking
make any domain-scoped web search thin and stale. The official API discontinued
its free tier, charges $0.005/post read, and caps normal tiers at a 7-day search
window; full archive is Enterprise at $42K+/mo.

twitterapi.io's /twitter/tweet/advanced_search takes the complete X
advanced-search syntax (from:, since:, until:), reaches back 7+ years, and
costs $0.00015/tweet ($0.15 per 1,000) with no minimum. Equivalents: GetXAPI
$0.05/1k, TwitterAPIs $0.04/1k, Sorsa from $0.02/1k.

Risk here is provider durability, not price -- SocialData.tools, same category,
was shut down. Hence: this stays behind the Provider interface so swapping is a
config change, and every result is written to the corpus so a shutdown costs
future queries rather than accumulated history. Legally split: hiQ v. LinkedIn
means scraping public data is not a CFAA violation, but it does breach X's ToS.
Fine for a personal research tool; revisit entirely if this ever ships to users.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from .base import SOCIAL, Provider, ProviderError, Result

BASE = os.environ.get("X_API_BASE", "https://api.twitterapi.io")


class X(Provider):
    name = "x"
    source_class = SOCIAL

    def __init__(self) -> None:
        self._key = os.environ.get("X_API_KEY")

    def available(self) -> bool:
        return bool(self._key)

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,
        until: str | None = None,
        min_faves: int | None = None,
        lang: str | None = None,
    ) -> list[Result]:
        # Compose X advanced-search syntax rather than exposing raw params.
        q = query
        if since:
            q += f" since:{since}"
        if until:
            q += f" until:{until}"
        if min_faves:
            q += f" min_faves:{min_faves}"
        if lang:
            q += f" lang:{lang}"

        # Billing is per PAGE, not per result: a call returns ~20 tweets and
        # is charged for ~20 regardless of how many we asked for. Truncating a
        # page mid-way throws away data already paid for -- and from the one
        # source most likely to vanish, where the corpus is the whole hedge.
        # So: fetch whole pages, keep every row, let the caller trim what the
        # model sees. Observed rate 2026-09-19: ~300 credits / 20 tweets.
        out: list[Result] = []
        cursor = ""
        async with httpx.AsyncClient(timeout=30) as client:
            while len(out) < limit:
                params = {"query": q, "queryType": "Top"}
                if cursor:
                    params["cursor"] = cursor
                try:
                    r = await client.get(
                        f"{BASE}/twitter/tweet/advanced_search",
                        params=params,
                        headers={"X-API-Key": self._key or ""},
                    )
                    r.raise_for_status()
                    d = r.json()
                except httpx.HTTPError as e:
                    raise ProviderError(f"x: {e}") from e

                tweets = d.get("tweets") or d.get("data") or []
                if not tweets:
                    break
                for t in tweets:
                    author = (t.get("author") or {})
                    handle = author.get("userName") or author.get("screen_name")
                    tid = t.get("id") or t.get("id_str")
                    out.append(
                        Result(
                            source=self.name,
                            source_class=self.source_class,
                            title=f"@{handle}" if handle else "tweet",
                            url=t.get("url") or (f"https://x.com/{handle}/status/{tid}" if handle and tid else ""),
                            text=t.get("text") or t.get("full_text") or "",
                            author=handle,
                            published_at=_parse(t.get("createdAt") or t.get("created_at")),
                            score=t.get("likeCount") or t.get("favorite_count"),
                            comments=t.get("replyCount"),
                            query=query,
                            raw={"retweets": t.get("retweetCount"), "views": t.get("viewCount")},
                        )
                    )
                if not d.get("has_next_page"):
                    break
                cursor = d.get("next_cursor") or ""
                if not cursor:
                    break
        return out


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
