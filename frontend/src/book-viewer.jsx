import React, { useEffect, useRef, useState } from 'react'
import { sourceLabel } from './source-label.js'
import { bookContents, currentContentsEntry, navigationJSON, pageBounds, stepPage } from './book-navigation.js'

export function usePageNavigation(bookId, number) {
  const [state, setState] = useState(null)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let active = true
    navigationJSON(`/api/book/${encodeURIComponent(bookId)}/page/${number}`)
      .then(data => { if (active) setState({ bookId, number, data }) })
      .catch(() => { if (active) setState({ bookId, number, error: true }) })
    return () => { active = false }
  }, [bookId, number, retry])
  const current = state?.bookId === bookId && state?.number === number ? state : null
  return { navigation: current?.data, error: current?.error, retry: () => { setState(null); setRetry(value => value + 1) } }
}

export function PageLabel({ number, navigation }) {
  return <span>PDF page {number}{navigation?.printed_page != null ? ` · Printed page ${navigation.printed_page}` : ''}</span>
}

export function PageImage({ bookId, number, title }) {
  const [failed, setFailed] = useState(false)
  return failed ? <p role="alert">The source page could not be loaded. <button type="button" onClick={() => setFailed(false)}>Retry image</button></p> :
    <img src={`/api/page/${encodeURIComponent(bookId)}/${number}`} alt={`${title}, PDF page ${number}`} onError={() => setFailed(true)} />
}

function ContentsTree({ entries, current, jump }) {
  return <ul>{entries.map((entry, index) => <li key={index}>
    <button type="button" className="contents-entry" aria-current={entry === current ? 'location' : undefined}
      onClick={() => jump(entry.pdf_page)}>{entry.title} <small>PDF {entry.pdf_page}{entry.printed_page != null ? ` · Printed ${entry.printed_page}` : ''}</small></button>
    {entry.children.length > 0 && <details open>
      <summary>Subsections</summary>
      <ContentsTree entries={entry.children} current={current} jump={jump} />
    </details>}
  </li>)}</ul>
}

export function ViewerPage({ page, number, navigation, contents, contentsError, loading, navigationError, retry, jump, navigationAvailable = Boolean(navigation) }) {
  const bounds = pageBounds(page.pdf_pages, navigation)
  const current = currentContentsEntry(contents || [], navigation?.section_path || [])
  return <>
    <div className="page-controls">
      <button type="button" disabled={loading || navigationError || number <= bounds[0]} onClick={() => jump(stepPage(number, -1, bounds))}>Previous page</button>
      <span aria-live="polite"><PageLabel number={number} navigation={navigation} /></span>
      <button type="button" disabled={loading || navigationError || number >= bounds[1]} onClick={() => jump(stepPage(number, 1, bounds))}>Next page</button>
    </div>
    {loading && <p role="status">Loading book navigation…</p>}
    {navigationError && <p role="alert">Book navigation could not be loaded. <button type="button" onClick={retry}>Retry navigation</button></p>}
    {navigationAvailable && <>
      <nav className="breadcrumbs" aria-label="Book section">{(navigation?.section_path || []).map((level, index) =>
        <button type="button" className="secondary" key={index} onClick={() => jump(level.pdf_page)}>{level.title}</button>)}</nav>
      <details className="book-contents">
        <summary>Contents</summary>
        {contentsError ? <p role="alert">The contents could not be loaded.</p> : contents ?
          <ContentsTree entries={contents} current={current} jump={jump} /> : <p role="status">Loading contents…</p>}
      </details>
    </>}
    <PageImage key={`${page.book_id}/${number}`} bookId={page.book_id} number={number} title={page.title} />
  </>
}

export function BookViewer({ page, question, close, onAnswerPage, busy, labelNote }) {
  const [number, setNumber] = useState(page.number)
  const { navigation, error, retry } = usePageNavigation(page.book_id, number)
  const [hasNavigation, setHasNavigation] = useState(false)
  useEffect(() => { if (navigation) setHasNavigation(true) }, [navigation])
  const [contents, setContents] = useState(null)
  const [contentsError, setContentsError] = useState(false)
  const panel = useRef(null)
  const closeButton = useRef(null)
  const loading = navigation === undefined && !error
  useEffect(() => {
    let active = true
    bookContents(page.book_id).then(data => { if (active) setContents(data?.contents || []) })
      .catch(() => { if (active) setContentsError(true) })
    return () => { active = false }
  }, [page.book_id])
  useEffect(() => { closeButton.current?.focus() }, [])
  useEffect(() => {
    function keydown(event) {
      if (event.key === 'Escape') { event.preventDefault(); close() }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault()
        if (!loading && !error) setNumber(stepPage(number, event.key === 'ArrowLeft' ? -1 : 1, pageBounds(page.pdf_pages, navigation)))
      }
      if (event.key === 'Tab') {
        const controls = [...panel.current.querySelectorAll('button:not(:disabled), summary')]
          .filter(element => {
            for (let ancestor = element.parentElement; ancestor !== panel.current; ancestor = ancestor.parentElement) {
              if (ancestor.tagName === 'DETAILS' && !ancestor.open && !ancestor.querySelector('summary')?.contains(element)) return false
            }
            return true
          })
        const first = controls[0], last = controls[controls.length - 1]
        if (event.shiftKey && (document.activeElement === first || !panel.current.contains(document.activeElement))) {
          event.preventDefault(); last.focus()
        } else if (!event.shiftKey && (document.activeElement === last || !panel.current.contains(document.activeElement))) {
          event.preventDefault(); first.focus()
        }
      }
    }
    document.addEventListener('keydown', keydown)
    return () => document.removeEventListener('keydown', keydown)
  }, [number, navigation, loading, error, page, close])
  return <div className="panel-backdrop" onClick={close}>
    <aside ref={panel} className="source-panel" role="dialog" aria-modal="true" aria-labelledby="source-title" onClick={event => event.stopPropagation()}>
      <div className="panel-heading"><h2 id="source-title">{page.title}</h2><button ref={closeButton} type="button" onClick={close}>Close</button></div>
      <p className="asked">{question}</p>
      <p>Opened from: {sourceLabel(page)}</p>
      {onAnswerPage && <div><button type="button" disabled={busy} onClick={() => onAnswerPage(page.book_id, number)}>This page answers</button><p role="status">{labelNote}</p></div>}
      <ViewerPage {...{ page, number, navigation, contents, contentsError, loading, navigationError: error, retry, jump: setNumber }} navigationAvailable={navigation !== null && (hasNavigation || Boolean(navigation))} />
    </aside>
  </div>
}
