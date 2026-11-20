# Extraction

This document covers how a book becomes passages: provenance, kinds, contents exclusion,
reading headings and joins across pages. Read it before changing `src/med_ask/extract.py` or
anything that changes passage text, ids or kinds; such a change is served only through a new
index version ([index-versions.md](index-versions.md)). Page readings are in [ocr.md](ocr.md)
and figures in [figures.md](figures.md).

## Provenance

Extraction runs in memory, without network access, and returns an account of every PDF page.
Every passage records its text source: `born-digital`, `inherited-ocr` (the PDF's own text
layer came from OCR) or `ocr` (a kept reading). Pages whose whole text is `Hidden page` are
missing-content placeholders: they produce no passage, are never read, and block joins across
them. Owner resolutions ([resolve.md](resolve.md)) are applied before passages are built.

## Kinds

Every passage has a kind: `content`, `summary`, `glossary`, `exercise` or `references`. The
nearest recognised heading in its section path sets it, reading upward; matching ignores case
and whitespace, including letter-spaced titles, and an unknown heading means content. The
vocabulary is `KIND_HEADINGS` in `extract.py`. A kind changes neither the passage id nor its
embedding input. All kinds are stored; search uses only content, summary and glossary, so
exercises and references are never candidates, in the app or in eval. This is the one place
that lists the kinds search uses, in every index version.

## Contents and headings

Sections titled as contents or index (`EXCLUDED_HEADINGS`) produce no passages, in text layers
and readings alike. In readings, a new chapter heading or a front-matter heading such as
Preface or Acknowledgments closes the prior section, while repeated headers for the same
chapter keep its subsection. Reading headings are kind titles, chapter and numbered section
titles, lines matching a PDF heading, or short uppercase or title-case lines (at most 14 words
and 120 characters, without sentence or list punctuation). Matching PDF heading sizes set the
hierarchy; headings without typography sit at one peer level.

## Readings as passages

Without current readings, extraction keeps the text layer's paragraphs. With them, each
blank-line block of a reading is a passage, the watermark words `Copyrighted material` are
removed from each line while the adjoining text and numbers stay, and `[FIGURE n]` markers and their captions are kept apart from body text. A sustained
repetition (eight copies of a 1–64-token pattern over at least 64 tokens and 200 characters,
or 200 copies of one horizontal whitespace character) cuts the reading's tail when passages
are built; the saved reading is untouched. Passages from readings that failed a rule keep
those reasons, are indexed anyway, and are labelled **from OCR — check the page**.

## Joins across pages

An unfinished content sentence is joined to the next passage in the same book and section when
the next one starts in lower case or the first ends in a hyphenated word; a trailing hyphen is
removed and the word rejoined. Only pages without body passages may lie between them, and none
may be hidden. The page-join pass excludes inherited OCR, readings that failed a rule, and
every kind but content. Legacy extraction, without kept readings, can still join inherited
OCR with the next page's text. A joined passage's PDF and verified printed ranges span both
pages.
