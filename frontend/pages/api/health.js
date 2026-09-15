// Keepalive proxy: cron-job.org pings this (Vercel reachable from anywhere);
// the fetch to the Render worker /dbping wakes Neon's auto-suspended compute
// AND keeps the worker from spinning down (/health never touches the DB, so
// Neon still suspended nightly and the learn wedged on a zombie connection).
// Open on purpose. 55s abort survives Render free-tier cold boots.
export default async function handler(req, res) {
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 55000);
  try {
    const r = await fetch(`${base}/dbping`, { signal: ctl.signal });
    const j = await r.json().catch(() => ({}));
    return res.status(r.status).json({ ok: !!j.ok, db: !!j.db, via: 'vercel' });
  } catch (e) {
    return res.status(502).json({ ok: false, via: 'vercel', error: String(e).slice(0, 120) });
  } finally {
    clearTimeout(t);
  }
}

export const config = { maxDuration: 60 };
