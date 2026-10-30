#!/usr/bin/env python3
"""Build data/movies.json: every movie on Netflix Australia, with IMDb ratings.

Sources
- TMDB (themoviedb.org): which movies are on Netflix AU (availability data
  supplied to TMDB by JustWatch), plus details such as runtime, genres,
  countries and the Australian classification.
- IMDb public dataset (title.ratings.tsv.gz): IMDb rating and vote count.

Needs the environment variable TMDB_TOKEN (TMDB "API Read Access Token").
Uses only the Python standard library.
"""

import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

TMDB_API = os.environ.get("TMDB_API", "https://api.themoviedb.org/3")
IMDB_RATINGS_URL = os.environ.get(
    "IMDB_RATINGS_URL", "https://datasets.imdbws.com/title.ratings.tsv.gz"
)
REGION = "AU"
# 8 = Netflix, 1796 = Netflix Standard with Ads
PROVIDERS = os.environ.get("NETFLIX_PROVIDER_IDS", "8|1796")
OUT_DIR = os.environ.get("OUT_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
MOVIES_PATH = os.path.join(OUT_DIR, "movies.json")
FIRST_SEEN_PATH = os.path.join(OUT_DIR, "first_seen.json")
WORKERS = 8
MIN_EXPECTED = int(os.environ.get("MIN_EXPECTED", "500"))

TOKEN = os.environ.get("TMDB_TOKEN", "").strip()


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def tmdb(path, params=None, attempts=6):
    query = urllib.parse.urlencode(params or {})
    url = f"{TMDB_API}{path}" + (f"?{query}" if query else "")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}
    delay = 1.0
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SystemExit(
                    "TMDB rejected the token (401). Check the TMDB_TOKEN secret is the "
                    "long 'API Read Access Token', not the short API key."
                )
            if e.code == 404:
                return None
            retry_after = e.headers.get("Retry-After") if e.headers else None
            wait = float(retry_after) if retry_after else delay
            log(f"HTTP {e.code} on {path}, retry in {wait:.1f}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log(f"Network error on {path}: {e}, retry in {delay:.1f}s")
            time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError(f"Gave up on {url}")


def discover_ids():
    """All TMDB movie ids on Netflix AU (flat-rate)."""
    base = {
        "watch_region": REGION,
        "with_watch_providers": PROVIDERS,
        "with_watch_monetization_types": "flatrate|ads",
        "include_adult": "false",
        "sort_by": "popularity.desc",
        "language": "en-AU",
    }

    def crawl(extra):
        ids, page, total_pages, total_results = [], 1, 1, 0
        while page <= total_pages and page <= 500:
            data = tmdb("/discover/movie", {**base, **extra, "page": page})
            if not data:
                break
            total_pages = data.get("total_pages", 1)
            total_results = data.get("total_results", 0)
            ids.extend(r["id"] for r in data.get("results", []))
            page += 1
        return ids, total_pages, total_results

    ids, total_pages, total_results = crawl({})
    log(f"Discover: {total_results} results over {total_pages} pages")
    if total_pages > 500:
        # TMDB caps paging at 500 pages; split by release year to get the rest.
        log("More than 500 pages, splitting by release year")
        this_year = datetime.now(timezone.utc).year
        ranges = [("1900-01-01", "1979-12-31")]
        ranges += [(f"{y}-01-01", f"{y + 9}-12-31") for y in (1980, 1990, 2000)]
        ranges += [(f"{y}-01-01", f"{y}-12-31") for y in range(2010, this_year + 2)]
        for gte, lte in ranges:
            more, _, _ = crawl({"primary_release_date.gte": gte, "primary_release_date.lte": lte})
            ids.extend(more)
    seen, unique = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


def au_certification(release_dates):
    by_country = {r.get("iso_3166_1"): r.get("release_dates", []) for r in release_dates or []}
    for country in ("AU", "US", "GB"):
        for rd in by_country.get(country, []):
            cert = (rd.get("certification") or "").strip()
            if cert:
                return cert if country == "AU" else f"{country}:{cert}"
    return ""


def details(movie_id):
    d = tmdb(
        f"/movie/{movie_id}",
        {"append_to_response": "external_ids,release_dates", "language": "en-AU"},
    )
    if not d:
        return None
    imdb_id = d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id") or ""
    countries = d.get("origin_country") or [
        c.get("iso_3166_1") for c in d.get("production_countries", []) if c.get("iso_3166_1")
    ]
    date = d.get("release_date") or ""
    overview = (d.get("overview") or "").strip()
    if len(overview) > 280:
        overview = overview[:277].rsplit(" ", 1)[0] + "..."
    return {
        "id": movie_id,
        "t": d.get("title") or d.get("original_title") or "",
        "ot": d.get("original_title") if d.get("original_title") != d.get("title") else "",
        "y": int(date[:4]) if date[:4].isdigit() else None,
        "rt": d.get("runtime") or None,
        "g": [g["id"] for g in d.get("genres", [])],
        "c": countries[:4],
        "l": d.get("original_language") or "",
        "cert": au_certification((d.get("release_dates") or {}).get("results")),
        "imdb": imdb_id,
        "p": d.get("poster_path") or "",
        "o": overview,
        "tm": round(d.get("vote_average") or 0, 1),
        "pop": round(d.get("popularity") or 0, 1),
    }


def imdb_ratings(wanted):
    log("Downloading IMDb ratings")
    req = urllib.request.Request(IMDB_RATINGS_URL, headers={"User-Agent": "netflix-au-ratings"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    text = gzip.decompress(raw).decode("utf-8")
    out = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        tid = row.get("tconst")
        if tid in wanted:
            try:
                out[tid] = (float(row["averageRating"]), int(row["numVotes"]))
            except (KeyError, ValueError):
                pass
    return out


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    if not TOKEN:
        raise SystemExit("TMDB_TOKEN is not set.")
    os.makedirs(OUT_DIR, exist_ok=True)

    genres = tmdb("/genre/movie/list", {"language": "en-AU"}) or {}
    genre_map = {str(g["id"]): g["name"] for g in genres.get("genres", [])}

    ids = discover_ids()
    log(f"{len(ids)} unique movies on Netflix {REGION}")
    if len(ids) < MIN_EXPECTED:
        raise SystemExit(
            f"Only {len(ids)} movies found, expected at least {MIN_EXPECTED}. "
            "Not overwriting the previous data."
        )

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        movies = [m for m in pool.map(details, ids) if m]
    log(f"Got details for {len(movies)} movies")

    ratings = imdb_ratings({m["imdb"] for m in movies if m["imdb"]})
    for m in movies:
        r, v = ratings.get(m["imdb"], (None, 0))
        m["r"], m["v"] = r, v

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    first_seen = load_json(FIRST_SEEN_PATH, {})
    first_run = not first_seen
    for m in movies:
        key = str(m["id"])
        if key not in first_seen:
            first_seen[key] = "" if first_run else today
        m["fs"] = first_seen[key]

    movies.sort(key=lambda m: (m["r"] is None, -(m["r"] or 0), -m["v"]))
    rated = sum(1 for m in movies if m["r"] is not None)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": REGION,
        "count": len(movies),
        "rated": rated,
        "genres": genre_map,
        "movies": movies,
    }
    with open(MOVIES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    with open(FIRST_SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(first_seen, f, separators=(",", ":"), sort_keys=True)
    log(f"Wrote {len(movies)} movies ({rated} with IMDb ratings) to {MOVIES_PATH}")


if __name__ == "__main__":
    main()
