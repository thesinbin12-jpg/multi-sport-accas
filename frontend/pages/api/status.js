export default async function handler(req, res) {
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  try {
    const r = await fetch(`${base}/status`);
    const j = await r.json();
    return res.status(r.status).json(j);
  } catch (e) {
    return res.status(502).json({ status: 'error', message: `worker unreachable: ${String(e).slice(0, 200)}` });
  }
}
