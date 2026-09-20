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
    min_faves: int | None = None,
    include_comments: bool = False,
    sort: str = "top",
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
        subreddits: REQUIRED for Reddit -- the archive has no global full-text
            search, only within a subreddit. Reddit is skipped with a clear
            reason if omitted.
            ⚠ Pass ONE subreddit per call. The archive is a free service that
            sustains roughly one search per 30-60s; a call naming several
            subreddits searches only the first and says so. To cover
            r/running, r/hyrox and r/Garmin, make three separate calls and
            expect each to take 10-20s. Batching them returns nothing.
        since/until: "YYYY-MM-DD" bounds.
        min_points: HN score floor -- use ~50 to cut noise on broad topics.
        min_faves: X like floor. Worth setting: X bills per page whether the
            tweets are useful or not, so filtering junk up front is the main
            lever on cost. ~10 for niche topics, ~100 for busy ones.
        sort: "top" (default) ranks X by engagement and skews OLD -- a Top
            search can return results 1-2 years back. "latest" returns today's
            posts, but they have near-zero likes because nothing has had time
            to vote, so min_faves must be dropped when using it. Use "top" for
            "what is the strongest signal", "latest" for "what is happening
            right now".
        include_comments: also search reply bodies, not just posts/stories.
            Slower and noisier, but where complaints actually live -- the
            first-person "this plan wrecked my knee" account is a reply, not
            a thread title.
        limit: how many results to SHOW per provider. Everything retrieved is
            stored in the corpus regardless -- this only trims the reply.
    """
    chosen = [COMMUNITY_PROVIDERS[p] for p in (platforms or COMMUNITY_PROVIDERS) if p in COMMUNITY_PROVIDERS]
    res = await fan_out(
        chosen, question,
        limit_per=limit, since=since, until=until,
        subreddits=subreddits, min_points=min_points, min_faves=min_faves,
        include_comments=include_comments, sort=sort,
    )
    corpus.record(tool="search_community", question=question, queries=res.queries,
                  providers=res.providers, results=res.results)
    return res.to_model(max_shown=limit)


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
    return res.to_model(max_shown=limit)


@mcp.tool()
async def read_discussion(
    post_url_or_id: str,
    limit: int = 200,
) -> dict[str, Any]:
    """Read the FULL comment thread under one Reddit post.

    This is the highest-signal call for complaint mining, and the natural
    follow-up to search_community. Posts are the question; comments are the
    answer. A thread titled "non stop injuries from training" carries dozens of
    first-person accounts -- what the injury was, what plan caused it, what
    people did instead -- none of which keyword search surfaces individually,
    because each one is a reply, not a title.

    One request, up to 25k comments, no keyword matching involved. Prefer this
    over include_comments: searching comment bodies is the slow throttled path
    and returns fragments without the thread context that makes them readable.

    Args:
        post_url_or_id: a reddit.com/r/.../comments/<id>/... URL, or the bare id.
        limit: max comments to retrieve.
    """
    pid = _post_id(post_url_or_id)
    try:
        results = await REDDIT.comment_tree(pid, limit=limit)
    except Exception as e:  # noqa: BLE001 - report, never raise through MCP
        return {"post": pid, "error": f"{type(e).__name__}: {e}", "count": 0, "results": []}

    corpus.record(tool="read_discussion", question=f"thread:{pid}",
                  queries=[pid], providers={"reddit": len(results)}, results=results)

    # Deepest signal sits in the longest replies, not the top-voted ones --
    # scores on a young thread mean little, but a 400-character answer is
    # someone recounting what actually happened to them.
    ranked = sorted(results, key=lambda r: len(r.text or ""), reverse=True)
    return {
        "post": pid,
        "count": len(results),
        "results": [r.to_model(max_text=900) for r in ranked[:60]],
        "note": ("ranked by reply length, not score: a young thread has no "
                 "settled votes, and long replies are first-person accounts"),
    }


def _post_id(s: str) -> str:
    """Accept a full permalink or a bare id."""
    s = s.strip().rstrip("/")
    if "/comments/" in s:
        return s.split("/comments/")[1].split("/")[0]
    return s.rsplit("/", 1)[-1].replace("t3_", "")


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
    rows = [
        {"name": p.name, "class": p.source_class,
         "available": p.available(),
         "needs": _needs(p.name) if not p.available() else None}
        for p in allp
    ]
    # Only X meters per call, and its free bonus is small enough to run out
    # mid-question. A metered source going quiet must be visible.
    if bal := await XP.balance():
        next(r for r in rows if r["name"] == "x")["budget"] = bal
    return {"providers": rows, "corpus": str(corpus.path)}


def _needs(name: str) -> str:
    return {
        "x": "X_API_KEY (twitterapi.io or equivalent)",
        "tavily": "TAVILY_API_KEY (1,000 free credits/month)",
    }.get(name, "")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
