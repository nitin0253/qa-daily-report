// Vercel Cron fires this at 21:30 UTC daily (= 03:00 IST), per vercel.json.
// It POSTs a workflow_dispatch to the daily.yml GitHub Actions workflow,
// which does the actual data fetch + Slack post.
//
// Env vars (set in Vercel project settings):
//   GH_PAT               — fine-grained PAT with Actions: write on
//                          nitin0253/qa-daily-report
//   SLACK_ALERT_WEBHOOK  — Slack incoming webhook; if the GH dispatch
//                          returns anything other than HTTP 204, we POST
//                          an alert message to this webhook so the
//                          failure is loud, not silent
//   CRON_SECRET          — optional; if set, inbound requests must send it
//                          as `Authorization: Bearer <CRON_SECRET>`.
//                          Vercel Cron includes this automatically when
//                          the env var is defined.

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
    await alertSlack('🚨 qa-daily-report cron: GH_PAT env var not set in Vercel');
    return res.status(500).json({ error: 'GH_PAT env var not set' });
  }

  const firedAt = new Date().toISOString();
  let ghStatus = 0;
  let ghBody = '';
  let fetchErr = null;

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
    ghStatus = r.status;
    ghBody = (await r.text()).slice(0, 400);
  } catch (err) {
    fetchErr = String(err);
  }

  // GitHub returns 204 No Content on a successful workflow_dispatch.
  // Anything else means the trigger did not fire.
  if (ghStatus !== 204) {
    await alertSlack(
      `🚨 *QA daily report cron failed to trigger the workflow*\n` +
      `• HTTP: \`${ghStatus || 'no response'}\`\n` +
      `• fetch error: \`${fetchErr || '(none)'}\`\n` +
      `• GH body: \`${ghBody || '(empty)'}\`\n` +
      `• fired at: \`${firedAt}\`\n` +
      `• debug: https://vercel.com/nitinkumar-1873s-projects/qa-daily-report/crons`
    );
    return res.status(502).json({
      gh_status: ghStatus,
      gh_body: ghBody,
      fetch_err: fetchErr,
      fired_at: firedAt,
    });
  }

  return res.status(200).json({ gh_status: ghStatus, fired_at: firedAt });
}

async function alertSlack(message) {
  const url = process.env.SLACK_ALERT_WEBHOOK;
  if (!url) return;
  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: message }),
    });
  } catch (err) {
    // Alerting itself failed; nothing more we can do server-side.
    console.error('alert webhook POST failed:', err);
  }
}
