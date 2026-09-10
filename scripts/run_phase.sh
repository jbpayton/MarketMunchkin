#!/usr/bin/env bash
# cron-friendly wrapper: scripts/run_phase.sh premarket|intraday|postmarket|research
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/munchkin run --phase "${1:-intraday}" --quiet >> "logs/cron-$(date +%F).log" 2>&1
