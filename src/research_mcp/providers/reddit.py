"""Reddit via Arctic Shift -- the Pushshift successor.

The official route is closed. Self-service app creation now returns a link to
the Responsible Builder Policy instead of credentials, unauthenticated .json
endpoints 403, and approval is a ticket with an uncertain outcome that reportedly
skews against small projects.

Web search doesn't substitute either: Reddit's robots.txt blocks every crawler
except Google's (a $60M licensing deal), so Bing, DuckDuckGo and the AI search
APIs built on them see nothing. Tavily scoped to reddit.com returns subreddit
landing pages regardless of the query -- verified 2026-09-19.

Arctic Shift serves the same archive as the bulk dumps over plain
unauthenticated HTTP: 2005 to present, no key, no registration, no approval.

Its one real limitation is that keyword parameters only work alongside an
author or subreddit -- there is no global full-text search. For complaint
mining that costs nothing, since the question is always "what do people in
r/running say", never "search all of Reddit".
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from .base import COMMUNITY, Provider, ProviderError, Result

API = "https://arctic-shift.photon-reddit.com/api"

# Measured 2026-09-19, not taken from the docs: the published "~2 req/s" trips
# a 429 almost immediately and then leaves the endpoint answering 422 for a
# while afterwards. At a 3s gap it is stable. Keyword search on a busy
# subreddit is genuinely slow too -- 6-8s server-side is normal, occasionally
# instant when cached.
#
# The limiter is module-level because the router fans out concurrently; a
# per-instance one would let parallel calls trip the limit together.
_MIN_INTERVAL = 5.0        # full-text search: expensive, trips the limiter
_MIN_INTERVAL_LIST = 1.5   # plain listings: ~1s server-side, far lighter

# ONE request per call. Measured: a single keyword search takes 8-18s including
# retries, and a second immediately afterwards is refused. Arctic Shift is a
# free community service running full-text queries over a 20-year archive --
# roughly one search per 30-60s is what it actually sustains.
#
# So Reddit cannot join interactive fan-out the way HN and X do. Two requests
# would exceed the ~30s budget Claude Code allows a tool and return nothing at
# all. The right usage is several narrow calls spaced out, one subreddit at a
# time, which the tool description tells the caller to do.
_MAX_REQUESTS = 1

# Pages of 100 when browsing by date. Measured: a listing returns 100 posts in
# ~1s, while ONE server-side keyword search over the same subreddit takes 8-18s
# and then trips the throttle. So browsing a date window and filtering locally
# is both faster and more complete -- it covers everything in the window rather
# than whatever the archive's relevance ranking decides to surface.
_PAGE = 100
# 12 pages = 1200 posts. That is a COMPLETE window for a small sub (r/hyrox ran
# ~760 posts in five weeks) but silently truncates a busy one -- r/Entrepreneur
# burns 1200 posts in under three weeks, so a "since January" browse quietly
# returned only the last fortnight. Raised, and browse() now reports whether it
# reached `since` or hit the cap, because a truncated window that looks
# complete is how you conclude a topic is not discussed.
_MAX_PAGES = 40
_lock = asyncio.Lock()
_last = 0.0


async def _throttle(interval: float = _MIN_INTERVAL) -> None:
    global _last
    async with _lock:
        wait = interval - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.monotonic()


class Reddit(Provider):
    name = "reddit"
    source_class = COMMUNITY

    last_browse_truncated: tuple[str, object] | None = None

    def available(self) -> bool:
        return True  # no credentials, no approval

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,
        until: str | None = None,
        subreddits: list[str] | None = None,
        include_comments: bool = False,
    ) -> list[Result]:
        if not subreddits:
            # Not a failure of ours -- state the constraint plainly so the
            # caller can fix the call rather than conclude Reddit is empty.
            raise ProviderError(
                "reddit (arctic-shift) has no global full-text search: pass "
                "subreddits, e.g. ['running','hyrox','Garmin']"
            )

        # Fast path. With a date window we can page listings (~1s per 100
        # posts) and match locally, instead of paying 8-18s for one throttled
        # full-text search. It is also more COMPLETE: browsing covers every
        # post in the window, where search returns only what ranks -- and
        # "what do people complain about" is a question about the whole
        # window, not about the best-known threads in it.
        if since and not include_comments:
            posts = await self.browse(subreddits[0], since=since, until=until)
            if query:
                scored = [(_relevance(r, query), r) for r in posts]
                hits = [r for sc, r in sorted(
                    ((sc, r) for sc, r in scored if sc > 0),
                    key=lambda p: (-p[0], -(p[1].score or 0)),
                )]
            else:
                hits = posts
            if len(subreddits) > 1:
                raise _Truncated(hits[:limit] if limit else hits,
                                 [(s, "posts") for s in subreddits[1:]])
            return hits

        kinds = ["posts", "comments"] if include_comments else ["posts"]

        plan = [(sub, k) for sub in subreddits for k in kinds]
        skipped = plan[_MAX_REQUESTS:]
        plan = plan[:_MAX_REQUESTS]

        out: list[Result] = []

        async with httpx.AsyncClient(
            timeout=40, follow_redirects=True,
            headers={"User-Agent": "research-mcp/0.1 (personal research tool)"},
        ) as client:
            for sub, k in plan:
                params: dict[str, object] = {
                    "subreddit": sub,
                    "limit": min(limit, 100),
                    # keyword field differs by endpoint: posts take `query`
                    # (title + selftext), comments take `body`
                    ("body" if k == "comments" else "query"): query,
                }
                if since:
                    params["after"] = since
                if until:
                    params["before"] = until

                data = await self._get(client, f"{API}/{k}/search", params)
                for item in data:
                    r = _to_result(item, k, query)
                    if _is_empty(r):
                        continue
                    out.append(r)

        if skipped:
            # Partial coverage must be visible: silently searching 2 of 4
            # subreddits and reporting a clean result is how you conclude
            # nobody complains about something.
            raise _Truncated(out, skipped)
        return out

    async def browse(
        self,
        subreddit: str,
        *,
        since: str | None = None,
        until: str | None = None,
        max_pages: int = _MAX_PAGES,
    ) -> list[Result]:
        """Page a subreddit backwards by date. No full-text search involved.

        This is the fast path: ~1s per 100 posts versus 8-18s for one keyword
        search. It also gives *complete* coverage of the window, where search
        gives only what ranks -- which matters when the question is "what do
        people complain about", not "find the best-known thread".
        """
        out: list[Result] = []
        before = until
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=True,
            headers={"User-Agent": "research-mcp/0.1 (personal research tool)"},
        ) as client:
            for _ in range(max_pages):
                params: dict[str, object] = {
                    "subreddit": subreddit, "limit": _PAGE, "sort": "desc",
                }
                if before:
                    params["before"] = before
                if since:
                    params["after"] = since

                page = await self._get(client, f"{API}/posts/search", params, light=True)
                if not page:
                    break
                for item in page:
                    r = _to_result(item, "posts", "")
                    if not _is_empty(r):
                        out.append(r)
                oldest = page[-1].get("created_utc")
                if not oldest:
                    break
                # Step the cursor one second past the oldest row to avoid
                # re-requesting the same boundary post forever.
                before = datetime.fromtimestamp(oldest - 1, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                if len(page) < _PAGE:
                    break
            else:
                # Loop finished without break => we exhausted max_pages and the
                # window is NOT complete back to `since`.
                if out:
                    oldest_seen = min(r.published_at for r in out if r.published_at)
                    self.last_browse_truncated = (subreddit, oldest_seen)
        return out

    async def comment_tree(self, post_id: str, limit: int = 500) -> list[Result]:
        """Full discussion under one post (server caps at 25k comments).

        The highest-signal call for complaint mining: a single thread titled
        "anyone else getting injured on this plan?" carries hundreds of
        first-person accounts that keyword search never surfaces individually.
        """
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=True,
            headers={"User-Agent": "research-mcp/0.1 (personal research tool)"},
        ) as client:
            data = await self._get(client, f"{API}/comments/tree",
                                   {"link_id": post_id, "limit": limit})
        out = [_to_result(c, "comments", f"tree:{post_id}") for c in _flatten(data)]
        return [r for r in out if not _is_empty(r)]

    async def _get(self, client: httpx.AsyncClient, url: str,
                   params: dict[str, object], *, light: bool = False) -> list[dict]:
        for attempt in range(3):
            await _throttle(_MIN_INTERVAL_LIST if light else _MIN_INTERVAL)
            try:
                r = await client.get(url, params=params)
            except httpx.HTTPError as e:
                raise ProviderError(f"reddit/arctic-shift: {e}") from e

            if r.status_code == 200:
                d = r.json()
                return d.get("data") or [] if isinstance(d, dict) else (d or [])
            # 422 here means "slow down", not "bad request" -- back off rather
            # than reporting an empty subreddit.
            # 422 "Timeout. Maybe slow down a bit" and 429 are both throttle
            # states here, not bad requests -- and they persist for a few
            # seconds after being tripped. Back off hard; reporting an empty
            # subreddit would read as "nobody discusses this".
            if r.status_code in (422, 429) and attempt < 2:
                await asyncio.sleep(4.0 * (attempt + 1))
                continue
            raise ProviderError(
                f"reddit/arctic-shift {r.status_code}: {r.text[:160]}"
            )
        return []


# Explicit tombstones only. An EMPTY body is not dead: a link post legitimately
# has no selftext, and treating "" as removed would silently discard exactly
# the posts that share an article -- often the most useful ones.
_TOMBSTONE = {"[removed]", "[deleted]", "[removed by reddit]"}


# Reddit's automated accounts. Their comments are boilerplate that shows up in
# every thread and crowds out real replies in a length-ranked list.
_BOTS = {"automoderator", "read-the-rules", "sneakpeekbot", "remindmebot",
         "totesmessenger", "wikitextbot", "b0trank", "converter-bot"}


def _is_bot(author: str | None) -> bool:
    a = (author or "").lower()
    return a in _BOTS or a.endswith("-bot") or a.endswith("_bot")


def _is_empty(r: Result) -> bool:
    """Drop rows with nothing usable left.

    Dropping every tombstoned POST was wrong, and badly so. Strict subreddits
    auto-remove submissions pending human review, and the archive ingests at
    creation time -- so it stores the removed state. Measured 2026-09-20:
    r/running is 94% "[removed]" against r/hyrox's 2%. Filtering those out made
    one of the most active running communities on Reddit look like it posts
    twenty times a fortnight.

    The title survives removal, and comments are not auto-removed at all. So
    keep the post, flag the missing body, and get the substance from
    read_discussion. A tombstoned comment really is worthless -- there is no
    title to fall back on.
    """
    if _is_bot(r.author):
        return True
    body = (r.text or "").strip().lower()
    kind = r.raw.get("kind")
    if kind == "comments":
        return body in _TOMBSTONE or not body
    if body in _TOMBSTONE:
        # Keep it only if the title alone carries meaning.
        r.text = ""
        r.raw["body_removed"] = True
        return len((r.title or "").split()) < 4
    return False


def _flatten(nodes: object, depth: int = 0) -> list[dict]:
    """Unwrap Reddit's comment tree into a flat list.

    /comments/tree returns the raw Reddit shape -- {"kind": "t1", "data": {...}}
    with replies nested under data.replies, itself wrapped in
    {"data": {"children": [...]}}. The flat /comments/search endpoint returns
    bare comment objects instead, so the two need different handling.

    Depth is kept because it is real signal: a deep reply is usually someone
    answering a specific follow-up, which is where the detail lives.
    """
    out: list[dict] = []
    if isinstance(nodes, dict):
        nodes = nodes.get("children") or nodes.get("data") or []
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if not isinstance(node, dict):
            continue
        body = node.get("data") if node.get("kind") else node
        if not isinstance(body, dict):
            continue
        replies = body.pop("replies", None)
        if body.get("body"):
            body["_depth"] = depth
            out.append(body)
        if replies:
            out.extend(_flatten(replies, depth + 1))
    return out


def _relevance(r: Result, query: str) -> int:
    """How many of the query's content words appear in title + body.

    RANK, don't filter. Matching ANY term was useless -- "training plan app
    injury adapt recovery" matched every post in r/running, because "training"
    is in all of them. But requiring a majority threw out the two posts that
    actually mattered, since a title alone rarely carries three terms.

    Browsing already covers the whole window, so nothing is gained by
    discarding rows: order them by how many terms they hit and let `limit` cut
    the tail. Title matches count double -- a word in the title is what the
    post is ABOUT, the same word in the body may be an aside.

    Word boundaries with a short suffix allowance, because substring matching
    pulled "SLC female partner needed" into an injury search.
    """
    import re
    terms = [t for t in re.findall(r"[a-z0-9]{3,}", query.lower()) if t not in _STOP]
    if not terms:
        return 1
    title, body = (r.title or "").lower(), (r.text or "").lower()
    score = 0
    for t in terms:
        pat = rf"\b{re.escape(t)}\w{{0,4}}\b"
        if re.search(pat, title):
            score += 2
        elif re.search(pat, body):
            score += 1
    return score


_STOP = {"the", "and", "for", "with", "что", "are", "was", "how", "why", "who"}


class _Truncated(Exception):
    """Results plus the subreddit/kind pairs left unsearched. Router unwraps
    this: partial coverage is a caveat on the answer, not a failure."""

    def __init__(self, results: list[Result], skipped: list[tuple[str, str]]) -> None:
        self.results = results
        self.skipped = skipped
        super().__init__(
            f"request budget reached; not searched: "
            + ", ".join(f"r/{s} {k}" for s, k in skipped)
        )


def _to_result(item: dict, kind: str, query: str) -> Result:
    created = item.get("created_utc")
    sub = item.get("subreddit")
    if kind == "comments":
        title = f"comment in r/{sub}"
        text = item.get("body", "") or ""
        link = item.get("link_id", "").replace("t3_", "")
        url = f"https://reddit.com/comments/{link}/_/{item.get('id','')}" if link else ""
    else:
        title = item.get("title", "") or ""
        text = item.get("selftext", "") or ""
        url = item.get("url") or f"https://reddit.com/comments/{item.get('id','')}"

    return Result(
        source="reddit",
        source_class=COMMUNITY,
        title=title,
        url=url,
        text=text,
        author=item.get("author"),
        published_at=(
            datetime.fromtimestamp(created, tz=timezone.utc) if created else None
        ),
        score=item.get("score"),
        comments=item.get("num_comments"),
        query=query,
        raw={"subreddit": sub, "kind": kind, "depth": item.get("_depth")},
    )
