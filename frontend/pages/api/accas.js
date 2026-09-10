export default async function handler(req, res) {
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  const limit = req.query.limit || '20';
  try {
    const r = await fetch(`${base}/accas?limit=${encodeURIComponent(limit)}`);
    const j = await r.json();
    return res.status(r.status).json(j);
  } catch (e) {
    return res.status(502).json({ ok: false, error: `worker unreachable: ${String(e).slice(0, 200)}`, tickets: [] });
  }
}
