#!/bin/zsh
# Runs the scan and pushes updated results.json to GitHub.
# Called by launchd every 5 minutes.

cd "$HOME/tht-dashboard" || exit 1

# Ensure Python + git are on PATH for launchd's barebones environment
export PATH="$HOME/.homebrew/bin:$HOME/.homebrew/Caskroom/miniconda/base/bin:/usr/bin:/bin:/usr/local/bin"

# Run the scan with the miniconda python (3.14 from homebrew lacks SSL CA bundle)
"$HOME/.homebrew/Caskroom/miniconda/base/bin/python3" scan.py >> /tmp/tht-scan.log 2>&1

# Commit + push all changed scan outputs (results + bars across all timeframes)
if [[ -n $(git status --porcelain docs/) ]]; then
    TS=$(date -u +"%Y-%m-%d %H:%M UTC")
    git add docs/results.json docs/results_weekly.json docs/results_monthly.json \
            docs/bars docs/bars_weekly docs/bars_monthly
    git commit -m "scan @ $TS" -q
    git push -q 2>> /tmp/tht-scan.log
fi
