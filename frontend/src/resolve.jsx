import React, { useEffect, useRef, useState } from 'react'

const base = '/api/private/resolve'
async function api(path, body) {
  const response = await fetch(base + path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'Could not load the page. Try again.')
  return data
}

export function Resolve() {
  const [queue, setQueue] = useState([]), [books, setBooks] = useState([]), [reasons, setReasons] = useState({})
  const [book, setBook] = useState(''), [reason, setReason] = useState('')
  const [page, setPage] = useState(null), [text, setText] = useState(''), [number, setNumber] = useState('')
  const [hint, setHint] = useState(''), [candidate, setCandidate] = useState(null)
  const [busy, setBusy] = useState(false), [note, setNote] = useState(''), [imageError, setImageError] = useState(false)
  const version = useRef(0)
  useEffect(() => {
    const current = ++version.current
    setBusy(true); setNote('Loading queue…'); setPage(null); setCandidate(null)
    api(`/queue?${new URLSearchParams({ ...(book ? { book } : {}), ...(reason ? { reason } : {}) })}`)
      .then(data => {
        if (current !== version.current) return
        setQueue(data.queue); setBooks(data.books); setReasons(data.reasons); setNote('')
      }).catch(error => { if (current === version.current) setNote(error.message) })
      .finally(() => { if (current === version.current) setBusy(false) })
    return () => { version.current++ }
  }, [book, reason])
  async function open(row) {
    setBusy(true); setNote(''); setPage(null); setCandidate(null); setImageError(false)
    try {
      const data = await api(`/page/${row.book_id}/${row.pdf_page}`)
      setPage(data); setText(data.text); setNumber(data.printed_page || ''); setHint('')
    } catch (error) { setNote(error.message) }
    finally { setBusy(false) }
  }
  async function act(action, fields = {}) {
    setBusy(true); setNote('')
    try {
      await api(`/page/${page.book_id}/${page.pdf_page}/resolution`, { action, ...fields })
      const index = queue.findIndex(row => row.book_id === page.book_id && row.pdf_page === page.pdf_page)
      const remaining = queue.filter(row => row.book_id !== page.book_id || row.pdf_page !== page.pdf_page)
      setQueue(remaining); setPage(null); setCandidate(null)
      if (remaining.length) await open(remaining[Math.min(index, remaining.length - 1)])
      setNote('Resolution saved. The index changes at the next rebuild.')
    } catch (error) { setNote(error.message) }
    finally { setBusy(false) }
  }
  async function rerun(event) {
    event.preventDefault(); setBusy(true); setNote('Reading this page with your hint…'); setCandidate(null)
    try {
      setCandidate(await api(`/page/${page.book_id}/${page.pdf_page}/hint`, { hint }))
      setNote('Review the generated reading against the image before accepting.')
    } catch (error) { setNote(error.message) }
    finally { setBusy(false) }
  }
  return <section aria-label="Resolve issues" aria-busy={busy}>
    <h2>Resolve issues</h2>
    <p>Compare each reading with the source page. Saved resolutions apply at the next rebuild.</p>
    <div className="resolve-filters">
      <label>Book<select disabled={busy} value={book} onChange={event => setBook(event.target.value)}>
        <option value="">All books</option>{books.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
      </select></label>
      <label>Reason<select disabled={busy} value={reason} onChange={event => setReason(event.target.value)}>
        <option value="">All reasons</option>{Object.entries(reasons).map(([code, words]) => <option key={code} value={code}>{words}</option>)}
      </select></label>
    </div>
    <p role="status">{note}</p>
    <details open={!page}><summary>{queue.length} queued pages</summary>
      <div className="resolve-queue">{queue.map(row => <button type="button" disabled={busy} key={`${row.book_id}-${row.pdf_page}`} onClick={() => open(row)}>
        {row.title} · PDF page {row.pdf_page}
      </button>)}</div>
    </details>
    {page && <ResolvePage {...{ page, busy, imageError, setImageError, act, number, setNumber,
      text, setText, hint, setHint, rerun, candidate, setCandidate, setNote }} />}
  </section>
}

export function ResolvePage({ page, busy, imageError, setImageError, act, number, setNumber,
  text, setText, hint, setHint, rerun, candidate, setCandidate, setNote }) {
  return <>
      <h3>{page.title} · PDF page {page.pdf_page}</h3>
      <ul>{page.reason_messages.map(message => <li key={message}>{message}</li>)}</ul>
      <div className="resolve-pair">
        <div><h4>Source page</h4><img className="resolve-image" alt={`Source PDF page ${page.pdf_page}`} onError={() => setImageError(true)} src={`/api/page/${page.book_id}/${page.pdf_page}`} />
          {imageError && <p role="alert">The page image could not be loaded. Reopen the page to try again.</p>}</div>
        <div>
          <section className={page.generated ? 'generated' : ''}><h4>{page.generated ? 'Kept reading — generated OCR' : 'Existing page text'}</h4><pre className="resolve-reading">{page.text}</pre></section>
          <fieldset disabled={busy || imageError}>
            <legend>Resolve this page</legend>
            <button type="button" onClick={() => act('confirm')}>Confirm reading as correct</button>
            <form onSubmit={event => { event.preventDefault(); act('number', { printed_page: number }) }}>
              <label>Correct printed page number<input required value={number} onChange={event => setNumber(event.target.value)} /></label>
              <button>Save printed number</button>
            </form>
            <form onSubmit={event => { event.preventDefault(); act('text', { text }) }}>
              <label>Type the page’s text<textarea required rows={12} maxLength={100000} value={text} onChange={event => setText(event.target.value)} /></label>
              <button>Save replacement text</button>
            </form>
            <form onSubmit={rerun}>
              <label>Hint for a new reading<textarea required maxLength={4000} value={hint} onChange={event => setHint(event.target.value)} /></label>
              <button>Re-read with hint</button>
            </form>
            {candidate && <section className="generated"><h4>Hinted reading — generated OCR</h4><pre className="resolve-reading">{candidate.text}</pre>
              <ul>{candidate.reason_messages.map(message => <li key={message}>{message}</li>)}</ul>
              <button type="button" onClick={() => act('hinted', { attempt_id: candidate.attempt_id })}>Accept hinted reading</button>
              <button type="button" onClick={() => { setCandidate(null); setNote('Candidate discarded. Its attempt remains in history; the kept reading is unchanged.') }}>Discard candidate</button>
            </section>}
          </fieldset>
        </div>
      </div>
    </>
}
