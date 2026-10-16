import React, { useEffect, useRef, useState } from 'react'
import { linkSegments } from './book-navigation.js'
import { PageImage, PageLabel, usePageNavigation } from './book-viewer.jsx'

export function QuickLookPage({ item, link, text, navigation, error, retry, close, openBook, closeButton }) {
  return <aside className="quick-look" aria-label="Reference quick look">
    <div className="panel-heading"><h5>{text} <small>({link.kind})</small></h5>
      <button ref={closeButton} type="button" className="secondary" onClick={close}>Close quick look</button></div>
    <p><PageLabel number={link.target_pdf_page} navigation={navigation} /></p>
    {navigation?.section_path.length > 0 && <p className="note">{navigation.section_path.map(level => level.title).join(' › ')}</p>}
    {navigation === undefined && !error && <p role="status">Loading book navigation…</p>}
    {error && <p role="alert">Book navigation could not be loaded. <button type="button" onClick={retry}>Retry navigation</button></p>}
    <PageImage key={`${item.book_id}/${link.target_pdf_page}`} bookId={item.book_id} number={link.target_pdf_page} title={item.title} />
    <button type="button" onClick={event => openBook(item, event, link.target_pdf_page)}>Open in the book</button>
  </aside>
}

function QuickLook(props) {
  const { navigation, error, retry } = usePageNavigation(props.item.book_id, props.link.target_pdf_page)
  const closeButton = useRef(null)
  useEffect(() => { closeButton.current?.focus() }, [])
  return <QuickLookPage {...props} {...{ navigation, error, retry, closeButton }} />
}

export function LinkedPassage({ item, openBook }) {
  const [selected, setSelected] = useState(null)
  const opener = useRef(null)
  function close() { setSelected(null); opener.current?.focus() }
  return <div className={selected ? 'linked-passage has-quick-look' : 'linked-passage'}>
    <blockquote className="passage">{linkSegments(item.text, item.links).map((segment, index) => segment.link ?
      <button type="button" className="passage-link" key={index} onClick={event => { opener.current = event.currentTarget; setSelected(segment) }}>{segment.text}</button> :
      <React.Fragment key={index}>{segment.text}</React.Fragment>)}</blockquote>
    {selected && <QuickLook key={`${selected.link.start}/${selected.link.end}`} item={item} link={selected.link} text={selected.text} close={close} openBook={openBook} />}
  </div>
}
