#!/bin/zsh
# Rebuilds the $3.5B universe + market-cap cache, then pushes them to GitHub.
# Called by launchd (com.iqalgo.universe-builder) at 20:50 London.
#
# The push is the whole point: the cloud scan.py Action reads the COMMITTED
# docs/mcap_cache.json. Rebuilding it locally without pushing lets the cloud
# copy go stale, and scan.py then falls back to the price x shares_outstanding
# calc, which returns ~100x-wrong caps for ADRs (BCH, HDB, CUK).

cd "$HOME/tht-data" || exit 1

# launchd gives us a barebones environment — put python + git on PATH.
export PATH="$HOME/.homebrew/bin:$HOME/.homebrew/Caskroom/miniconda/base/bin:/usr/bin:/bin:/usr/local/bin"

LOG=/Users/kubakaszynski/tht-data/logs/universe-builder.log
PY="$HOME/.homebrew/Caskroom/miniconda/base/bin/python3"

"$PY" build_universe_3_5b.py || { echo "[$(date -u)] build failed — not pushing" >> "$LOG"; exit 1; }

# Only the two files this job owns. Naming them explicitly keeps the local
# regime_state.json / setups.json churn (written by the regime scan) out of
# the commit.
FILES=(docs/mcap_cache.json docs/universe_filtered.json)

if [[ -z $(git status --porcelain $FILES) ]]; then
    echo "[$(date -u)] universe cache unchanged — nothing to push" >> "$LOG"
    exit 0
fi

git add $FILES
git -c commit.gpgsign=false commit -q -m "universe: refresh mcap cache @ $(date -u +'%Y-%m-%d %H:%M UTC')"

# The cloud Action commits scan results every ~5 min, so our push races it.
# --autostash keeps the rebase from tripping over unrelated dirty files.
for attempt in 1 2 3; do
    git pull --rebase --autostash -q 2>> "$LOG" && git push -q 2>> "$LOG" && {
        echo "[$(date -u)] mcap cache pushed (attempt $attempt)" >> "$LOG"
        exit 0
    }
    sleep 10
done

echo "[$(date -u)] ERROR: could not push mcap cache after 3 attempts" >> "$LOG"
exit 1
