// Vercel Cron fires this at 21:30 UTC daily (= 03:00 IST), per vercel.json.
// It POSTs a workflow_dispatch to the daily.yml GitHub Actions workflow,
// which does the actual data fetch + Slack post.
//
// Env vars (set in Vercel project settings):
//   GH_PAT       — fine-grained PAT with Actions: write on nitin0253/qa-daily-report
//   CRON_SECRET  — optional; if set, inbound requests must send it as
//                  `Authorization: Bearer <CRON_SECRET>`. Vercel Cron sends
//                  this header automatically when the env var is set.

export default async function handler(req, res) {
  const cronSecret = process.env.CRON_SECRET;
  if (cronSecret) {
    const auth = req.headers['authorization'] || '';
    if (auth !== `Bearer ${cronSecret}`) {
      return res.status(401).json({ error: 'Unauthorized' });
    }
  }

  const pat = process.env.GH_PAT;
  if (!pat) {
    return res.status(500).json({ error: 'GH_PAT env var not set' });
  }

  try {
    const r = await fetch(
      'https://api.github.com/repos/nitin0253/qa-daily-report/actions/workflows/daily.yml/dispatches',
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${pat}`,
          'Accept': 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'Content-Type': 'application/json',
          'User-Agent': 'qa-daily-report-cron',
        },
        body: JSON.stringify({ ref: 'main' }),
      }
    );
    const text = await r.text();
    return res.status(r.ok ? 200 : 502).json({
      gh_status: r.status,
      gh_body: text.slice(0, 400),
      fired_at: new Date().toISOString(),
    });
  } catch (err) {
    return res.status(500).json({ error: String(err) });
  }
}
