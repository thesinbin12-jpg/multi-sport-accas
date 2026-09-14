export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).json({ ok: false, error: 'POST only' });
  // Gate: manual builds burn ~100 LLM calls + an hour of worker time, so require
  // the shared cron/build secret. Fail closed (deny when unset or mismatch).
  const auth = req.headers.authorization || '';
  if (!process.env.CRON_SECRET || auth !== `Bearer ${process.env.CRON_SECRET}`) {
    return res.status(401).json({ ok: false, error: 'unauthorized build: missing or wrong build key' });
  }
  const base = process.env.WORKER_URL || 'http://localhost:8000';
  const headers = { 'Content-Type': 'application/json' };
  if (process.env.WORKER_SECRET) headers['x-accas-secret'] = process.env.WORKER_SECRET;
  try {
    const r = await fetch(`${base}/build`, {
      method: 'POST',
      headers,
      body: JSON.stringify(req.body || {}),
    });
    const j = await r.json();
    return res.status(r.status).json({ ...j, forwarded: true });
  } catch (e) {
    return res.status(502).json({ ok: false, error: `worker unreachable: ${String(e).slice(0, 200)}` });
  }
}
