# QA Daily Report → Slack

Posts yesterday's QA reviewer counts (Image + 360 + Video) as one table to a
Slack channel, every day at **03:15 IST**.

```
*QA Daily Report — 14 Oct 2026*
User   | Image | 360 | Video | Total
-------+-------+-----+-------+------
Anup   |  3614 |   0 |    65 |  3679
Khushi |  4008 | 301 |     0 |  4309
Vijay  |  2802 | 115 |   120 |  3037
-------+-------+-----+-------+------
Total  | 10424 | 416 |   185 | 11025
```

## Sources

| Column | Dashboard | Underlying feed | "Yesterday" |
|---|---|---|---|
| Image | [spot-qc-dashboard](https://spot-qc-dashboard.vercel.app/) → "By Login User" | `/api/qc?product=images` | IST calendar day |
| 360 | spot-qc-dashboard (product filter 360-Spin) → "By Login User" | `/api/qc?product=360` | IST calendar day |
| Video | [SpotQC-QA-Report](https://nitin0253.github.io/SpotQC-QA-Report/) → "QA Reviewer Breakdown" | Published Google Sheet CSV | **06:00 IST shift day** |

Users are keyed by first name: `Khushi Gautam` and `khushi.gautam@spyne.ai`
both collapse to `Khushi`.

## Setup

1. Push this repo to GitHub.
2. In the repo → **Settings → Secrets and variables → Actions**, add:

   | Secret | Value |
   |---|---|
   | `SLACK_BOT_TOKEN` | `xoxb-...` from the Spyne QC Slack app. Needs `chat:write`. |
   | `SLACK_CHANNEL`   | `C0996AMFQRZ` (the target channel). |

   Invite the bot into the channel: in Slack, `/invite @<bot-name>` in the
   target channel, otherwise `chat.postMessage` returns `not_in_channel`.

3. The workflow is already scheduled for 21:45 UTC = 03:15 IST next day.

## Manual run / back-fill

Actions → **QA Daily Report → Slack** → **Run workflow**. Optional inputs:

- `target_date` — `YYYY-MM-DD` IST calendar date to report for. Blank =
  yesterday.
- `dry_run` — `true` to print the message in the Actions log instead of
  posting to Slack.

Or locally:

```bash
pip install -r requirements.txt
DRY_RUN=1 python report.py                   # yesterday
TARGET_DATE=2026-10-14 DRY_RUN=1 python report.py
```

## Why the "day" differs by column

The image/360 dashboard groups by IST calendar day. The video dashboard runs
on QC shift days (06:00 IST → next 06:00 IST), so a row with
`qa_timestamp = 2026-10-15T05:30:00+05:30` still counts as the Oct 14 shift
day. The script matches each source's own definition — same numbers the user
sees when they pick "Yesterday" on each dashboard.
