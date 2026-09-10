export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ ok: false, error: 'POST only' });
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  try {
    const r = await fetch(`${base}/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body || {}),
    });
    const j = await r.json();
    return res.status(r.status).json({ ...j, forwarded: true });
  } catch (e) {
    return res.status(502).json({ ok: false, error: `worker unreachable: ${String(e).slice(0, 200)}` });
  }
}
