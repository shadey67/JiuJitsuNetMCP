# JiuJitsuNetMCP

An MCP server exposing [JiuJitsu.net](https://jiujitsu.net)'s Elo ratings, athlete
profiles, rated match history and IBJJF tournament brackets to an LLM client.

JiuJitsu.net rates IBJJF competitors with an Elo system, separately for gi and
no-gi. This server wraps its public JSON API as seven tools, trimming each
response down to the fields that are useful for reasoning and dropping the rest.

The site is not mine. It renders client-side, so fetching its pages returns an
empty HTML shell — all the data comes from the API, and the server is built to
keep the number of requests it makes low.

## Requirements

- Python 3.14 (developed against 3.14.7)
- `mcp` 1.30, `httpx` 0.28, `cachetools` 7.2

```bash
python -m venv mcp-env
mcp-env/Scripts/pip install "mcp[cli]" httpx cachetools
```

## Client configuration

Point your MCP client at the venv interpreter and `server.py`:

```json
{
  "mcpServers": {
    "jiujitsunet": {
      "command": "REPO LOCATION\\JiuJitsuNetMCP\\mcp-env\\Scripts\\python.exe",
      "args": ["REPO LOCATION\\JiuJitsuNetMCP\\server.py"]
    }
  }
}
```


## API endpoints used

| Path | Tool |
|---|---|
| `/api/athletes?search=` | `search_athlete` |
| `/api/athlete/{slug}?gi=` | `get_athlete`, slug resolution in `get_matches` |
| `/api/matches?gi=&athlete_id=&page=` | `get_matches` |
| `/api/top?...` | `get_rankings` |
| `/api/brackets/events` | `get_upcoming_events` |
| `/api/brackets/categories/{event_id}` | `get_event_categories` |
| `/api/brackets/competitors?link=&age=&gender=&gi=&belt=&weight=` | `get_bracket` |

### search_athlete

Name search. Returns `name` (full legal name), `personal_name` (what they go by)
and `slug`. No athlete id — use the slug, or get an id from `get_rankings`.

### get_athlete

Full profile for one slug. `gi` is **required**: gi and no-gi are separate rating
systems and an athlete's two profiles differ in rating, medals and ranked
divisions. Returns the `gi` flag it was called with, plus belt, team, country,
Instagram, current rating, `rating_history` (dated snapshots, each recording the
belt *and* team held at that point), `rankings` (per division, with percentile
and the division's average rating) and `medals`.

### get_matches

Rated match history, newest first, 12 per page. Each match has date, event,
division, result from this athlete's point of view, opponent name and slug, the
opponent's rating before the match, this athlete's rating change, whether it
ended by submission, and `videoLink` when a recording exists (YouTube is free;
FloGrappling needs a subscription).

`athlete` takes either a UUID `athlete_id` or a slug. Prefer the id — a slug
costs an extra request to resolve. `get_rankings` and `get_bracket` both return
ids; `search_athlete` does not.

### get_rankings

Elo rankings for a division, best first, 30 per page. Each athlete has rank,
name, slug, `athlete_id`, country, rating, `rank_change` and `rating_change`
since the last ranking update, match count, and any upcoming events they're
registered for. `belt` is uppercase; `weight` empty means pound-for-pound across
all weights; `country` is a two-letter lowercase code.

Prefer filters over paging. This returns ratings and match counts only — any
question about records, opponents or submissions needs `get_matches`.

### get_upcoming_events

Events whose brackets have been released, as `{"id", "name"}`. The name is the
official IBJJF title, which is the only place the gi/no-gi ruleset is recorded.

### get_event_categories

Every bracket at an event, each as `age`, `belt`, `gender`, `weight` and `link`.
A single event carries hundreds — Pan No-Gi 2026 has 333 across four belts and
ten age groups — so narrow it with the optional filters. They match whole values
case-insensitively and are applied locally, so filtering costs nothing and the
underlying call is cached. `total` is the event's category count, `matching` is
how many came back.

### get_bracket

One bracket's competitors and draw. `link`, `age`, `belt`, `gender` and `weight`
must come from the same `get_event_categories` record — a mismatch is rejected
with a 400, not ignored. `gi` is not in that record and must come from the event
name.

`competitors` is in seed order with seed, name, slug, `athlete_id`, team,
country, rating, world rank, percentile and match count.

`matches` is the draw, ordered by round, each with bracket number, round,
`scheduled` time and mat, and a red and blue corner. A corner is one of:

```
a competitor   {"name": "...", "slug": "...", "seed": 11, "win_probability": 0.892}
a bye          {"bye": true}
an empty slot  {"tbd": "Winner of Fight 10, Mat 1"}
```

`win_probability` is the corner's Elo chance of winning, so the two corners sum
to 1; it appears only once both competitors are known. Corners carry just enough
to identify an athlete — join on `slug` to `competitors` for team and rating. A
decided match also has `winner`, and the beaten corner is marked `eliminated`.
