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
    _last = time.time()
    r = client.get(path)
    r.raise_for_status()
    try:
        cache[path] = r.json()
    except ValueError:
        # Unknown paths serve the single-page app's HTML shell with a 200,
        # so raise_for_status lets them through and only the decode fails.
        raise RuntimeError(
            f"JiuJitsu.net served HTML rather than JSON for {path}, which "
            f"usually means the path is wrong") from None
    _disk_put(path, cache[path])
    return cache[path]


def _trim_athlete(raw: dict, gi: bool) -> dict:
    athlete = raw.get("athlete", {})
    result = {
        # Stamped from the request: medal records carry no ruleset of their
        # own, so two profiles for the same athlete are otherwise identical
        # in shape and impossible to tell apart once separated.
        "gi": gi,
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
        "videoLink": m.get("videoLink")
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

def _trim_competitor(c: dict) -> dict:
    result = {
        "seed": c.get("seed"),
        "name": c.get("name"),
        "personal_name": c.get("personal_name"),
        "slug": c.get("slug"),
        "athlete_id": c.get("id"),
        "team": c.get("team"),
        "country": c.get("country"),
        "rating": round(c["rating"]) if c.get("rating") is not None else None,
        "rank": c.get("rank"),
        "percentile": round(c["percentile"] * 100, 1)
        if c.get("percentile") is not None else None,
        "matches": c.get("match_count"),
    }
    if c.get("next_when") or c.get("next_where"):
        result["next_match"] = {"when": c.get("next_when"),
                                "where": c.get("next_where")}
    return result

def _trim_corner(m: dict, side: str):
    """One side of a bracket match: a competitor, a bye, or an empty slot.

    Always a dict so the shape is predictable. Detail is deliberately thin -
    join on slug to the competitors list rather than repeating team, country
    and rating for every round an athlete appears in.
    """
    if m.get(f"{side}_bye"):
        return {"bye": True}
    name = m.get(f"{side}_personal_name") or m.get(f"{side}_name")
    if not name:
        # Slot not filled yet; next_description names the feeding match.
        nxt = m.get(f"{side}_next_description")
        return {"tbd": nxt} if nxt else {"tbd": None}
    corner = {
        "name": name,
        "slug": m.get(f"{side}_slug"),
        "seed": m.get(f"{side}_seed"),
    }
    expected = m.get(f"{side}_expected")
    if expected is not None:
        corner["win_probability"] = round(expected, 3)
    if m.get(f"{side}_loser") is True:
        corner["eliminated"] = True
    return corner

def _trim_bracket_match(m: dict) -> dict:
    result = {
        "match": m.get("display_match_num"),
        "round": m.get("fight_num"),
        "red": _trim_corner(m, "red"),
        "blue": _trim_corner(m, "blue"),
    }
    if m.get("final"):
        result["final"] = True
    when, where = m.get("when") or None, m.get("where")
    if when or where:
        result["scheduled"] = {"when": when, "where": where}
    # loser is False rather than None for a fight that simply has not
    # happened yet, so only call a winner when one side is explicitly out.
    red_out, blue_out = m.get("red_loser") is True, m.get("blue_loser") is True
    if red_out != blue_out:
        won = "blue" if red_out else "red"
        result["winner"] = (m.get(f"{won}_personal_name")
                            or m.get(f"{won}_name"))
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
    rating change, and whether it ended by submission.
    We also return videoLink, if present, this will be a link to the recording
    of the match. Videos can come from 2 sources, flograppling or youtube.
    Flograppling requires a subscribtion to watch, whereas youtube is free.
    If videoLink is not present, there's no publicly available recording of the match.
    Does NOT include the opponent's team - call get_athlete with opponent_slug for that.
    If total_pages > 1, call again with page=2, 3... to see older matches, at
    one request per page.
    """
    try:
        # Quoted with gi even though the id is the same either way, so the
        # lookup shares a cache entry with get_athlete for this ruleset.
        athlete_id = (athlete if _UUID.match(athlete)
                      else fetch(f"/api/athlete/{athlete}?gi={str(gi).lower()}"
                                 )["athlete"]["id"])
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
def get_athlete(slug: str, gi: bool) -> dict:
    """Get a BJJ athlete's profile from JiuJitsu.net by their URL slug
    (e.g. "owen-patrick-shade"). You can also pass a boolean into
    the gi parameter. true will return gi information, false will
    return no gi. If you get asked about an athlete and the user doesn't specify
    whether they're interested in gi or no gi, call the endpoint twice and report
    about both.

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
        return _trim_athlete(fetch(f"/api/athlete/{slug}?gi={str(gi).lower()}"), gi)
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
        return fetch(f"/api/brackets/events")
    except httpx.HTTPStatusError as e:
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}

