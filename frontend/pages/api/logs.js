export default async function handler(req, res) {
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  const headers = {};
  if (process.env.WORKER_SECRET) headers['x-accas-secret'] = process.env.WORKER_SECRET;
  try {
    const r = await fetch(`${base}/logs?tail=${encodeURIComponent(req.query.tail || '200')}`, { headers });
    return res.status(r.status).json(await r.json());
  } catch (e) {
    return res.status(502).json({ ok: false, error: `worker unreachable: ${String(e).slice(0, 200)}` });
  }
}
