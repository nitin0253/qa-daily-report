"""Daily QA Review Report → Slack

Pulls yesterday's review counts per QA user from three live dashboards and
posts a single combined table to a Slack channel.

Sources
-------
Image & 360   https://spot-qc-dashboard.vercel.app/api/qc?product=images|360
              Row shape: {day: 'YYYY-MM-DD', loginUser, n, ...}
              "day" is IST calendar day.

Video         Published Google Sheet CSV behind
              https://nitin0253.github.io/SpotQC-QA-Report/
              Row shape: ..., qa_status, qa_reviewer, qa_timestamp, ...
              A "day" on that dashboard is the QC shift day:
              06:00 IST → next 06:00 IST (per SpotQC-QA-Report decisions).

Yesterday
---------
Image/360: previous IST calendar date.
Video:    previous shift day = [yesterday 06:00 IST, today 06:00 IST).

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

import csv
import io
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))

QC_API = "https://spot-qc-dashboard.vercel.app/api/qc"
VIDEO_CSV = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vQQviNsOTXM2LlIs5ipLiil6Nox9r6BH2o-7-WtgLbTKDIltzZlHXxnlWYm7KkJLfr1JsDM69wyevuT"
    "/pub?output=csv"
)
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


def shift_day_window_utc(day: date) -> tuple[datetime, datetime]:
    """Return [start, end) in UTC for the shift day = [day 06:00 IST, day+1 06:00 IST)."""
    start_ist = datetime(day.year, day.month, day.day, 6, 0, tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


# ── Image / 360 ───────────────────────────────────────────────────────────
def fetch_qc_counts(product: str, target_day: date) -> dict[str, int]:
    """Sum `n` grouped by first name of `loginUser` where day == target_day."""
    raw = _http_get(f"{QC_API}?product={product}")
    payload = json.loads(raw)
    want = target_day.isoformat()
    counts: dict[str, int] = defaultdict(int)
    for row in payload.get("rows", []):
        if row.get("day") != want:
            continue
        login = row.get("loginUser") or ""
        if not login or is_test_user(login):
            continue
        key = first_name(login)
        if not key:
            continue
        counts[key] += int(row.get("n") or 0)
    return dict(counts)


# ── Video ─────────────────────────────────────────────────────────────────
def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    # The sheet uses ISO with trailing Z. Py 3.11+ handles 'Z' in fromisoformat.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_video_counts(target_day: date) -> dict[str, int]:
    """Count rows per QA reviewer where qa_timestamp falls in the shift day
    window and qa_status is non-empty (i.e. actually reviewed)."""
    raw = _http_get(VIDEO_CSV).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    start_utc, end_utc = shift_day_window_utc(target_day)
    counts: dict[str, int] = defaultdict(int)
    for row in reader:
        status = (row.get("qa_status") or "").strip()
        if not status:
            continue
        reviewer = (row.get("qa_reviewer") or "").strip()
        if not reviewer or is_test_user(reviewer):
            continue
        ts = _parse_ts(row.get("qa_timestamp") or "")
        if ts is None or not (start_utc <= ts < end_utc):
            continue
        counts[first_name(reviewer)] += 1
    return dict(counts)


# ── Report text ───────────────────────────────────────────────────────────
def build_table(
    img: dict[str, int],
    spin: dict[str, int],
    vid: dict[str, int],
) -> tuple[list[tuple[str, int, int, int, int]], tuple[int, int, int, int]]:
    users = sorted(set(img) | set(spin) | set(vid), key=lambda u: u.lower())
    rows: list[tuple[str, int, int, int, int]] = []
    ti = ts = tv = 0
    for u in users:
        i, s, v = img.get(u, 0), spin.get(u, 0), vid.get(u, 0)
        rows.append((u, i, s, v, i + s + v))
        ti += i
        ts += s
        tv += v
    return rows, (ti, ts, tv, ti + ts + tv)


def render_slack_message(
    target_day: date,
    rows: list[tuple[str, int, int, int, int]],
    totals: tuple[int, int, int, int],
) -> str:
    """Monospaced code-block table so Slack renders aligned columns."""
    header_date = target_day.strftime("%d %b %Y")

    name_w = max([len("User")] + [len(r[0]) for r in rows] + [len("Total")])
    img_w = max(len("Image"), *(len(str(r[1])) for r in rows), len(str(totals[0])))
    spin_w = max(len("360"), *(len(str(r[2])) for r in rows), len(str(totals[1])))
    vid_w = max(len("Video"), *(len(str(r[3])) for r in rows), len(str(totals[2])))
    tot_w = max(len("Total"), *(len(str(r[4])) for r in rows), len(str(totals[3])))

    def line(name: str, i: str, s: str, v: str, t: str) -> str:
        return (
            f"{name.ljust(name_w)} | "
            f"{i.rjust(img_w)} | "
            f"{s.rjust(spin_w)} | "
            f"{v.rjust(vid_w)} | "
            f"{t.rjust(tot_w)}"
        )

    sep = (
        "-" * name_w + "-+-" + "-" * img_w + "-+-" + "-" * spin_w +
        "-+-" + "-" * vid_w + "-+-" + "-" * tot_w
    )

    body = [line("User", "Image", "360", "Video", "Total"), sep]
    for u, i, s, v, t in rows:
        body.append(line(u, str(i), str(s), str(v), str(t)))
    if rows:
        body.append(sep)
    ti, ts, tv, tt = totals
    body.append(line("Total", str(ti), str(ts), str(tv), str(tt)))

    table = "\n".join(body)
    return (
        f"*QA Daily Report — {header_date}*\n"
        f"QA reviewed counts for yesterday (Image & 360 = IST calendar day, "
        f"Video = 06:00 IST shift day).\n"
        f"```\n{table}\n```"
    )


# ── Slack ─────────────────────────────────────────────────────────────────
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
    vid = fetch_video_counts(target)

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

    resp = post_to_slack(message, channel, token)
    print(f"Posted to {channel} (ts={resp.get('ts')})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
