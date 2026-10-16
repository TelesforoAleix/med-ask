// Offsets come from Python strings, so count Unicode code points, not UTF-16 units.
export function linkSegments(text, links = []) {
  const characters = Array.from(text)
  const segments = []
  let cursor = 0
  const ordered = links.filter(link => Number.isInteger(link.start) && Number.isInteger(link.end)
    && link.start >= 0 && link.end <= characters.length && link.end > link.start)
    .sort((a, b) => a.start - b.start || a.end - b.end)
  for (const link of ordered) {
    // The first span wins an overlap; never duplicate or remove source characters.
    if (link.start < cursor) continue
    if (link.start > cursor) segments.push({ text: characters.slice(cursor, link.start).join('') })
    segments.push({ text: characters.slice(link.start, link.end).join(''), link })
    cursor = link.end
  }
  if (cursor < characters.length) segments.push({ text: characters.slice(cursor).join('') })
  return segments
}

export function pageBounds(pdfPages, navigation) {
  return navigation ? [1, navigation.page_count] : pdfPages
}

export function stepPage(number, direction, bounds) {
  return Math.max(bounds[0], Math.min(bounds[1], number + direction))
}

export function currentContentsEntry(contents, sectionPath) {
  let entries = contents, current = null
  for (const level of sectionPath) {
    const entry = entries.find(node => node.title === level.title && node.pdf_page === level.pdf_page)
    if (!entry) break
    current = entry
    entries = entry.children
  }
  return current
}

const contentsRequests = new Map()
export async function navigationJSON(path) {
  const response = await fetch(path)
  if (response.status === 404) return null
  if (!response.ok) throw new Error('Book navigation could not be loaded.')
  return response.json()
}

export function bookContents(bookId) {
  if (!contentsRequests.has(bookId)) {
    contentsRequests.set(bookId, navigationJSON(`/api/book/${encodeURIComponent(bookId)}/contents`))
  }
  return contentsRequests.get(bookId)
}
