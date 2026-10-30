# Netflix AU ratings

A weekly list of every movie on Netflix Australia, with IMDb ratings, and a page to filter it.

- `scripts/build.py` pulls the Netflix AU catalogue from TMDB and joins IMDb's public ratings file.
- `.github/workflows/refresh.yml` runs it every Friday morning (Sydney) and commits `data/movies.json`.
- `index.html` is the filter page. It reads `data/movies.json` from the same folder.

Setup: add a repository secret `TMDB_TOKEN` with your TMDB API Read Access Token, then run the workflow once from the Actions tab.

Data: availability and details from [TMDB](https://www.themoviedb.org) (powered by JustWatch). This product uses the TMDB API but is not endorsed or certified by TMDB. Ratings from the [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/), for personal use.
