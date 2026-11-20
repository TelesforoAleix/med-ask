# Figures

This document covers how printed figures are found, indexed, linked from prose and shown, the
Passarge atlas rule, and the dormant description job. Read it before changing
`src/med_ask/figures.py` or figure handling in retrieval, and before `figures-count` or
`figures-describe`. Which index versions include figures is in
[index-versions.md](index-versions.md).

## Figure entries

Figures come from the captions extraction has already separated: `[FIGURE n]` markers in kept
readings, and paragraphs starting with Figure, Figura or Fig. in text layers. Each entry keeps
the printed label and caption, book, PDF page and verified printed page. Its stable id hashes
the source identity and occurrence, so unnumbered figures on one page stay separate.

Figure nodes embed the label followed by the printed caption. Grading receives only that label
and caption, and answers and translations receive only captions. Opening a figure uses
**Review source**, which shows the whole page.

## Passarge

Passarge is an atlas, so it uses a settled plate rule instead of numbered captions:
`PASSARGE_PLATES` lists the 193 even PDF pages from 32 to 420 except 244 and 274, plus the five
chromosome-map pages 422–426. The label is the printed plate title and the caption is the
facing legend's printed text; the maps share the title from PDF 422 and the legend on PDF 421.
The rule fits only the selected edition: a PDF without the settled pages refuses extraction.

Each entry keeps both locations: `pdf_page` is the plate and `legend_pdf_page` the caption's
source. Evidence cites the legend, while `figure_pdf_page` makes Review source open the plate.
A passage naming a plate's printed title links that plate.

From v6, ordinary plates with lettered legends become one entry per panel: the legend is split
at its `A.`, `B.` … headings, and the label is the plate title plus the letter. Opening text is
included only when it has at most `PASSARGE_OPENING_LIMIT` characters and at most a third of
the panel's text. Unlettered plates and the maps stay whole. A legend passage links the panel
that contains it; a passage spanning several panels, or none, links the plate's panels in
letter order.

## Figures in search

From v5, search retrieves figures separately with the same question embedding, after all
passages, so a figure never displaces a passage. `FIGURE_CANDIDATES` (default 4) caps them:
the first half of the slots go to direct figure matches, then to figures that the retrieved
passages reference in the same book, in passage order, and unused slots go to further direct
matches. Prose references to figure numbers are found by `REFERENCE` without model calls.

## Counting and the description job

`figures-count` reports figures, captionless and unnumbered entries per book with no model
calls:

```sh
docker compose --profile ingest run -d --name med-ask-fig-count ingest figures-count <book-id> ... --output /data/originals/eval/fig-work/counts.json
```

`figures-describe` makes one `vision` request per figure with the whole page at 300 DPI plus
its label and caption, writing a generated record to
`/data/originals/figures/<book-id>/<figure-id>.json`. Reruns skip every kept record, including
failures; a timeout is a failure and an outage stops the run, resumably. The index does not use
descriptions: extraction, ingestion and status never read these files, and a description in old
metadata is dropped before grading, storage or the browser. Run the job only with the owner's
explicit authorization.
