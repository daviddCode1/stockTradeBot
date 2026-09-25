#!/usr/bin/env bash
# Run one bot command from cron with the project virtualenv.
# Usage: ./scripts/run_daily.sh cycle|protect|reconcile|status
set -euo pipefail                                   # stop on errors / unset variables
cd "$(dirname "$0")/.."                             # project root
source .venv/bin/activate                           # project Python + dependencies
mkdir -p logs                                       # log folder (git-ignored)
python -m src.main "${1:-cycle}" >> "logs/cron_$(date +%F).log" 2>&1   # append output to a dated log
