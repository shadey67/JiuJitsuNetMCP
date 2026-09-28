import time, httpx
from cachetools import TTLCache
from mcp.server.fastmcp import FastMCP
from urllib.parse import quote

mcp = FastMCP("jiujitsunet")
cache = TTLCache(maxsize=256, ttl= 6 * 3600)
client = httpx.Client(base_url="https://jiujitsu.net", timeout=10)

_last = 0.0

def fetch(path: str) -> dict:
    global _last
    if path in cache:
        return cache[path]
    time.sleep(max(0, 2 - (time.time() - _last)))
    _last = time.time()
    r = client.get(path)
    r.raise_for_status()
    cache[path] = r.json()
    return cache[path]


def _trim_athlete(raw: dict) -> dict:
    athlete = raw.get("athlete", {})
    result = {
        "name": athlete.get("name"),
        "belt": athlete.get("belt"),
        "team": athlete.get("team_name"),
        "country": athlete.get("country"),
        "rating": athlete.get("rating"),
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
            return {"error": f"No athlete found with slug '{slug}'"}
        return {"error": f"JiuJitsu.net returned HTTP {e.response.status_code}"}

@mcp.tool()
def get_athlete(slug: str) -> dict:
    """Get a BJJ athlete's profile from JiuJitsu.net by their URL slug
    (e.g. "owen-patrick-shade").

    Returns: current belt, team, country and Elo rating; their rank within
    each division they're ranked in (percentile = where they sit, as a
    percentage, among that division - lower is better, compare with the
    division's average rating); along with their
    achieved tournament medals with event, date, division and place
    (1 = gold).
    """

    return _trim_athlete(fetch(f"/api/athlete/{slug}"))

if __name__ == "__main__":
    mcp.run()
