# Search, answers and the question log

This document covers one question's path through the app: the search route, grading, the
evidence shown, the generated answer, translations, the question log, identity and feedback.
Read it before changing a route in `src/med_ask/__init__.py`, `generation.py`, the question
log, or the browser's search screen in `frontend/src/`. Retrieval internals are in
[index-versions.md](index-versions.md) and [figures.md](figures.md).

## Search

`POST /api/search` accepts `{"question": "…"}` of 1–4,000 characters after NUL characters are
removed. Every valid question is logged before search, including attempts with no index. The
app embeds the question once, retrieves `PASSAGE_CANDIDATES` passages (default 26) through
LlamaIndex and, from v5, up to `FIGURE_CANDIDATES` figures ([figures.md](figures.md#figures-in-search)), then sends the question
and one candidate per `grade` call. The passage count must stay at least 10 to keep the
eval's Hit@10 meaningful.

Grading calls run in parallel, at most `GRADE_CONCURRENCY` (default 15) at once, each with a
`GRADE_TIMEOUT` of 6 seconds. Only a complete `yes` or `no` counts; errors, timeouts and other
replies leave a candidate ungraded. There are no retries or fallback purposes. Compose sets
none of these variables, so the code defaults apply.

## Evidence

While grading runs the screen says "checking which passages answer this…". Only candidates
graded yes appear, grouped by book: passages within a book follow similarity order, books
follow their best passage, and numbers run across books. The response holds `groups`, flat
numbered `evidence`, `language`, `ungraded_count`, `not_found`, and embedding, grading and
total seconds. When nothing passes, the screen says "Not found. These books don't cover the
question." and requests no answer.

Each source carries its labels, language, section, OCR provenance, navigation `links` (empty
without navigation) and at most one neighbour on each side in the same section. Neighbours are
secondary source context capped at 120 words; they are not numbered and not answer inputs.
Summary and glossary passages show their kind beside the source label. **from OCR — check the
page** marks inherited OCR and readings that failed a rule; passing readings say **from OCR**.

**Review source** fetches `GET /api/page/<book-id>/<pdf-page>`: a page image about 1,000
pixels wide, rendered in memory, with private one-hour cache headers.

## Answer

After the evidence appears, the browser posts `question_id` to `POST /api/answer`. `chat`
receives only the question and the numbered passing passages. Its prompt asks for at most 150
words in the question's language, a citation on every sentence, and plain statements of gaps,
disagreements and thin support. The reply must be JSON sentences: empty or unknown citations,
several sentences in one item, malformed output and answers over 150 words are rejected, and a
failure leaves the evidence visible. The **Generated answer** panel sits above the sources,
labelled as generated. Citations point to passages; support still needs source review.

## Translation

Language detection runs locally with only English, Spanish and Catalan profiles; text without
detectable letters counts as English. **Open translation** appears only on a passage in
another language. `POST /api/translate` takes `question_id` and a displayed `passage_id`,
sends that original alone to `translate`, and returns generated text beside it. Postgres
caches translations by passage id and language, and a cached reply says `cached: true`
without a model call. `GENERATION_TIMEOUT` (default 20 seconds) applies to answers and
translations.

## Question log

`medask_questions` stores the question, asker, time, detected language, vector table, every
candidate's id, score, label and grade, the ungraded count, the numbered passing evidence, the
generated answer and optional feedback. The app creates its tables and adds missing columns on
use. A row logged before grading has no grades, so it cannot produce an answer. Exports include
these fields; the rebuildable translation cache is excluded from exports and originals.

## Identity

In `app` every asker is the literal identity `tailnet`. In `public` the asker is the
`Cf-Access-Authenticated-User-Email` header, and a request without it is rejected with 401.
Answer, translation and feedback requests are limited to the asker's own question id, so two
Access identities cannot read or change one another's responses. No asker identity is sent to
any purpose.

## Feedback

Thumbs and a comment judge the whole response and sit below the answer and evidence.
`POST /api/feedback` accepts `thumbs` of `up`, `down` or null and a comment of at most 4,000
characters; NUL characters are removed before storage.
