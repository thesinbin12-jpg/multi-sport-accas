// Keepalive proxy: cron-job.org pings this (Vercel reachable from anywhere);
// the fetch to the Render worker keeps it from spinning down. Open on purpose
// (worker /health is public). 55s abort survives Render free-tier cold boots.
export default async function handler(req, res) {
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 55000);
  try {
    const r = await fetch(`${base}/health`, { signal: ctl.signal });
    const j = await r.json().catch(() => ({}));
    return res.status(r.status).json({ ok: !!j.ok, via: 'vercel' });
  } catch (e) {
    return res.status(502).json({ ok: false, via: 'vercel', error: String(e).slice(0, 120) });
  } finally {
    clearTimeout(t);
  }
}

export const config = { maxDuration: 60 };
