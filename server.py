import json, os, re, sqlite3, time, httpx
from pathlib import Path
from cachetools import TTLCache
from mcp.server.fastmcp import FastMCP
from urllib.parse import quote, urlencode

INSTRUCTIONS = """JiuJitsu.net BJJ rankings, athlete profiles and rated match history.

This wraps a public API on a site we do not own, so keep the number of
requests low. Every tool call is one request, except get_matches given a
slug instead of an athlete_id, which costs two. Responses are cached for
six hours and the cache survives restarts, so repeating a call you have
already made is free.

Do not fetch or scrape jiujitsu.net pages directly. The site renders its
data client-side, so fetched HTML comes back empty, and the attempt still
costs the site a request. Everything available is exposed by these tools.

When a question needs many requests - comparing match histories across a
whole division, say, which is one request per athlete per page of matches
- say roughly how many requests it will take and ask before spending them.
Do not quietly answer a smaller question instead and present the gap as
missing data.
"""

mcp = FastMCP("jiujitsunet", instructions=INSTRUCTIONS)

TTL = 6 * 3600
cache = TTLCache(maxsize=256, ttl=TTL)
client = httpx.Client(base_url="https://jiujitsu.net", timeout=10)

_last = 0.0
_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.I)


def _db() -> sqlite3.Connection:
    """Open the on-disk response cache, creating it on first use.

    The server runs over stdio, so the process - and with it the in-memory
    cache - dies with every client session. Without this, each restart
    re-fetches everything the previous session had already asked for.
    """
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
            or str(Path.home() / ".cache"))
    d = Path(base) / "jiujitsunet-mcp"
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "api.sqlite3", timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS responses "
                 "(path TEXT PRIMARY KEY, fetched_at REAL, body TEXT)")
    return conn


def _disk_get(path: str):
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT body FROM responses WHERE path = ? AND fetched_at > ?",
                (path, time.time() - TTL),
            ).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, OSError, ValueError):
        return None


def _disk_put(path: str, body: dict) -> None:
    try:
        with _db() as conn:
            conn.execute("REPLACE INTO responses VALUES (?, ?, ?)",
                         (path, time.time(), json.dumps(body)))
            conn.execute("DELETE FROM responses WHERE fetched_at <= ?",
                         (time.time() - TTL,))
    except (sqlite3.Error, OSError, TypeError):
        pass


def fetch(path: str) -> dict:
    global _last
    if path in cache:
        return cache[path]
    stored = _disk_get(path)
    if stored is not None:
        cache[path] = stored
        return stored
    time.sleep(max(0, 2 - (time.time() - _last)))
    _last = time.time()
    r = client.get(path)
    r.raise_for_status()
    cache[path] = r.json()
    _disk_put(path, cache[path])
    return cache[path]


def _trim_athlete(raw: dict) -> dict:
    athlete = raw.get("athlete", {})
    result = {
        "name": athlete.get("name"),
        "belt": athlete.get("belt"),
        "team": athlete.get("team_name"),
        "country": athlete.get("country"),
        "instagram_profile": athlete.get("instagram_profile"),
        "rating": athlete.get("rating"),
        "rating_history": [
            {"date": e.get("date"), "rating": e.get("Rating"),
             "belt": e.get("belt"), "team": e.get("team")}
            for e in raw.get("eloHistory", [])
        ],
        "rankings": [
            {
                "division": " / ".join(
                    p for p in (r.get("belt"), r.get("age"), r.get("gender"),
                                r.get("weight") or "All weights") if p
                ),
                "rank": r.get("rank"),
                "percentile": round(r["percentile"] * 100, 1)
                if r.get("percentile") is not None else None,
                "division_avg_rating": r.get("avg_rating"),
            }
            for r in raw.get("ranks", [])
        ],
        "medals": [
            {"event": m.get("event_name"), "date": m.get("happened_at"),
             "division": m.get("division"), "place": m.get("place")}
            for m in raw.get("medals", [])
        ]
    }
    return result

def _trim_match(m: dict, athlete_id: str) -> dict:
    won = m.get("winnerId") == athlete_id
    opp, me = ("loser", "winner") if won else ("winner", "loser")
    start, end = m.get(f"{me}StartRating"), m.get(f"{me}EndRating")
    return {
        "date": (m.get("date") or "")[:10],
        "event": m.get("event"),
        "division": " / ".join(
            p for p in (m.get("belt"), m.get("age"), m.get("gender"), m.get("weight")) if p
        ),
        "result": "win" if won else "loss",
        "opponent": m.get(opp),
        "opponent_slug": m.get(f"{opp}Slug"),
        "opponent_rating_before": m.get(f"{opp}StartRating"),
        "athlete_rating_change": end - start if start is not None and end is not None else None,
        "submission": bool(m.get("submission")),
    }

def _trim_ranking(r: dict) -> dict:
    rank, prev_rank = r.get("rank"), r.get("previous_rank")
    rating, prev_rating = r.get("rating"), r.get("previous_rating")
    result = {
        "rank": rank,
        "name": r.get("name"),
        "personal_name": r.get("personal_name"),
        "slug": r.get("slug"),
        "athlete_id": r.get("athlete_id"),
        "country": r.get("country"),
        "rating": rating,
        "rank_change": prev_rank - rank if prev_rank is not None and rank is not None else None,
        "rating_change": rating - prev_rating if prev_rating is not None and rating is not None else None,
        "matches": r.get("match_count"),
    }
    if r.get("registrations"):
        result["upcoming_events"] = [
            {"event": e.get("event_name"), "division": e.get("division"),
             "date": e.get("event_start_date")}
            for e in r["registrations"]
        ]
    return result


