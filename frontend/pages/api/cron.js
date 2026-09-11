export default async function handler(req, res) {
  const auth = req.headers.authorization || '';
  if (process.env.CRON_SECRET && auth !== `Bearer ${process.env.CRON_SECRET}`) {
    return res.status(401).json({ ok: false, error: 'unauthorized cron' });
  }
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  const headers = { 'Content-Type': 'application/json' };
  if (process.env.WORKER_SECRET) headers['x-accas-secret'] = process.env.WORKER_SECRET;
  const isWeekly = (req.query.kind || '') === 'weekly';
  try {
    if (isWeekly) {
      const r = await fetch(`${base}/build`, {
        method: 'POST', headers,
        body: JSON.stringify({ kind: 'weekly', max_legs: 20, use_ai: true }),
      });
      return res.status(r.status).json(await r.json());
    }
    const r = await fetch(`${base}/learn`, { method: 'POST', headers, body: '{}' });
    return res.status(r.status).json(await r.json());
  } catch (e) {
    return res.status(502).json({ ok: false, error: `worker unreachable: ${String(e).slice(0, 200)}` });
  }
}
