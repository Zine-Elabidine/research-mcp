# research-mcp

A local MCP server that gives Claude **federated, multi-source research** —
for project ideas, job hunting, tech trends and market validation.

## Why

On 2026-09-19, asked "what are the hottest markets right now," Claude came back
with SEO content farms all repeating the same line: *the AI wrapper era is over,
80% will fail*. One structured data point contradicted the entire narrative —
**Cal AI: $40M ARR, bootstrapped, 7 employees, a calorie tracker.** That number
came from an ARR database, not an article.

The problem was never search quality. It was **single-source dependency on the
SEO layer, with nothing to cross-check against.**

> A good search doesn't rely on one source. Exhaustiveness comes from fan-out
> across source *classes*, dedup, and explicit disagreement detection — not from
> a better provider.

## Design

Sources are grouped by **what kind of truth they carry**, not by brand. Every
provider sits behind one interface and is swappable by config.

| Class | Answers | Provider | Cost |
|---|---|---|---|
| `consensus` | what the web says | Tavily | 1,000 credits/mo free |
| `community` | what users actually say | HN Algolia, Reddit | free |
| `social` | what's hot right now | twitterapi.io | $0.15/1k tweets |

Swappability isn't theoretical: twitterapi.io can be shut down (SocialData.tools
was), Tavily's free tier can change, Reddit can reject your OAuth app.

```
Claude (planner + synthesizer — already exists)
        │  MCP
┌───────▼──────────────────────────────────┐
│  ROUTER                                  │
│   fan-out · normalize · dedup            │
│   round-robin interleave                 │
│   graceful degradation                   │
└───────┬──────────────────────────────────┘
   ┌────┴─────┬──────────┐
  consensus community social
        │
┌───────▼──────────────────────────────────┐
│  CORPUS (SQLite) — every result, forever │
└──────────────────────────────────────────┘
```

**Not** a rebuild of GPT Researcher / open_deep_research. Their pattern is
planner → parallel executors → synthesizer, and Claude already *is* that loop.
What was missing is the retrieval layer underneath it.

Three behaviours the router guarantees:
- **concurrent** — one slow source never serialises the pass
- **graceful** — a dead or unconfigured provider is recorded and skipped, never fatal
- **interleaved** — results round-robin across providers, so no single source
  owns the top of the list (engagement scores aren't comparable across
  platforms anyway; an HN 500 and a Reddit 500 mean different things)

Items surfacing from two sources are tagged `corroborated_by` — the seed of
real cross-checking.

### The corpus

Write-through, never a cache: results are always fetched live, the corpus only
records *what was seen and when*. Two payoffs — longitudinal questions ("what
changed since March") become a single join, and anything already pulled stays
yours if a provider disappears.

## Tools

| Tool | Use it for |
|---|---|
| `search_community` | complaints, lived experience, launch reception, hiring demand |
| `search_web` | published claims, articles, docs — the consensus layer |
| `corpus_stats` | how much history has accumulated |
| `providers_status` | which sources are live vs missing credentials |

## Setup

```bash
uv sync
cp .env.example .env     # fill in what you have; HN needs nothing
```

Register with Claude Code:

```bash
claude mcp add research -s user -- uv --directory ~/research-mcp run research-mcp
```

Debug standalone:

```bash
npx @modelcontextprotocol/inspector uv --directory ~/research-mcp run research-mcp
```

### Credentials

- **HN** — none. Works immediately.
- **Tavily** — free key at app.tavily.com, no card. Advanced depth costs more
  than one credit, so a fanned-out pass burns the tier faster than the headline
  1,000 suggests: budget ~20–30 real research tasks/month.
- **Reddit** — ⛔ **currently unreachable.** Self-service app creation is closed
  under the Responsible Builder Policy (create app returns a policy link, not
  credentials), and unauthenticated `.json` endpoints return 403. Two things
  that do NOT work as substitutes, both verified 2026-09-19:
  - Tavily scoped to `reddit.com` returns subreddit *landing pages*
    (r/nfl, r/nsfw, r/deathbattle) regardless of query — Reddit blocks crawlers,
    so no web index holds thread content.
  - Tavily also **silently drops `include_domains`** when nothing matches on the
    domain, returning unrelated off-domain results with no flag. Enforced
    client-side now; discards are reported in provider stats.

  Complaint mining without Reddit: unfiltered web search surfaces Trustpilot,
  app-store reviews and review blogs, which carry the same 1-star language and
  are actually crawlable.

  Original terms, if access is ever granted: free for non-commercial (100 QPM
  with OAuth). ⚠ Self-service
  registration closed in late 2025; every app is **manually approved and can be
  silently rejected**. Register a `script` app at reddit.com/prefs/apps early —
  this is the long pole.
- **X** — the official API has no free tier ($0.005/read, **7-day search
  window**; full archive is Enterprise at $42K+/mo). twitterapi.io gives the
  full archive with no gate.
  - **Cost is a rounding error.** 100,000 credits = $1, 15 credits per tweet
    (min 15/call). Billing is per PAGE: ~20 tweets ≈ 300 credits ≈ **$0.003**.
    $1 ≈ 6,600 tweets. `limit` only trims what Claude is *shown* — everything
    fetched is stored — so `min_faves` is a result-quality lever, not a budget one.
  - **QPS is the real limit.** A never-paid account is capped at **0.2 QPS**
    (one request per five seconds), which throttles concurrent fan-out long
    before credits run out. Any paid top-up lifts it to 3 QPS permanently. The
    pricing table lists Free at 3/s — that's the *past-customer* rate.
  - **Don't subscribe.** Starter at $29/mo delivers ~208k tweets/month against
    a need of a few thousand. Pay-as-you-go $5 ≈ 33k tweets and flips the QPS
    tier for good.
  - ⚠ Legally split: hiQ v. LinkedIn means scraping public data isn't a CFAA
    violation, but it does breach X's ToS — fine for personal tooling, revisit
    entirely if this ever ships to users. Providers in this category do get shut
    down (SocialData.tools), hence the swappable interface and the corpus.

## Status

v0.1 — `search_community` (HN + Reddit + X), `search_web` (Tavily), corpus.
Deliberately three tools, not six.

Next: Exa semantic search · `lookup_facts` (GitHub velocity, ARR, app-store
reviews) · `UNCORROBORATED` flagging across classes · `fetch` with clean extraction.