@mcp.tool()
def get_event_categories(event_id: str, belt: str = "", age: str = "",
                         gender: str = "", weight: str = "") -> dict:
    """
    Get the registered categories at an event (where a category corresponds to a bracket)
    using event_id from get_upcoming_events. Returns a list of categories.
    Each object in the list has the following attributes:
    age (Juvenile 1, Juvenile 2, or Adult)
    belt (e.g. blue, black etc.)
    gender (male or female)
    link (used as a parameter in a later call to find the corresponding bracket)
    weight (the weight class, e.g. Light Feather)

    A single event carries several hundred categories, so narrow it with the
    optional belt, age, gender and weight filters, which match whole values
    and ignore case. They are applied here rather than by the API, so
    filtering is free - the request is the same either way, and repeating it
    with different filters is served from cache. total is how many
    categories the event has, matching is how many came back.

    Pass a category's link, age, belt, gender and weight straight to
    get_bracket to see who is in that bracket. Note that categories carry no
    gi field, because an event is entirely gi or entirely no-gi; get_bracket
    needs gi, so take it from the event name.
    """
    try:
        raw = fetch(f"/api/brackets/categories/{quote(event_id)}")
    except httpx.HTTPStatusError as e:
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}
    rows = all_rows = raw.get("categories", [])
    wanted = {"belt": belt, "age": age, "gender": gender, "weight": weight}
    for field, value in wanted.items():
        if value:
            rows = [c for c in rows
                    if (c.get(field) or "").casefold() == value.casefold()]
    # The payload's own "total" counts something wider than this event's
    # category list, so report the length we actually hold.
    return {
        "categories": rows,
        "total": len(all_rows),
        "matching": len(rows),
    }

@mcp.tool()
def get_bracket(
        link: str,
        age: str,
        gender: str,
        gi: bool,
        belt: str,
        weight: str
) -> dict:
    """Get the seeded competitor list for one bracket at an upcoming event.

    link comes from get_event_categories and must be passed through exactly
    as given (e.g. "/tournaments/3239/categories/2892286"). age, belt,
    gender and weight have to match that same category record - copy them
    across rather than retyping them, because a mismatch is rejected rather
    than ignored. gi is NOT part of the category record: take it from the
    event name returned by get_upcoming_events, so an event with "No-Gi" in
    its title is gi=False and anything else is gi=True.

    Returns competitors in seed order, each with: seed, name, personal_name,
    slug, athlete_id, team, country, their current rating, world rank and
    percentile within this division (lower is better), how many rated
    matches they have, and next_match giving the mat and scheduled time of
    their next fight. Pass athlete_id to get_matches, or slug to
    get_athlete, to go deeper on any of them.

    Also returns matches, the bracket itself, ordered by round. Each match
    has its bracket number, round, scheduled time and mat, and a red and
    blue corner. A corner is one of: a competitor, with name, slug, seed and
    win_probability (this corner's Elo chance of winning, so the two corners
    sum to 1, present only once both competitors are known); {"bye": true};
    or {"tbd": "Winner of Fight 10, Mat 1"} naming the match that will fill
    the slot. Corners carry only enough to identify the athlete - join on
    slug to the competitors list for team, rating and rank. A decided match
    also has winner, and the beaten corner is marked eliminated; on a
    bracket that has not been fought yet neither appears, so absence of a
    winner means undecided, not a draw. Costs one request.
    """
    # link is a path and the API wants its slashes literal, so it is quoted
    # separately - urlencode would force them to %2F.
    query = urlencode({
        "age": age, "gender": gender, "gi": str(gi).lower(),
        "belt": belt, "weight": weight,
    }, quote_via=quote)
    try:
        raw = fetch(f"/api/brackets/competitors?link={quote(link, safe='/')}&{query}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            return {"error": "JiuJitsu.net rejected this request (HTTP 400). Check that "
                             "age, belt, gender and weight match the get_event_categories "
                             "record for this link, and that gi matches the event."}
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}
    matches = sorted(raw.get("matches", []),
                     key=lambda m: (m.get("fight_num") or 0,
                                    m.get("display_match_num") or 0))
    return {
        "division": " / ".join(p for p in (belt, age, gender, weight) if p),
        "competitors": [_trim_competitor(c) for c in raw.get("competitors", [])],
        "matches": [_trim_bracket_match(m) for m in matches],
    }


if __name__ == "__main__":
    mcp.run()
