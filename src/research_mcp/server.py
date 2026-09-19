"""MCP server exposing federated research to Claude.

Tools are shaped by INTENT, not by provider. Claude asks for a kind of
evidence; the router decides which sources answer it. Providers can be added,
swapped or lost without Claude relearning anything.

Tool descriptions are load-bearing -- they are the only thing the model selects
on -- so they say when to reach for each tool, not just what it wraps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

# Explicit path, not find_dotenv(): the server is launched by Claude Code with
# an arbitrary cwd, and find_dotenv() walks the call stack -- which fails
# outright under some entry points. Repo root is two levels up from this file.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from .corpus import Corpus                                   # noqa: E402
from .providers import HackerNews, Reddit, Tavily, X         # noqa: E402
from .router import fan_out                                  # noqa: E402

mcp = MCPServer(
    "research",
    instructions=(
        "Federated research across source classes. Rule of thumb: search_community "
        "for what people actually experience, search_web for what is published. "
        "Agreement across a single class is not corroboration -- cross a class "
        "boundary before treating a claim as established."
    ),
)
corpus = Corpus()

HN, REDDIT, XP, TAVILY = HackerNews(), Reddit(), X(), Tavily()
COMMUNITY_PROVIDERS = {"hn": HN, "reddit": REDDIT, "x": XP}


@mcp.tool()
async def search_community(
    question: str,
    platforms: list[str] | None = None,
    subreddits: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    min_points: int | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """Search what real people said, in their own words, across Hacker News,
    Reddit and X simultaneously.

    Use this for: complaints and pain points about a product or workflow, how
    practitioners actually do something, reception of a launch, hiring demand
    (HN "Who is hiring"), and whether a trend has real users behind it or only
    press coverage.

    Prefer this over search_web whenever the question is about lived experience
    rather than published claims. Web search returns the pages that RANK;
    this returns what was actually posted, comments included.

    Args:
        question: plain-language question or keywords.
        platforms: subset of ["hn", "reddit", "x"]. Default: all configured.
        subreddits: restrict Reddit to these, e.g. ["running", "hyrox"].
        since/until: "YYYY-MM-DD" bounds.
        min_points: HN score floor -- use ~50 to cut noise on broad topics.
        limit: results per provider.
    """
    chosen = [COMMUNITY_PROVIDERS[p] for p in (platforms or COMMUNITY_PROVIDERS) if p in COMMUNITY_PROVIDERS]
    res = await fan_out(
        chosen, question,
        limit_per=limit, since=since, until=until,
        subreddits=subreddits, min_points=min_points,
    )
    corpus.record(tool="search_community", question=question, queries=res.queries,
                  providers=res.providers, results=res.results)
    return res.to_model()


@mcp.tool()
async def search_web(
    question: str,
    queries: list[str] | None = None,
    domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    since: str | None = None,
    limit: int = 10,
    depth: str = "basic",
) -> dict[str, Any]:
    """Search the general web for published claims, articles and documentation.

    This is the CONSENSUS layer: it tells you what the web says, which is not
    the same as what is true. SEO content farms dominate commercial topics and
    repeat each other, so a unanimous answer here is weak evidence. Corroborate
    anything load-bearing with search_community or a structured source.

    Args:
        question: the research question.
        queries: explicit query variants to fan out over. Supply several when
            the question is broad -- one phrasing returns one slice of the web.
        domains / exclude_domains: restrict or suppress sources.
        since: "YYYY-MM-DD".
        depth: "basic" (1 credit) or "advanced" (costs more, use sparingly).
    """
    res = await fan_out(
        [TAVILY], question, queries=queries,
        limit_per=limit, since=since,
        domains=domains, exclude_domains=exclude_domains, depth=depth,
    )
    corpus.record(tool="search_web", question=question, queries=res.queries,
                  providers=res.providers, results=res.results)
    return res.to_model()


@mcp.tool()
async def corpus_stats() -> dict[str, Any]:
    """Report what the local research corpus has accumulated: how many results
    and searches are stored, the spread across source classes, and the date
    range covered. Use it to check whether a longitudinal question ("what
    changed since March") has enough history behind it to be answerable."""
    return corpus.stats()


@mcp.tool()
async def providers_status() -> dict[str, Any]:
    """List every configured source, its class, and whether credentials are
    present. Call this when results look thin -- a missing key means a whole
    source class is silently absent from every search."""
    allp = [HN, REDDIT, XP, TAVILY]
    return {
        "providers": [
            {"name": p.name, "class": p.source_class,
             "available": p.available(),
             "needs": _needs(p.name) if not p.available() else None}
            for p in allp
        ],
        "corpus": str(corpus.path),
    }


def _needs(name: str) -> str:
    return {
        "reddit": "REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET (app must be approved by Reddit)",
        "x": "X_API_KEY (twitterapi.io or equivalent)",
        "tavily": "TAVILY_API_KEY (1,000 free credits/month)",
    }.get(name, "")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
