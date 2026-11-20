# Labelling real questions

This document covers the private **Label questions** screen: how a logged question is
labelled with the pages that answer it, where labels are kept and what their records contain.
Read it before changing `src/med_ask/labels.py` or `frontend/src/label.jsx`, and before
labelling. Scoring the labels is in [evaluation.md](evaluation.md#real-case-modes).

## The screen

Only `app` with private routes on shows **Label questions** beside **Resolve issues**; every
`/api/private/label` path is 404 on `public`. Choose an inclusive date range and a logged
question, newest first, and read its question, date, language and the generated answer as
logged.

Suggested pages list logged candidates graded yes first, then the other logged candidates in
their original order, then a wider search (`k=100`) on the served index, with each book page
once. Questions logged before grading recover their saved evidence ids from the recorded
index, without inventing grades. The wider search does no grading or answer generation; if it
fails, logged pages and labelling still work.

Open a suggested page in the book viewer and press **This page answers** to add the open PDF
page; **Remove** removes one. Any expected page makes the question **in scope**. **Out of the
sources** marks a question the books should not answer and clears expected pages; **Clear**,
or removing the last page, returns it to **unlabelled**. Only the owner and the student
supply labels; suggestions never become labels by themselves.

## Routes

`GET /api/private/label/questions` (`from` and `to` ISO dates) lists questions;
`GET /api/private/label/question/<log-id>` and its `/suggestions` child give the detail and
wider suggestions. `POST .../label` takes `action` `outside`, `clear`, `add` or `remove`; page
actions also need `book_id` and `pdf_page`. Writes from another origin are rejected, bodies
are limited to 32,000 bytes, and responses are `Cache-Control: no-store`. Saving a label never
changes a log, source, index or public response.

## The label file

Labels live apart from the gate set at `/data/originals/eval/med-ask-eval-real.v1.jsonl`,
created on the first save. Each save locks the eval directory across workers, rewrites the
whole file and atomically replaces it after syncing, keeping every other record. An unlabelled
question has no record. Never copy this file or its questions off the server or into git.

A record uses schema 1, status `confirmed`, type and subject `unclassified`, the log's
question and language, the id `r` plus the first twelve hex digits of the log's UUID, and the
full `log_id`; it holds no asker identity. When the log's language is null, saving detects it
locally and records `language_source: "detected"`, otherwise `log`. Evidence uses the
manifest's `eval_book` ids and PDF pages; an out-of-sources record has
`expect_not_found: true` and no evidence.
