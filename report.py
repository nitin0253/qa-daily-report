"""Daily QA Review Report → Slack

Pulls yesterday's review counts per QA user from one live dashboard and
posts a single combined table to a Slack channel.

Source
------
Image, 360 & Video   https://spot-qc-dashboard.spyne.ai/api/qc?product=images|360|video
                     Row shape: {day: 'YYYY-MM-DD', loginUser, n, ...}
                     "day" is IST calendar day for all three products.
                     Public, no auth (Google SSO was removed from this
                     endpoint 2026-11-02).

Yesterday
---------
Previous IST calendar date, for all three products.

Users are keyed by first name (title case). "Khushi Gautam" and
"khushi.gautam@spyne.ai" both collapse to "Khushi".

Env
---
SLACK_BOT_TOKEN   xoxb-... bot token with chat:write
SLACK_CHANNEL     channel id, default C0996AMFQRZ (the asked channel)
DRY_RUN           if set to "1", print the message instead of posting
TARGET_DATE       YYYY-MM-DD (IST calendar). Default: yesterday. Mainly for
                  manual back-fills via workflow_dispatch.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))

QC_API = "https://spot-qc-dashboard.spyne.ai/api/qc"
SLACK_API = "https://slack.com/api/chat.postMessage"
DEFAULT_CHANNEL = "C0996AMFQRZ"

# Known test / sandbox accounts the dashboards exclude. Keep in sync with
# spot-qc-dashboard's isTestUser() guard.
TEST_PATTERNS = ("claude_", "reviewersim", "test_")


_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "qa-daily-report/1.0"


def _http_get(url: str, timeout: int = 60) -> bytes:
    resp = _SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def first_name(raw: str) -> str:
    """'Khushi Gautam' or 'khushi.gautam@spyne.ai' → 'Khushi'."""
    if not raw:
        return ""
    s = raw.strip()
    if "@" in s:
        s = s.split("@", 1)[0]
    for sep in (" ", "."):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s.strip().title()


def is_test_user(raw: str) -> bool:
    s = (raw or "").strip().lower()
    return any(s.startswith(p) for p in TEST_PATTERNS)


def ist_yesterday(today_ist: date | None = None) -> date:
    now_ist = datetime.now(IST).date() if today_ist is None else today_ist
    return now_ist - timedelta(days=1)


# ── Image / 360 / Video ──────────────────────────────────────────────────
def is_issue(submit_issue: str) -> bool:
    """Same rule as the dashboard (spot-qc report.html updateStats): a row is
    an issue when its trimmed submitIssue is non-empty and not 'done'."""
    v = (submit_issue or "").strip().lower()
    return v != "" and v != "done"


def fetch_qc_counts(product: str, target_day: date) -> dict[str, tuple[int, int]]:
    """{first name: (QAed total, issues)} where day == target_day. Issues are
    the part of the total whose submitIssue passes is_issue()."""
    raw = _http_get(f"{QC_API}?product={product}")
    payload = json.loads(raw)
    want = target_day.isoformat()
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in payload.get("rows", []):
        if row.get("day") != want:
            continue
        login = row.get("loginUser") or ""
        if not login or is_test_user(login):
            continue
        key = first_name(login)
        if not key:
            continue
        n = int(row.get("n") or 0)
        counts[key][0] += n
        if is_issue(row.get("submitIssue")):
            counts[key][1] += n
    return {k: (v[0], v[1]) for k, v in counts.items()}


def pct(issues: int, total: int) -> str:
    """Issue rate to 1 decimal, as the dashboard shows it ('-' if no QAed)."""
    return f"{issues / total * 100:.1f}%" if total else "-"


# ── Report text ───────────────────────────────────────────────────────────
# Row = (user, img, img_iss, spin, spin_iss, vid, vid_iss, total_qaed)
# Totals = (img, img_iss, spin, spin_iss, vid, vid_iss, total_qaed)
Row = tuple[str, int, int, int, int, int, int, int]
Totals = tuple[int, int, int, int, int, int, int]


def build_table(
    img: dict[str, tuple[int, int]],
    spin: dict[str, tuple[int, int]],
    vid: dict[str, tuple[int, int]],
) -> tuple[list[Row], Totals]:
    users = sorted(set(img) | set(spin) | set(vid), key=lambda u: u.lower())
    rows: list[Row] = []
    t = [0] * 6
    for u in users:
        i, ii = img.get(u, (0, 0))
        s, si = spin.get(u, (0, 0))
        v, vi = vid.get(u, (0, 0))
        rows.append((u, i, ii, s, si, v, vi, i + s + v))
        for k, x in enumerate((i, ii, s, si, v, vi)):
            t[k] += x
    return rows, (t[0], t[1], t[2], t[3], t[4], t[5], t[0] + t[2] + t[4])


def render_slack_message(target_day: date, rows: list[Row], totals: Totals) -> str:
    """Monospaced code-block table so Slack renders aligned columns. Each
    product has a QAed count and an issue-rate (%) column."""
    header_date = target_day.strftime("%d %b %Y")
    lede_date = target_day.strftime("%d-%b")

    heads = ["User", "Image", "Iss%", "360", "Iss%", "Video", "Iss%", "Total"]

    def cells(name, i, ii, s_, si, v, vi, t) -> list[str]:
        return [name, str(i), pct(ii, i), str(s_), pct(si, s_),
                str(v), pct(vi, v), str(t)]

    body_rows = [cells(*r) for r in rows]
    total_row = cells("Total", *totals[:6], totals[6])
    widths = [
        max(len(heads[c]), *(len(r[c]) for r in body_rows), len(total_row[c]))
        for c in range(len(heads))
    ]

    def line(vals: list[str]) -> str:
        out = [vals[0].ljust(widths[0])]
        out += [vals[c].rjust(widths[c]) for c in range(1, len(vals))]
        return " | ".join(out)

    sep = "-+-".join("-" * w for w in widths)
    body = [line(heads), sep]
    body += [line(r) for r in body_rows]
    if rows:
        body.append(sep)
    body.append(line(total_row))

    table = "\n".join(body)
    return (
        f"*QA Daily Report — {header_date}*\n"
        f"Here's {lede_date}'s QA review summary across Image, 360 and Video. "
        f"Iss% = issues reported ÷ QAed.\n"
        f"cc: <@U03MLJZBLF5>\n"
        f"```\n{table}\n```"
    )


# ── Slack ─────────────────────────────────────────────────────────────────
SLACK_LOOKUP = "https://slack.com/api/users.lookupByEmail"


def resolve_email_to_user(email: str, token: str) -> str:
    """DM a user by email: look up their user ID; chat.postMessage then
    opens an IM for us. Requires `users:read.email` scope on the bot."""
    resp = requests.get(
        SLACK_LOOKUP,
        params={"email": email},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    try:
        data = resp.json()
    except ValueError:
        raise SystemExit(f"Slack HTTP {resp.status_code}: {resp.text[:400]}")
    if not data.get("ok"):
        raise SystemExit(f"Slack users.lookupByEmail error ({email}): {data}")
    return data["user"]["id"]


def post_to_slack(text: str, channel: str, token: str) -> dict:
    resp = requests.post(
        SLACK_API,
        json={"channel": channel, "text": text, "mrkdwn": True},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        raise SystemExit(f"Slack HTTP {resp.status_code}: {resp.text[:400]}")
    if not data.get("ok"):
        raise SystemExit(f"Slack API error: {data}")
    return data


# ── Main ──────────────────────────────────────────────────────────────────
def main() -> int:
    target_env = os.environ.get("TARGET_DATE", "").strip()
    if target_env:
        try:
            target = date.fromisoformat(target_env)
        except ValueError:
            print(f"Invalid TARGET_DATE {target_env!r}", file=sys.stderr)
            return 2
    else:
        target = ist_yesterday()

    print(f"Target day (IST): {target.isoformat()}", file=sys.stderr)

    img = fetch_qc_counts("images", target)
    spin = fetch_qc_counts("360", target)
    vid = fetch_qc_counts("video", target)

    print(
        f"Users — image:{len(img)} 360:{len(spin)} video:{len(vid)}",
        file=sys.stderr,
    )

    rows, totals = build_table(img, spin, vid)
    message = render_slack_message(target, rows, totals)

    if os.environ.get("DRY_RUN") == "1":
        print(message)
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        print("SLACK_BOT_TOKEN is not set", file=sys.stderr)
        return 1
    channel = os.environ.get("SLACK_CHANNEL", DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
    if "@" in channel:
        channel = resolve_email_to_user(channel, token)

    resp = post_to_slack(message, channel, token)
    print(f"Posted text to {channel} (ts={resp.get('ts')})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
