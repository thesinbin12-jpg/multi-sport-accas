import { useState, useEffect, useRef } from 'react';

const FILTERS = ['All', 'Pending', 'Won', 'Lost'];
const KINDS = [
  { id: 'daily', label: 'Daily', blurb: '4–6 legs, best value today. Settles fast.' },
  { id: 'weekly', label: 'Weekly', blurb: 'Up to 8 legs, bigger odds, settles over the week.' },
];

function shortId(id) {
  return String(id || '').replace(/[^a-zA-Z0-9]/g, '').slice(0, 6).toUpperCase() || '—';
}

function fmtDate(iso) {
  if (!iso) return 'Undated';
  const d = new Date(iso);
  if (isNaN(d)) return 'Undated';
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
}

function fmtOdds(x) {
  const n = Number(x);
  return isNaN(n) ? '—' : n.toFixed(2);
}

function todayName() {
  return new Date().toLocaleDateString('en-GB', { weekday: 'long' });
}

export default function Home() {
  const [kind, setKind] = useState('daily');
  const [tickets, setTickets] = useState([]);
  const [status, setStatus] = useState({ status: 'idle', message: 'Worker idle', tickets_in_db: 0 });
  const [building, setBuilding] = useState(false);
  const [loading, setLoading] = useState(true);
  const [workerDown, setWorkerDown] = useState(false);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('All');
  const [openId, setOpenId] = useState(null);
  const pollRef = useRef(null);
  const buildPollRef = useRef(null);

  useEffect(() => {
    refreshAll();
    pollRef.current = setInterval(fetchStatus, 8000);
    return () => {
      clearInterval(pollRef.current);
      clearInterval(buildPollRef.current);
    };
  }, []);

  useEffect(() => {
    setOpenId(null);
    fetchTickets(kind);
  }, [kind]);

  async function refreshAll() {
    setLoading(true);
    await Promise.all([fetchTickets(kind), fetchStatus()]);
    setLoading(false);
  }

  async function fetchStatus() {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      setStatus(data);
      setWorkerDown(data.status === 'error' && /unreachable/i.test(data.message || ''));
    } catch (e) {
      setWorkerDown(true);
    }
  }

  async function fetchTickets(k) {
    try {
      const res = await fetch(`/api/accas?kind=${k || kind}`);
      const data = await res.json();
      if (data.ok || Array.isArray(data.tickets)) setTickets(data.tickets || []);
    } catch (e) {}
  }

  async function build() {
    setBuilding(true);
    setError('');
    try {
      const res = await fetch('/api/build', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Build rejected (${res.status})`);
      clearInterval(buildPollRef.current);
      buildPollRef.current = setInterval(async () => {
        try {
          const s = await (await fetch('/api/status')).json();
          setStatus(s);
          if (s.status === 'done' || s.status === 'error') {
            clearInterval(buildPollRef.current);
            setBuilding(false);
            if (s.status === 'error') setError(s.message || 'Build failed');
            fetchTickets(kind);
          }
        } catch (e) {
          clearInterval(buildPollRef.current);
          setBuilding(false);
          setError('Lost contact with worker mid-build');
        }
      }, 3000);
    } catch (e) {
      setError(e.message);
      setBuilding(false);
    }
  }

  const counts = {
    All: tickets.length,
    Pending: tickets.filter((t) => (t.status || 'pending') === 'pending').length,
    Won: tickets.filter((t) => t.status === 'won').length,
    Lost: tickets.filter((t) => t.status === 'lost').length,
  };
  const visible = tickets.filter((t) =>
    filter === 'All' ? true : (t.status || 'pending') === filter.toLowerCase()
  );
  const totalLegs = tickets.reduce((s, t) => s + (t.legs?.length || 0), 0);
  const best = tickets.length
    ? Math.max(...tickets.map((t) => Number(t.combined_odds) || 0))
    : 0;
  const activeKind = KINDS.find((k) => k.id === kind);

  return (
    <div className="page">
      <header className="masthead">
        <div className="masthead-rule" />
        <div className="masthead-row">
          <div>
            <p className="kicker">Multi-sport value ledger</p>
            <h1 className="nameplate">The Acca Ledger</h1>
          </div>
          <div className="worker">
            <span className={`dot dot-${status.status || 'idle'}`} />
            <span className="worker-text">{status.message || 'Worker idle'}</span>
          </div>
        </div>
        <p className="dateline">
          {new Date().toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' })}
          {' · '}{status.tickets_in_db ?? tickets.length} slip{(status.tickets_in_db ?? tickets.length) === 1 ? '' : 's'} on file
        </p>
      </header>

      {workerDown && (
        <div className="notice" role="alert">
          <strong>Worker unreachable.</strong> The Render service may be asleep or
          WORKER_URL is unset. Wake it and retry — nothing here will build until it answers.
        </div>
      )}

      <section className="sheet">
        <div className="sheet-copy">
          <h2 className="sheet-head">{todayName()}&rsquo;s value, on one slip.</h2>
          <p className="sheet-sub">
            Scans 177 leagues across football, basketball, tennis and more, prices each leg
            with AI, and keeps the best-value combination.
            {best > 0 ? ` Best ${kind} on file pays ${fmtOdds(best)}x.` : ` No ${kind} slips filed yet.`}
          </p>
          <div className="kinds" role="tablist" aria-label="Slip type">
            {KINDS.map((k) => (
              <button
                key={k.id}
                role="tab"
                aria-selected={kind === k.id}
                className={kind === k.id ? 'kind kind-active' : 'kind'}
                onClick={() => { setKind(k.id); setFilter('All'); }}
              >
                <span className="kind-label">{k.label}</span>
                <span className="kind-blurb">{k.blurb}</span>
              </button>
            ))}
          </div>
        </div>
        <div className="sheet-action">
          <button className="build" onClick={build} disabled={building || workerDown}>
            {building ? 'Scanning odds…' : `File a ${kind} slip`}
          </button>
          <p className="sheet-note">
            {building
              ? status.message || 'Working…'
              : `Manual trigger only. Takes about a minute. No staking. ${activeKind.blurb}`}
          </p>
          {error && <p className="sheet-error">{error}</p>}
        </div>
      </section>

      <nav className="tabs" aria-label="Filter slips">
        {FILTERS.map((f) => (
          <button
            key={f}
            className={filter === f ? 'tab tab-active' : 'tab'}
            onClick={() => setFilter(f)}
          >
            {f} <span className="tab-count">{counts[f]}</span>
          </button>
        ))}
      </nav>

      <main className="ledger">
        {loading && (
          <div className="skeleton" aria-hidden="true">
            <div className="sk-line sk-w40" />
            <div className="sk-line" />
            <div className="sk-line sk-w70" />
          </div>
        )}
        {!loading && visible.length === 0 && (
          <div className="empty">
            <h3>{filter === 'All' ? `No ${kind} slips yet.` : `No ${filter.toLowerCase()} ${kind} slips.`}</h3>
            <p>
              {filter === 'All'
                ? `File your first ${kind} slip above. It will appear here with every leg priced.`
                : 'Try another filter, or file a fresh slip.'}
            </p>
          </div>
        )}
        {visible.map((t, i) => (
          <Slip
            key={t.id || i}
            ticket={t}
            index={tickets.length - tickets.indexOf(t)}
            open={openId === (t.id || i)}
            onToggle={() => setOpenId(openId === (t.id || i) ? null : t.id || i)}
          />
        ))}
      </main>

      <footer className="colophon">
        <p>Settled slips are verified nightly. Pending means kickoff hasn&rsquo;t arrived yet.</p>
      </footer>
    </div>
  );
}

function Slip({ ticket, index, open, onToggle }) {
  const st = ticket.status || 'pending';
  const legs = ticket.legs || [];
  const stake = ticket.stake || {};
  return (
    <article className={`slip slip-${st}`}>
      <button className="slip-top" onClick={onToggle} aria-expanded={open}>
        <div className="slip-id">
          <span className="slip-no">Slip {String(index).padStart(2, '0')}</span>
          <span className="slip-meta">
            {fmtDate(ticket.created_at)} · {legs.length} leg{legs.length === 1 ? '' : 's'} · #{shortId(ticket.id)}
          </span>
          {stake.units != null && (
            <span className="slip-stake">
              Stake {stake.units}u{stake.confidence != null ? ` · ${Math.round(stake.confidence * 100)}% confidence` : ''}
              {stake.note ? ` — ${stake.note}` : ''}
            </span>
          )}
        </div>
        <div className="slip-right">
          <span className={`pill pill-${st}`}>{st}</span>
          <span className="pays">{fmtOdds(ticket.combined_odds)}x</span>
          <span className="caret" aria-hidden="true">{open ? '–' : '+'}</span>
        </div>
      </button>
      {open && (
        <ol className="legs">
          {legs.map((leg, i) => (
            <li key={i} className="leg">
              <div className="leg-main">
                <span className="leg-pick">{leg.selection || 'Pick TBC'}</span>
                <span className="leg-match">
                  {leg.match || 'Fixture TBC'}{leg.league ? ` — ${leg.league}` : ''}
                </span>
              </div>
              <div className="leg-figures">
                {leg.probability != null && (
                  <span className="leg-prob">{Math.round(Number(leg.probability) * 100)}%</span>
                )}
                <span className="leg-odds">{leg.odds != null ? `${leg.odds}` : '—'}</span>
                {leg.result && leg.result !== 'pending' && (
                  <span className={`leg-result leg-result-${leg.result}`}>{leg.result}</span>
                )}
              </div>
            </li>
          ))}
          {legs.length === 0 && <li className="leg leg-empty">Leg detail not recorded for this slip.</li>}
        </ol>
      )}
    </article>
  );
}
