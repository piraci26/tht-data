# tht-data — data backbone

Pure data backend for the THT Dual Scan project. Runs `scan.py` every 5 minutes via GitHub Actions and serves the output JSON via GitHub Pages.

**No UI.** UIs that consume this data:
- Production dashboard: <https://piraci26.github.io/tht-dashboard/>
- Dev sandbox: <https://piraci26.github.io/tht-dashboard-dev/>
- Lovable dashboard: <https://trend-iq.lovable.app/dashboard>

## Endpoints (CORS-enabled, public, no auth)

```
https://piraci26.github.io/tht-data/results.json
https://piraci26.github.io/tht-data/results_weekly.json
https://piraci26.github.io/tht-data/results_monthly.json
https://piraci26.github.io/tht-data/articles.json
https://piraci26.github.io/tht-data/bars/{SYM}.json
https://piraci26.github.io/tht-data/bars_weekly/{SYM}.json
https://piraci26.github.io/tht-data/bars_monthly/{SYM}.json
```

Every 5 minutes, GitHub Actions runs `scan.py`, computes FVB + B-Xtrender across ~1,500 US equities (mcap > $3.5B), writes the JSON files, and commits the result. GitHub Pages then deploys it globally.

## Source repo for the UI

<https://github.com/piraci26/tht-dashboard> (frontend code lives there).
