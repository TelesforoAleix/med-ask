import React, { useEffect, useRef, useState } from 'react'
import { BookViewer } from './book-viewer.jsx'

async function api(path, body, signal) {
  const response = await fetch('/api/private/label' + path, { signal, ...(body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }) })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'Could not load labels.')
  return data
}

export function LabelQuestion({ detail, busy, act, open }) {
  return <section aria-label="Selected question">
    <h3>{detail.question}</h3>
    <p>{detail.asked_at} · {detail.language} · {detail.label}</p>
    <section className="generated"><h4>App answer — generated, as logged</h4><p className="label-answer">{detail.answer || 'No generated answer was logged.'}</p></section>
    <fieldset disabled={busy}><legend>Human label</legend>
      <button type="button" onClick={() => act('outside')}>Out of the sources</button>
      <button type="button" onClick={() => act('clear')}>Clear</button>
      <h4>Expected pages</h4>
      {detail.expected.length === 0 && <p>No expected pages selected.</p>}
      <ul>{detail.expected.map(p => <li key={`${p.book_id}/${p.pdf_page}`}>
        <button type="button" onClick={() => open(p)}>{p.book_id} · PDF {p.pdf_page}</button>{' '}
        <button type="button" onClick={() => act('remove', p)}>Remove</button>
      </li>)}</ul>
      <h4>Suggested pages</h4>
      <p>Logged candidates, passing ones first, followed by wider search. Suggestions are not labels.</p>
      {detail.unresolved > 0 && <p role="status">{detail.unresolved} logged candidates have unresolved pages.</p>}
      <div className="label-suggestions">{detail.suggestions.map(p => <button type="button" key={`${p.book_id}/${p.pdf_page}`} onClick={() => open(p)}>
        {p.title} · PDF {p.pdf_page}{p.passed ? ' · Passed grading' : ''}
      </button>)}</div>
    </fieldset>
  </section>
}

export function LabelQuestions() {
  const [from, setFrom] = useState(''), [to, setTo] = useState('')
  const [rows, setRows] = useState([]), [detail, setDetail] = useState(null), [page, setPage] = useState(null)
  const [busy, setBusy] = useState(false), [note, setNote] = useState(''), [reload, setReload] = useState(0)
  const version = useRef(0), opener = useRef(null)
  useEffect(() => {
    const controller = new AbortController()
    api(`/questions?${new URLSearchParams({from, to})}`, undefined, controller.signal)
      .then(data => setRows(data.questions)).catch(error => { if (error.name !== 'AbortError') setNote(error.message) })
    return () => controller.abort()
  }, [from, to, reload])
  useEffect(() => () => { version.current++ }, [])
  async function select(row) {
    const current = ++version.current
    setBusy(true); setNote('Loading question…'); setDetail(null); setPage(null)
    try {
      const data = await api(`/question/${row.id}`)
      if (current !== version.current) return
      setDetail(data); setBusy(false); setNote('Loading wider suggestions…')
      try {
        const wider = await api(`/question/${row.id}/suggestions`)
        if (current === version.current) { setDetail(previous => previous?.id === row.id ? {...previous, suggestions: wider.suggestions} : previous); setNote(previous => previous === 'Loading wider suggestions…' ? '' : previous) }
      } catch (error) { if (current === version.current) setNote('Wider suggestions unavailable. Logged pages and labels remain available.') }
    } catch (error) { if (current === version.current) { setNote(error.message); setBusy(false) } }
  }
  async function act(action, fields = {}) {
    setBusy(true); setNote('')
    try {
      const saved = await api(`/question/${detail.id}/label`, {action, ...fields})
      setDetail(previous => ({...previous, ...saved})); setReload(value => value + 1); setNote('Label saved.')
    } catch (error) { setNote(error.message) }
    finally { setBusy(false) }
  }
  function open(p) {
    opener.current = document.activeElement
    setPage({book_id: p.book_id, title: p.title || p.book_id, number: p.pdf_page, pdf_pages: [p.pdf_page, p.pdf_page], label: `PDF ${p.pdf_page}`})
  }
  return <section aria-label="Label questions" aria-busy={busy}>
    <h2>Label questions</h2><p>Choose the pages the books should have supplied, or mark the question out of the sources.</p>
    <div className="resolve-filters"><label>From<input type="date" value={from} onChange={e => setFrom(e.target.value)} /></label>
      <label>Through<input type="date" value={to} onChange={e => setTo(e.target.value)} /></label></div>
    <p role="status">{note}</p>
    <details open={!detail}><summary>{rows.length} logged questions</summary>
      <div className="resolve-queue">{rows.map(row => <button type="button" disabled={busy} key={row.id} onClick={() => select(row)}>
        {row.question} · {row.asked_at.slice(0,10)} · {row.label}
      </button>)}</div>
    </details>
    {detail && <LabelQuestion {...{detail, busy, act, open}} />}
    {page && <BookViewer {...{page, busy}} labelNote={note} question={detail.question} onAnswerPage={(book_id, pdf_page) => act('add', {book_id, pdf_page})}
      close={() => { setPage(null); opener.current?.focus() }} />}
  </section>
}
