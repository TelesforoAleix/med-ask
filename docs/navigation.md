# Navigation data

This document covers the model-free book navigation files: what they hold, when to rebuild
them, how the app reads them and the precision rule that decides which references are
published. Read it before changing `src/med_ask/navigation.py`, the viewer in `frontend/src/`,
or after any change that requires a rebuild.

## What it is

`navigate` builds one file per book at `/data/navigation/<book-id>.json`, separately from the
index and originals, with no model calls. Each holds a version, an input fingerprint, the
contents tree, page information and references keyed by passage id. It copies no passage text
but contains source headings and labels, so it stays outside git. Only `ingest` mounts the
directory read-write; `app` and `public` mount it read-only.

## Build

```sh
docker compose --profile ingest run -d --name med-ask-navigation ingest navigate <book-id> ... --output /data/originals/eval/nav-work/report.json
docker wait med-ask-navigation
docker logs med-ask-navigation
```

Rebuild after a change to a PDF, a kept reading, an owner resolution, the extraction or figure
code, or the navigation version. The builder fingerprints those inputs and refuses to publish
if they change mid-build; files are replaced atomically, so an interrupted build keeps the old
ones.

## How the app reads it

The app checks only the file's version and the PDF's size and modification time, so a changed
reading or resolution needs the explicit rebuild above. Each worker keeps a parsed file in
memory until it changes. Requests never extract a book or call a model for navigation.

`GET /api/book/<book-id>/contents` returns `{"contents": [...]}`, and
`GET /api/book/<book-id>/page/<pdf-page>` returns the page's `pdf_page`, `printed_page` and
`section_path` with `page_count`. Missing or stale navigation, unknown books and out-of-range
pages return 404, and search evidence then has empty `links`. The viewer shows every page with
PDF and printed numbers, breadcrumbs and collapsible contents; passage references open a quick
look with the page image. Books without navigation keep passage-only paging.

## Published references

The counts-only report records resolved and unresolved mentions per kind (figure, table,
section, chapter, page), seeded precision samples and contents coverage. Every reference kind
whose sample precision is below 90% is suppressed and listed in `excluded_kinds`; its mentions
stay plain text. Overall precision below 90% blocks handover; a weak kind or thin contents is
reported for follow-up.

For mostly scanned books, where kept readings supply more than half of the content pages,
contents keep only numbered chapter and section headings, and sections attach to observed
chapters with matching numbers; no missing chapter is invented. The report's
`chapter_entry_coverage` is diagnostic, not a verified chapter total.

To disable navigation, see [Rollback](operations.md#rollback).
