# Reading pages with vision

This document covers how failing pages are found, read through the `vision` purpose, kept and
re-checked. Read it before changing `src/med_ask/ocr.py` or the rules, and before a reading
run (`ocr`, `ocr-queue`, `ocr-recheck`). How readings become passages is in
[extraction.md](extraction.md); fixing a queued page by hand is in [resolve.md](resolve.md).

## Which pages are read

Every page is checked before any call. Publisher text is kept unless the page is nearly empty
while showing ink. Readings and inherited OCR also get language, word-shape and printed-number
checks. Blank pages are kept, and hidden placeholders are never read or queued. A page that
fails a rule is queued; the reason codes are:

| Reason | Meaning |
| --- | --- |
| `empty-ink` | Fewer than `EMPTY_MIN` trimmed characters on a page with ink |
| `language` | At least `LANGUAGE_MIN` characters, and detection disagrees with the book language |
| `nonwords` | More than `NONWORD_LIMIT` malformed tokens among at least `NONWORD_MIN` eligible ones |
| `number-missing` | No whole decimal or Roman page number in the edge lines |
| `number-order` | A printed number conflicts with nearby pages' numbers of the same family |
| `token-limit` | A vision reply finished with `length` |
| `page-timeout` | A page request exceeded its timeout |

The thresholds are the constants at the top of `ocr.py`. The ink test renders the inner page at
72 DPI in memory and needs more than 0.1% of pixels darker than intensity 180. The word-shape
check is a shape test, not a dictionary, so scientific terminology passes.

## A reading run

`ocr` and `ocr-queue` need the manifest but not Postgres. Inspect first, then read:

```sh
docker compose --profile ingest run --rm ingest ocr-queue <book-id> ...
# A bounded first run; the limit counts new page readings, including their retry.
docker compose --profile ingest run --rm ingest ocr <book-id> --limit 20
# Full runs: one book at a time, as a retained container (operations.md, Long runs).
docker compose --profile ingest run -d --name med-ask-ocr-<book-id> ingest ocr <book-id>
```

Follow [Mac-served purposes](operations.md#mac-served-purposes) before a long run. The queue
reports pages with reason codes, hidden, kept, read-and-passing and queued totals, word-shape
quantiles, reading seconds, rotation retries and rescues; it prints no book text. Never ingest
a book while it is still being read.

Pages are sent one at a time as in-memory PNGs at 300 DPI, temperature 0, with at most 4,096
completion tokens and a 180-second timeout. The fixed prompt keeps the printed language and
spelling, joins line-end hyphenation, separates paragraphs and replaces figure interiors with
`[FIGURE n]` and the printed caption.

An upright reading gets one retry rotated 180 degrees only for `empty-ink`, `nonwords` or
`page-timeout`. A timeout is saved as an empty failed attempt. A completed reply wins over a
timed-out one; otherwise the reading with the fewest failed rules wins, upright on ties. A
connection failure or HTTP 5xx stops the run with exit code 3 and marks nothing; run the same
command again to continue.

## Kept readings

Readings live only at `/data/originals/ocr/<book-id>/<pdf-page>.json`. Each holds the selected
text, the reported model, method version, rotation, failed rules, seconds and UTC time, plus
every attempt. Files are written to a synced temporary file and renamed atomically, so a
failure leaves the previous file intact. `METHOD_VERSION` covers the prompt, DPI and settings
and must change when any of them does; files of another version are ignored and kept inside
the replacement. Current-version attempts are never repeated.

## Re-checking after a rule change

`ocr-recheck <book-id>` recomputes kept readings' rules and finalizes partial readings that no
longer need rotation, without model calls; `ocr` does the same before resuming. A change to
the checking rules keeps `METHOD_VERSION`. After one, re-check each book before its versioned
ingest and record queue totals and reason counts before and after.
