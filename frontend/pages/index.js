import { useEffect, useRef, useState } from 'react';

export default function Home() {
  const [status, setStatus] = useState({ status: 'idle', message: 'never built' });
  const [accas, setAccas] = useState([]);
  const [accuracy, setAccuracy] = useState(null);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState('');
  const pollRef = useRef(null);

  const fetchStatus = async () => {
    try {
      const r = await fetch('/api/status');
      const j = await r.json();
      setStatus(j);
      if (j.status === 'done' || j.status === 'error' || j.status === 'idle') {
        setBuilding(false);
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
        fetchAccas();
      }
    } catch (e) { /* keep polling */ }
  };

  const fetchAccas = async () => {
    try {
      const r = await fetch('/api/accas');
      const j = await r.json();
      if (j.ok) {
        setAccas(j.tickets || []);
        setAccuracy(j.accuracy || null);
      }
    } catch (e) { setError('Failed to load accas'); }
  };

  useEffect(() => { fetchStatus(); fetchAccas(); return () => pollRef.current && clearInterval(pollRef.current); }, []);

  const startBuild = async () => {
    setError('');
    setBuilding(true);
    try {
      const r = await fetch('/api/build', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
      const j = await r.json();
      if (!j.ok && !j.forwarded) { setError(j.error || 'Build failed to start'); setBuilding(false); return; }
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(fetchStatus, 3000);
      fetchStatus();
    } catch (e) { setError(String(e)); setBuilding(false); }
  };

  return (
    <main style={{ maxWidth: 900, margin: '0 auto', padding: 24, fontFamily: 'system-ui, sans-serif' }}>
      <h1>Multi-Sport Accumulators</h1>
      <p>Status: <b>{status.status}</b> — {status.message || ''}</p>
      {status.tickets_in_db != null && <p>Tickets in DB: {status.tickets_in_db}</p>}
      {accuracy && <p>Verified: {accuracy.verified_tickets} · Won: {accuracy.won_tickets} · Acc: {(accuracy.accuracy * 100).toFixed(1)}%</p>}
      <button onClick={startBuild} disabled={building} style={{ padding: '12px 28px', fontSize: 16, cursor: building ? 'wait' : 'pointer' }}>
        {building ? 'Building…' : 'Build'}
      </button>
      {error && <p style={{ color: 'red' }}>{error}</p>}
      <hr style={{ margin: '24px 0' }} />
      {accas.length === 0 && <p>No accumulators yet — hit Build.</p>}
      {accas.map((t) => (
        <section key={t.id} style={{ border: '1px solid #ddd', borderRadius: 8, padding: 16, marginBottom: 16 }}>
          <h3 style={{ margin: '0 0 4px' }}>{t.id} <span style={{ color: '#555' }}>· {t.status}</span></h3>
          <p>Combined odds: <b>{t.combined_odds}</b> · {t.created_at}</p>
          <table style={{ width: '100%', borderCollapse: 'collapse' }} cellPadding={6}>
            <thead><tr style={{ textAlign: 'left', borderBottom: '2px solid #ddd' }}>
              <th>Match</th><th>League</th><th>Pick</th><th>Odds</th><th>Prob</th><th>Result</th>
            </tr></thead>
            <tbody>
              {(t.legs || []).map((l, i) => (
                <tr key={i} style={{ borderBottom: '1px solid #eee' }}>
                  <td>{l.match}</td><td>{l.league}</td><td>{l.selection}</td>
                  <td>{l.odds}</td><td>{l.probability}</td><td>{l.result}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </main>
  );
}
