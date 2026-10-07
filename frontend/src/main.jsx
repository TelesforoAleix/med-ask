import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || 'Please try again.')
  return data
}

function App() {
  const [question, setQuestion] = useState('')
  const [searchedQuestion, setSearchedQuestion] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [page, setPage] = useState(null)
  const [imageError, setImageError] = useState(false)
  const [thumbs, setThumbs] = useState(null)
  const [comment, setComment] = useState('')
  const [feedbackNote, setFeedbackNote] = useState('')
  const [saving, setSaving] = useState(false)
  const closeButton = useRef(null)
  const reviewButton = useRef(null)
  const panel = useRef(null)

  function closePage() {
    setPage(null)
    reviewButton.current?.focus()
  }

  useEffect(() => {
    if (!page) return
    closeButton.current?.focus()
    function keydown(event) {
      if (event.key === 'Escape') closePage()
      if (event.key === 'Tab') {
        const controls = panel.current.querySelectorAll('button')
        const first = controls[0], last = controls[controls.length - 1]
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault(); last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first.focus()
        }
      }
    }
    document.addEventListener('keydown', keydown)
    return () => document.removeEventListener('keydown', keydown)
  }, [page])

  async function submit(event) {
    event.preventDefault()
    setBusy(true); setError(''); setResult(null); setPage(null)
    setThumbs(null); setComment(''); setFeedbackNote('')
    setSearchedQuestion(question.trim())
    try { setResult(await post('/api/search', { question })) }
    catch (failure) { setError(failure.message) }
    finally { setBusy(false) }
  }

  async function feedback(event) {
    event.preventDefault()
    setSaving(true); setFeedbackNote('')
    try {
      await post('/api/feedback', { question_id: result.question_id, thumbs, comment })
      setFeedbackNote('Feedback saved.')
    } catch (failure) { setFeedbackNote(failure.message) }
    finally { setSaving(false) }
  }

  function review(item, event) {
    reviewButton.current = event.currentTarget
    setImageError(false)
    setPage({ ...item, number: item.pdf_pages[0] })
  }

  return (
    <main>
      <header>
        <p className="eyebrow">YOUR TEXTBOOKS, WITH CONTEXT</p>
        <h1>med-ask</h1>
        <p className="intro">Start with a question. Read the original passages.</p>
      </header>
      <form onSubmit={submit} role="search">
        <label htmlFor="question">What would you like to understand?</label>
        <div className="search-row">
          <input id="question" type="search" value={question} required maxLength={4000}
            onChange={event => setQuestion(event.target.value)}
            placeholder="Ask a question about your textbooks" aria-describedby="search-note" />
          <button disabled={busy} type="submit">{busy ? 'Searching…' : 'Search'}</button>
        </div>
        <p id="search-note" className="note">Results come from a provisional search model and may change.</p>
        <p className="note">Questions and feedback are logged with your signed-in identity to improve search.</p>
      </form>
      <div role="status" aria-live="polite">{error && <p className="error">{error}</p>}</div>
      {result && <section aria-label="Search results" aria-busy={busy}>
        <h2>Original passages</h2>
        <p className="asked">{searchedQuestion}</p>
        {result.evidence.map(item => <article key={item.id}>
          <h3>{item.title}</h3>
          <p className="source-label">{item.label}</p>
          {item.inherited_ocr && <p className="ocr">from OCR — check the page</p>}
          {item.section_path.length > 0 && <p className="note">{item.section_path.join(' › ')}</p>}
          <p className="passage-heading">Original passage (in {item.language}):</p>
          {item.neighbours.filter(n => n.position === 'before').map(n =>
            <blockquote className="neighbour" key={n.id}>
              <small>Preceding passage · {n.label}</small>
              <p>{n.text}{n.truncated ? '…' : ''}</p>
            </blockquote>)}
          <blockquote className="passage">{item.text}</blockquote>
          {item.neighbours.filter(n => n.position === 'after').map(n =>
            <blockquote className="neighbour" key={n.id}>
              <small>Following passage · {n.label}</small>
              <p>{n.text}{n.truncated ? '…' : ''}</p>
            </blockquote>)}
          <button type="button" className="secondary" onClick={event => review(item, event)}>Review source</button>
        </article>)}
        <form onSubmit={feedback} className="feedback">
          <h3>Were these results helpful?</h3>
          <div className="thumbs">
            <button type="button" aria-pressed={thumbs === 'up'} onClick={() => setThumbs(thumbs === 'up' ? null : 'up')}>👍 Thumbs up</button>
            <button type="button" aria-pressed={thumbs === 'down'} onClick={() => setThumbs(thumbs === 'down' ? null : 'down')}>👎 Thumbs down</button>
          </div>
          <label htmlFor="comment">Comment (optional)</label>
          <textarea id="comment" value={comment} maxLength={4000} onChange={event => setComment(event.target.value)} />
          <button disabled={saving} type="submit">{saving ? 'Saving…' : 'Save feedback'}</button>
          <p role="status">{feedbackNote}</p>
        </form>
      </section>}
      {page && <div className="panel-backdrop" onClick={closePage}>
        <aside ref={panel} className="source-panel" role="dialog" aria-modal="true" aria-labelledby="source-title" onClick={event => event.stopPropagation()}>
          <div className="panel-heading">
            <h2 id="source-title">{page.title}</h2>
            <button ref={closeButton} type="button" onClick={closePage}>Close</button>
          </div>
          <p className="asked">{searchedQuestion}</p>
          <p>{page.label}</p>
          {page.pdf_pages[1] > page.pdf_pages[0] && <div className="page-controls">
            <button disabled={page.number === page.pdf_pages[0]} onClick={() => { setImageError(false); setPage({ ...page, number: page.number - 1 }) }}>Previous page</button>
            <span>PDF page {page.number}</span>
            <button disabled={page.number === page.pdf_pages[1]} onClick={() => { setImageError(false); setPage({ ...page, number: page.number + 1 }) }}>Next page</button>
          </div>}
          {imageError ? <p role="alert">The source page could not be loaded. Close and try again.</p> :
            <img key={`${page.book_id}/${page.number}`} src={`/api/page/${encodeURIComponent(page.book_id)}/${page.number}`} alt={`${page.title}, PDF page ${page.number}`} onError={() => setImageError(true)} />}
        </aside>
      </div>}
    </main>
  )
}

createRoot(document.getElementById('root')).render(<App />)
