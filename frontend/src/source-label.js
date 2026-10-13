export function sourceLabel(item) {
  const kind = ['summary', 'glossary'].includes(item.kind) ? ` · ${item.kind}` : ''
  const ocr = item.check_page || item.inherited_ocr ? ' · from OCR — check the page' : item.text_source === 'ocr' ? ' · from OCR' : ''
  return `${item.label}${kind}${ocr}`
}

export function reviewLocation(item) {
  if (!item.figure_pdf_page) return { pdf_pages: item.pdf_pages, label: item.label }
  const printed = item.figure_printed_page ? `print page: ${item.figure_printed_page}` : `print page unknown: ${item.figure_printed_page_reason}`
  return {
    pdf_pages: [item.figure_pdf_page, item.figure_pdf_page],
    label: `${item.book_id}: pdf page ${item.figure_pdf_page} <${printed}>`,
  }
}
