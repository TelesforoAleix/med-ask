import React from 'react'

export function EvidenceHeading({ item }) {
  return <>
    <h4>{item.kind === 'figure' ? item.figure_label : 'Passage'} [{item.number}]</h4>
    {item.found_by_description && <p className="note">Found by its description (generated, for discovery only).</p>}
    <p className="passage-heading">{item.kind === 'figure' ? 'Printed caption' : 'Original passage'} (in {item.language}):</p>
  </>
}
