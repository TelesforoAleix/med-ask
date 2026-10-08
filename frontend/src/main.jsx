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
  const [answer, setAnswer] = useState(null)
  const [answerNote, setAnswerNote] = useState('')
  const [translations, setTranslations] = useState({})
  const requestVersion = useRef(0)
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
    const version = ++requestVersion.current
    setBusy(true); setError(''); setResult(null); setPage(null)
    setAnswer(null); setAnswerNote(''); setTranslations({})
    setThumbs(null); setComment(''); setFeedbackNote('')
    setSearchedQuestion(question.trim())
    let found
    try {
      found = await post('/api/search', { question })
      if (version !== requestVersion.current) return
      setResult(found)
    } catch (failure) { if (version === requestVersion.current) setError(failure.message) }
    finally { if (version === requestVersion.current) setBusy(false) }
    if (!found || found.not_found || version !== requestVersion.current) return
    setAnswerNote('Generating an answer from the passing passages…')
    try {
      const generated = await post('/api/answer', { question_id: found.question_id })
      if (version === requestVersion.current) { setAnswer(generated.answer); setAnswerNote('') }
    } catch (failure) { if (version === requestVersion.current) setAnswerNote(failure.message) }
  }

  async function translate(item) {
    const version = requestVersion.current
    if (translations[item.id]?.text) {
      setTranslations(current => ({ ...current, [item.id]: { ...current[item.id], open: true } }))
      return
    }
    setTranslations(current => ({ ...current, [item.id]: { loading: true, open: true } }))
    try {
      const data = await post('/api/translate', { question_id: result.question_id, passage_id: item.id })
      if (version === requestVersion.current)
        setTranslations(current => ({ ...current, [item.id]: { text: data.translation, open: true } }))
    } catch (failure) {
      if (version === requestVersion.current)
        setTranslations(current => ({ ...current, [item.id]: { error: failure.message, open: true } }))
    }
  }

  async function feedback(event) {
    event.preventDefault()
    const version = requestVersion.current
    setSaving(true); setFeedbackNote('')
    try {
      await post('/api/feedback', { question_id: result.question_id, thumbs, comment })
      if (version === requestVersion.current) setFeedbackNote('Feedback saved.')
    } catch (failure) { if (version === requestVersion.current) setFeedbackNote(failure.message) }
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
      <div role="status" aria-live="polite">{busy && <p>checking which passages answer this…</p>}{error && <p className="error">{error}</p>}</div>
      {result && <section aria-label="Search results" aria-busy={busy}>
        {!result.not_found && <section className="generated answer" aria-label="Generated answer">
          <h2>Generated answer</h2>
          <p className="note">Generated from the passages below. Check the original sources.</p>
          {answer && <p className="answer-text">{answer}</p>}
          <p role="status">{answerNote}</p>
        </section>}
        <h2>Original passages</h2>
        {result.not_found && <p className="not-found">Not found. These books don't cover the question.</p>}
        {result.ungraded_count > 0 && <p role="status">{result.ungraded_count} passages could not be checked and are excluded.</p>}
        <p className="asked">{searchedQuestion}</p>
        {result.groups.map(group => <section className="book-group" key={group.book_id} aria-label={group.title}>
          <h3>{group.title}</h3>
          {group.evidence.map(item => <article key={item.id} id={`passage-${item.number}`}>
          <h4>Passage [{item.number}]</h4>
          <p className="source-label">{item.label}</p>
          {item.inherited_ocr && <p className="ocr">from OCR — check the page</p>}
          {item.section_path.length > 0 && <p className="note">{item.section_path.join(' › ')}</p>}
          <p className="passage-heading">Original passage (in {item.language}):</p>
          {item.neighbours.filter(n => n.position === 'before').map(n =>
            <blockquote className="neighbour" key={n.id}>
              <small>Preceding passage · {n.label}</small>
              <p>{n.text}{n.truncated ? '…' : ''}</p>
            </blockquote>)}
          <div className={translations[item.id]?.open ? 'passage-pair' : ''}>
            <blockquote className="passage">{item.text}</blockquote>
            {translations[item.id]?.open && <section className="generated translation" aria-label="Generated translation">
              <h4>Generated translation</h4>
              {translations[item.id].loading && <p role="status">Translating…</p>}
              {translations[item.id].error && <p role="alert">{translations[item.id].error}</p>}
              {translations[item.id].text && <p>{translations[item.id].text}</p>}
              <button type="button" className="secondary" onClick={() => setTranslations(current => ({ ...current, [item.id]: { ...current[item.id], open: false } }))}>Close translation</button>
            </section>}
          </div>
          {item.neighbours.filter(n => n.position === 'after').map(n =>
            <blockquote className="neighbour" key={n.id}>
              <small>Following passage · {n.label}</small>
              <p>{n.text}{n.truncated ? '…' : ''}</p>
            </blockquote>)}
          <button type="button" className="secondary" onClick={event => review(item, event)}>Review source</button>
          {item.translation_available && <button type="button" className="secondary translation-button"
            disabled={translations[item.id]?.loading} onClick={() => translate(item)}>Open translation</button>}
        </article>)}
        </section>)}
        <form onSubmit={feedback} className="feedback">
          <h3>Was the whole response helpful?</h3>
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
