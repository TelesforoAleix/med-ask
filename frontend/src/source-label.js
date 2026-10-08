export function sourceLabel(item) {
  const kind = ['summary', 'glossary'].includes(item.kind) ? ` · ${item.kind}` : ''
  const ocr = item.check_page || item.inherited_ocr ? ' · from OCR — check the page' : item.text_source === 'ocr' ? ' · from OCR' : ''
  return `${item.label}${kind}${ocr}`
}
