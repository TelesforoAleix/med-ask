export function sourceLabel(item) {
  const kind = ['summary', 'glossary'].includes(item.kind) ? ` · ${item.kind}` : ''
  return `${item.label}${kind}`
}
