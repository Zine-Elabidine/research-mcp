"""Common contract every source provider implements.

The whole point of this layer: Claude never learns provider names. It asks for
a *kind* of information, the router picks providers, and any provider can be
swapped out via config without changing the tool surface. Matters concretely --
twitterapi.io can be shut down (SocialData.tools was), Tavily's free tier can
change, Reddit can reject the OAuth app.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

# What kind of truth a source carries. Cross-checking works across these:
# a claim living only in CONSENSUS with nothing in STRUCTURED is suspect.
CONSENSUS = "consensus"     # what the web says      -- Tavily, Exa, Brave
COMMUNITY = "community"     # what users say         -- Reddit, HN
SOCIAL = "social"           # what is hot right now  -- X
STRUCTURED = "structured"   # the actual numbers     -- GitHub, ARR, app stores
ACADEMIC = "academic"       # state of the art       -- arXiv


@dataclass
class Result:
    source: str                          # provider name, e.g. "hn"
    source_class: str                    # one of the constants above
    title: str
    url: str
    text: str = ""                       # body / snippet / comment
    author: str | None = None
    published_at: datetime | None = None
    score: int | None = None             # upvotes, likes -- engagement, not relevance
    comments: int | None = None
    query: str = ""                      # the query that surfaced this
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def fingerprint(self) -> str:
        """Dedup key. URL when we have one, else source+title+text.

        Same story surfaces from HN and Tavily with different tracking params,
        so normalise before hashing.
        """
        if self.url:
            basis = self.url.split("?")[0].rstrip("/").lower()
        else:
            basis = f"{self.source}:{self.title}:{self.text[:200]}".lower()
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw")
        d["published_at"] = self.published_at.isoformat() if self.published_at else None
        d["fingerprint"] = self.fingerprint
        return d

    def to_model(self, max_text: int = 600) -> dict[str, Any]:
        """Trimmed shape handed back to Claude. Keep it lean -- tokens are the
        real cost of a search tool, not API credits."""
        out: dict[str, Any] = {
            "source": self.source,
            "class": self.source_class,
            "title": self.title,
            "url": self.url,
        }
        if self.text:
            t = " ".join(self.text.split())
            out["text"] = t[:max_text] + ("..." if len(t) > max_text else "")
        if self.author:
            out["author"] = self.author
        if self.published_at:
            out["date"] = self.published_at.strftime("%Y-%m-%d")
        if self.score is not None:
            out["score"] = self.score
        if self.comments is not None:
            out["comments"] = self.comments
        return out


class ProviderError(RuntimeError):
    """A provider failed. The router degrades gracefully rather than failing
    the whole call -- one dead source must never take down a research pass."""


@runtime_checkable
class Provider(Protocol):
    name: str
    source_class: str

    def available(self) -> bool:
        """False when credentials are missing. Router skips instead of erroring,
        so the tool stays usable before every key is set up."""
        ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,   # YYYY-MM-DD
        until: str | None = None,
    ) -> list[Result]:
        ...
