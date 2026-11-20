# Decisions

This log records choices that were not obvious, with their reasons, newest last. Read it when
you need to know why something is the way it is. It is append-only: add a dated entry and never
edit an old one. The current state is in the documents the
[AGENTS.md document map](AGENTS.md#document-map) names.

## 2026-11-20: Documents restructured into a core set with checked claims

**Decision.** The repository's documents follow one shape shared with Home Lab: `AGENTS.md`
as a router with the rules and a document map, `ARCHITECTURE.md` for parts and boundaries,
`docs/contracts.md` for every interface, `docs/operations.md` for server steps, one document
per recurring procedure, this log, and `docs/claims.json` listing each claim with the lines
that prove it. `tests/test_docs_links.py` keeps the map, links and citations valid, and the
code-only guard allows exactly `docs/claims.json` as a JSON file under `docs/`.

**Why.** Executors start from a task prompt and these documents. The old runbook had grown to
1,294 lines mixing current behaviour with finished one-off procedures, so the current facts
were hard to find and could not be checked.

**How.** The runbook's current facts moved into the document that owns each topic. The step
lists of finished one-off rebuilds were generalized into
[docs/index-versions.md](docs/index-versions.md); their full text stays in git history at
commit `4eece14`, `docs/operations.md`. Measurements and outcomes that no file in this
repository can prove are kept here, as recorded on that date:

- **Embedding comparison.** Four arms were built and scored: A `embed` roles off (the original
  plain table), B `embed` roles on (a separate role table), C `embed-large` roles off (a new
  plain table) and D `embed-large` roles on (reusing C's table). The owner chose
  `embed-large`, roles off. Its role tables stay plain because the gateway's passage role for
  `embed-large` leaves passage vectors unchanged, so one plain index serves both query modes.
- **Versions built.** v3 added kept readings (and Stryer, as `stryer`, Spanish, with
  `eval_book = "stryer-bioquimica-6-es"`); v4 corrected extraction, with a switch threshold of
  Hit@5 29/32 overall and 18/20 on the 20-question three-book cohort; v5 added captions-only
  figures; v6 split Passarge plates into panels. Passarge has no pages to read.
- **Measured baselines.** v4: Hit@10 32/32 and cohort 20/20. v5: Hit@5 28/32, Hit@10 32/32,
  cohort Hit@10 20/20. The plain v2 `embed-large` baseline run was
  `run-20261010T152043Z-embed-large-roles-off-97cc8d.json`.
- **Figure counts at v5.** Alberts 1,734, Mathews 914, Stryer 851 and Passarge 198 (3,697).
  In v6, 192 Passarge plates have lettered legends; the plate on PDF 200 is unlettered.
- **Rates.** CPU ingest on the server's four cores was expected at about 0.14–0.26
  passages/s (about 70 characters/s); long passages took about 45 seconds, so CPU ingests
  used `EMBEDDING_TIMEOUT=120`, and the four books were expected to take around 30 hours.
  Bulk builds through `embed-large-bulk` use 300 seconds; a v6 copy plus Passarge build was
  estimated at 20–30 minutes of the shared GPU against about three hours for all four books.
- **The Mac's purposes.** `vision` took about 9 seconds from cold and then 1.5–2 seconds per
  page; a batch of 20 bulk embeddings about 0.8–1.4 seconds; each model unloads after 10
  minutes idle. `embed-large-bulk` and `embed-large` report the same identity and 2,560
  dimensions. In the Working profile `vision` answers "not available until Inference"; a fast
  generic HTTP 500 (under about 1.3 seconds) meant the Mac was off, asleep or its router
  stopped.
- **Search caps.** `PASSAGE_CANDIDATES` stays at least 10 so the figure gate's Hit@10 stays
  measurable; figures follow passages so they never displace one.

## 2026-11-20: The claims file and single-owner facts

- **Claims contract.** med-ask adopts Factory's `claims` contract at version 1: Factory
  provides the schema and its `docs-verify` reads `docs/claims.json`. The link test only
  proves that cited paths and lines exist; whether a cited line supports its claim is
  Factory's check.
- **One owner per fact.** Embedding defaults live in the server environment table, the
  search kinds in `extraction.md`, figure slots in `figures.md` and table protection in the
  database contract; other documents link to them. A change to one fact then edits one
  document.
- **Documents follow the code.** Where a document contradicted the code, as for the
  embedding dimensions, the password default, the watermark words and the legacy
  inherited-OCR join, the document was corrected and no code was changed.
