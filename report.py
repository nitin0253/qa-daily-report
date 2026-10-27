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

PDF_FILENAME_PREFIX = "qa-daily-report"

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
    lede_date = target_day.strftime("%d-%b")

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
        f"Here's {lede_date}'s QA review summary across Image, 360 and Video.\n"
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


# ── PDF rendering ────────────────────────────────────────────────────────
PDF_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: {page_w}mm {page_h}mm; margin: 5mm 10mm; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{ font-family: 'Helvetica Neue', 'Arial', sans-serif; color: #1a1f2b;
          line-height: 1.2; }}
  h1 {{ font-size: 22pt; font-weight: 800; margin: 0 0 2pt; letter-spacing: -0.02em;
        line-height: 1.1; }}
  .date {{ color: #5c6473; font-size: 10.5pt; margin: 0 0 5pt; }}
  .lede {{ font-size: 10pt; margin: 0 0 8pt; }}
  table {{ width: 100%; border-collapse: separate; border-spacing: 0;
           font-size: 10pt; border: 1px solid #e3e6ec; }}
  thead th {{ background: #3b4557; color: #fff; font-weight: 700;
              text-align: center; padding: 6pt 10pt;
              border-bottom: 1px solid #e3e6ec; }}
  thead th:first-child {{ text-align: left; }}
  thead th.total {{ background: #3f5230; }}
  tbody td {{ padding: 6pt 10pt; border-bottom: 1px solid #e3e6ec;
              text-align: center; }}
  tbody td:first-child {{ text-align: left; }}
  tbody td.total {{ background: #dfe8d0; font-weight: 600; }}
  tbody tr.sum td {{ background: #c6d5dc; color: #16202b; font-weight: 700;
                     border-bottom: none; }}
  tbody tr.sum td.total {{ background: #c6d5dc; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  .footer {{ margin-top: 6pt; margin-bottom: 0; color: #5c6473;
             font-size: 8.5pt; font-style: italic; }}
</style></head><body>
<h1>QA Daily Report</h1>
<p class="date">{date_full}</p>
<p class="lede">Here's {lede_date}'s QA review summary across Image, 360 and Video.<br>cc: @Praveen Agarwal</p>
<table>
<thead><tr><th>User</th><th>Image</th><th>360</th><th>Video</th><th class="total">Total</th></tr></thead>
<tbody>
{rows_html}
<tr class="sum"><td>Total</td><td>{ti}</td><td>{ts}</td><td>{tv}</td><td class="total">{tt}</td></tr>
</tbody></table>
<p class="footer">Generated {generated_at} &middot; Sources: spot-qc-dashboard (Image + 360) &middot; SpotQC-QA-Report (Video)</p>
</body></html>"""


def _fmt_int(n: int) -> str:
    return f"{int(n):,}"


def render_pdf_bytes(
    target_day: date,
    rows: list[tuple[str, int, int, int, int]],
    totals: tuple[int, int, int, int],
) -> bytes:
    """Render the same table used in the artifact as a one-page PDF."""
    from weasyprint import HTML  # imported lazily so text-only runs stay light

    date_full = target_day.strftime("%A, %d %B %Y")
    lede_date = target_day.strftime("%d-%b")
    generated_at = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    rows_html = "\n".join(
        f"<tr><td>{u}</td><td>{_fmt_int(i)}</td><td>{_fmt_int(s)}</td>"
        f"<td>{_fmt_int(v)}</td><td class=\"total\">{_fmt_int(t)}</td></tr>"
        for u, i, s, v, t in rows
    )
    ti, ts, tv, tt = totals

    # Content-fit page: fixed width that holds the table comfortably, height
    # grown per data row so the PDF doesn't leave an A4-sized blank below.
    # Base covers 5mm top + 5mm bottom margin + ~55mm chrome
    # (title + date + lede + table head + totals row + footer).
    # Each data row adds ~9mm at the current 10pt / 6pt-padding settings.
    page_w = 190
    page_h = 70 + 9 * max(len(rows), 1)

    html = PDF_HTML.format(
        page_w=page_w,
        page_h=page_h,
        date_full=date_full,
        lede_date=lede_date,
        rows_html=rows_html,
        ti=_fmt_int(ti),
        ts=_fmt_int(ts),
        tv=_fmt_int(tv),
        tt=_fmt_int(tt),
        generated_at=generated_at,
    )
    return HTML(string=html).write_pdf()


# ── Slack file upload (new three-step external-upload flow) ──────────────
SLACK_GET_UPLOAD_URL = "https://slack.com/api/files.getUploadURLExternal"
SLACK_COMPLETE_UPLOAD = "https://slack.com/api/files.completeUploadExternal"
SLACK_CONVERSATIONS_OPEN = "https://slack.com/api/conversations.open"


def open_im_channel(user_id: str, token: str) -> str:
    """Resolve a `U…` user ID to a `D…` IM channel ID (files.complete needs
    a conversation id, not a user id). Needs `im:write` on the bot."""
    resp = requests.post(
        SLACK_CONVERSATIONS_OPEN,
        json={"users": user_id},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise SystemExit(f"conversations.open: {data}")
    return data["channel"]["id"]


def upload_file_to_slack(
    file_bytes: bytes,
    filename: str,
    content_type: str,
    title: str,
    initial_comment: str,
    channel: str,
    token: str,
) -> dict:
    """Upload a file to Slack and share it in `channel`. Needs `files:write`."""
    headers = {"Authorization": f"Bearer {token}"}

    r1 = requests.get(
        SLACK_GET_UPLOAD_URL,
        params={"filename": filename, "length": len(file_bytes)},
        headers=headers,
        timeout=30,
    )
    d1 = r1.json()
    if not d1.get("ok"):
        raise SystemExit(f"files.getUploadURLExternal: {d1}")

    r2 = requests.post(
        d1["upload_url"],
        files={"file": (filename, file_bytes, content_type)},
        timeout=60,
    )
    if r2.status_code != 200:
        raise SystemExit(f"Slack upload POST {r2.status_code}: {r2.text[:400]}")

    payload = {
        "files": [{"id": d1["file_id"], "title": title}],
        "channel_id": channel,
    }
    if initial_comment:
        payload["initial_comment"] = initial_comment

    r3 = requests.post(
        SLACK_COMPLETE_UPLOAD,
        json=payload,
        headers={**headers, "Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )
    d3 = r3.json()
    if not d3.get("ok"):
        raise SystemExit(f"files.completeUploadExternal: {d3}")
    return d3


def render_jpg_bytes(pdf_bytes: bytes, dpi: int = 180) -> bytes:
    """Rasterize page 1 of the PDF to a JPEG so the same artwork can be shared
    inline in Slack (previews better than a PDF in a DM)."""
    import pypdfium2 as pdfium  # imported lazily
    from io import BytesIO

    pdf = pdfium.PdfDocument(pdf_bytes)
    page = pdf[0]
    pil = page.render(scale=dpi / 72).to_pil().convert("RGB")
    buf = BytesIO()
    pil.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()


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
    if "@" in channel:
        channel = resolve_email_to_user(channel, token)

    fmt_raw = (os.environ.get("REPORT_FORMAT") or "text").strip().lower()
    formats = {f.strip() for f in fmt_raw.split(",") if f.strip()}
    valid = {"text", "pdf", "jpg"}
    unknown = formats - valid
    if unknown or not formats:
        print(
            f"Invalid REPORT_FORMAT {fmt_raw!r}; use any comma-separated subset of {sorted(valid)}",
            file=sys.stderr,
        )
        return 2

    # files.completeUploadExternal wants a conversation id, not a user id.
    file_channel = (
        open_im_channel(channel, token) if channel.startswith("U") else channel
    )
    date_label = target.strftime("%d %b %Y")
    lede_date = target.strftime("%d-%b")
    lead_comment = (
        f"*QA Daily Report — {date_label}*\n"
        f"Here's {lede_date}'s QA review summary across Image, 360 and Video.\n"
        f"cc: <@U03MLJZBLF5>"
    )
    # Only one post or upload carries the lead comment, so the DM shows the
    # intro once instead of once per file.
    comment_used = False

    if "text" in formats:
        resp = post_to_slack(message, channel, token)
        print(f"Posted text to {channel} (ts={resp.get('ts')})", file=sys.stderr)
        comment_used = True

    pdf = None
    if "pdf" in formats or "jpg" in formats:
        pdf = render_pdf_bytes(target, rows, totals)

    if "pdf" in formats:
        filename = f"{PDF_FILENAME_PREFIX}-{target.isoformat()}.pdf"
        title = f"QA Daily Report — {date_label}"
        comment = "" if comment_used else lead_comment
        upload_file_to_slack(
            pdf, filename, "application/pdf", title, comment, file_channel, token,
        )
        print(f"Uploaded PDF to {file_channel} ({len(pdf)} bytes)", file=sys.stderr)
        comment_used = True

    if "jpg" in formats:
        jpg = render_jpg_bytes(pdf)
        filename = f"{PDF_FILENAME_PREFIX}-{target.isoformat()}.jpg"
        title = f"QA Daily Report — {date_label}"
        comment = "" if comment_used else lead_comment
        upload_file_to_slack(
            jpg, filename, "image/jpeg", title, comment, file_channel, token,
        )
        print(f"Uploaded JPG to {file_channel} ({len(jpg)} bytes)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
