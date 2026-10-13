import React from 'react'

export function EvidenceHeading({ item }) {
  return <>
    <h4>{item.kind === 'figure' ? item.figure_label : 'Passage'} [{item.number}]</h4>
    {item.figure_pdf_page && item.figure_pdf_page !== item.pdf_pages[0] && <p className="note">Plate: PDF page {item.figure_pdf_page}. Caption cited from PDF page {item.pdf_pages[0]}.</p>}
    <p className="passage-heading">{item.kind === 'figure' ? 'Printed caption' : 'Original passage'} (in {item.language}):</p>
  </>
}
