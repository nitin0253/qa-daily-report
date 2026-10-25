#!/bin/bash
# Fires a repository_dispatch on nitin0253/qa-daily-report so the GH
# Actions workflow runs and posts yesterday's QA report to Slack.
# Called by the LaunchAgent at ~/Library/LaunchAgents/ai.spyne.qa-daily-report.plist
# at 03:00 IST every day. Uses the gh CLI's existing auth; no PAT needed.
set -euo pipefail

export PATH="/Users/nitinkumar/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

log_dir="/Users/nitinkumar/Library/Logs/qa-daily-report"
mkdir -p "$log_dir"
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [[ "${1:-}" == "--dry-run" ]]; then
  payload='{"event_type":"daily-report","client_payload":{"dry_run":"true"}}'
else
  payload='{"event_type":"daily-report"}'
fi

echo "[$ts] POSTing repository_dispatch (payload=$payload)" >> "$log_dir/trigger.log"

gh api repos/nitin0253/qa-daily-report/dispatches \
  -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  --input - <<<"$payload" \
  >> "$log_dir/trigger.log" 2>&1

echo "[$ts] dispatch POST exit=$?" >> "$log_dir/trigger.log"
