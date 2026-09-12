export default async function handler(req, res) {
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  const headers = {};
  if (process.env.WORKER_SECRET) headers['x-accas-secret'] = process.env.WORKER_SECRET;
  try {
    let url;
    if (req.query.diag) {
      const q = `home=${encodeURIComponent(req.query.home || '')}&away=${encodeURIComponent(req.query.away || '')}&league=${encodeURIComponent(req.query.league || '')}&date=${encodeURIComponent(req.query.date || '')}`;
      url = `${base}/diag?${q}`;
    } else {
      url = `${base}/logs?tail=${encodeURIComponent(req.query.tail || '200')}`;
    }
    const r = await fetch(url, { headers });
    return res.status(r.status).json(await r.json());
  } catch (e) {
    return res.status(502).json({ ok: false, error: `worker unreachable: ${String(e).slice(0, 200)}` });
  }
}