@mcp.tool()
def get_matches(athlete: str, gi: bool = True, page: int = 1) -> dict:
    """Get an athlete's rated match history from JiuJitsu.net, most recent first.

    athlete is either an athlete_id (a UUID) or a slug. Prefer the
    athlete_id, which get_rankings returns for every athlete: passing it
    costs one request instead of two, because a slug has to be resolved
    first. search_athlete returns only a slug, so use that when you have
    nothing better. Set gi=False for no-gi matches. Each match has: date, event,
    division, result (win/loss from this athlete's point of view), opponent
    name and slug, the opponent's rating before the match, this athlete's
    rating change, and whether it ended by submission. Does NOT include the
    opponent's team - call get_athlete with opponent_slug for that. If
    total_pages > 1, call again with page=2, 3... to see older matches, at
    one request per page.
    """
    try:
        athlete_id = (athlete if _UUID.match(athlete)
                      else fetch(f"/api/athlete/{athlete}")["athlete"]["id"])
        raw = fetch(f"/api/matches?gi={str(gi).lower()}&athlete_id={athlete_id}&page={page}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"No athlete found matching '{athlete}'"}
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}
    return {
        "matches": [_trim_match(m, athlete_id) for m in raw.get("rows", [])],
        "page": page,
        "total_pages": raw.get("totalPages"),
    }

@mcp.tool()
def search_athlete(searchparam : str) -> list[dict]:
    """Search JiuJitsu.net for a BJJ athlete by name.

    Returns: name (their full legal name), personal_name (the name they go by)
    and slug. The value of slug can then be used to hit the athlete api to get
    the belt, team, country, rating, and medals of the athlete.
    """
    try:
        return fetch(f"/api/athletes?search={quote(searchparam)}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"No athlete found after searching: '{searchparam}'"}
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}

@mcp.tool()
def get_athlete(slug: str) -> dict:
    """Get a BJJ athlete's profile from JiuJitsu.net by their URL slug
    (e.g. "owen-patrick-shade").

    Returns: current belt, team, country, instagram profile and Elo rating; rating_history, a
    dated list of rating snapshots that also records the belt and team the
    athlete had at each point (use it to work out who was on which team at
    the time of a past match); their rank within
    each division they're ranked in (percentile = where they sit, as a
    percentage, among that division - lower is better, compare with the
    division's average rating); along with their
    achieved tournament medals with event, date, division and place
    (1 = gold).
    """
    try:
        return _trim_athlete(fetch(f"/api/athlete/{slug}"))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"No athlete found with slug '{slug}'"}
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}

@mcp.tool()
def get_rankings(
        belt: str = "BLACK",
        gender: str = "Male",
        age: str = "Adult",
        weight: str = "",
        country: str = "",
        name: str = "",
        gi: bool = True,
        upcoming: bool = False,
        page: int = 1,
) -> dict:
    """Get the JiuJitsu.net Elo rankings for a division, best first.

    belt is uppercase (e.g. "WHITE", "BLACK"). gender is "Male" or "Female".
    age is e.g. "Adult". weight is a class name such as "Feather" or
    "Light Feather"; leave it empty for the pound-for-pound ranking across
    all weights. country is a two-letter lowercase code such as "br" or "gb"
    (empty = all). name filters by athlete name. Set gi=False for no-gi
    rankings. upcoming=True limits results to athletes registered for a
    future event.

    Returns 30 athletes per page, each with: rank, name, personal_name, slug,
    athlete_id, country, current rating, rank_change and rating_change since
    the previous ranking update (positive = moved up), matches fought, and any
    upcoming events they are registered for. Page N holds roughly ranks
    (N-1)*30+1 to N*30, and total_pages says how many pages exist. Use the
    filters to narrow the list instead of paging through many pages. Pass a
    slug to get_athlete, or the athlete_id to get_matches, for more detail -
    note that the rankings give you rating and match count only, so any
    question about records, opponents or submissions needs get_matches.
    """
    query = urlencode({
        "gender": gender, "age": age, "belt": belt, "weight": weight,
        "country": country, "changed": "false",
        "upcoming": str(upcoming).lower(), "name": name,
        "gi": str(gi).lower(), "page": page,
    }, quote_via=quote)
    try:
        raw = fetch(f"/api/top?{query}")
    except httpx.HTTPStatusError as e:
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}
    return {
        "rankings": [_trim_ranking(r) for r in raw.get("rows", [])],
        "page": page,
        "total_pages": raw.get("totalPages"),
    }

@mcp.tool()
def get_upcoming_events() -> dict:
    """Get all upcoming events in the IBJJF where brackets are already released
    returns events, which is a list of objects. Each object has an ID along with a
    verbose name, which corresponds to the official IBJJF name for the given event
    eg:
    {"events":[{"id":"3239","name":"Pan IBJJF Jiu-Jitsu No-Gi Championship 2026"}]}
    """
    try:
        return fetch(f"/api/events/bracket")
    except httpx.HTTPStatusError as e:
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}

@mcp.tool()
def get_event_categories(event_id: str) -> dict:
    """
    Get all registered categories at an event (where a category corresponds to a bracket)
    using event_id from get_upcoming_events. Returns a list of categories.
    Each object in the list has the following attributes:
    age (Juvenile 1, Juvenile 2, or Adult)
    belt (e.g. blue, black etc.)
    gender (male or female)
    link (used as a parameter in a later call to find the corresponding bracket)
    weight (the weight class, e.g. Light Feather)
    """
    try:
        return fetch(f"/api/brackets/categories/{quote(event_id)}")
    except httpx.HTTPStatusError as e:
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}


if __name__ == "__main__":
    mcp.run()
