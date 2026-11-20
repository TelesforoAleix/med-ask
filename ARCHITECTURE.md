# Architecture

This document names med-ask's parts, what each part and library owns, how a book, a question
and an answer move between them, where each part runs, and which other repositories it depends
on. Read it before changing a boundary, adding a part or deciding where new code belongs. It
describes the current state only; reasons are in [DECISIONS.md](DECISIONS.md), interfaces in
[docs/contracts.md](docs/contracts.md), and procedures in the documents the
[AGENTS.md document map](AGENTS.md#document-map) names.

## Boundary

The README diagrams ([architecture](docs/diagrams/architecture-light.svg),
[one question](docs/diagrams/question-light.svg)) are the visitor view. The boundary in text:

```
 student browser ── Cloudflare Access ── cloudflared ── tunnel network ── public ─┐
                                                                                   │
 owner browser (Tailscale) ── APP_BIND_ADDRESS:8100 ── app ────────────────────────┤
                                                                                   │
 operator ── docker compose run ── ingest ─────────────────────────────────────────┤
                                                                                   │
                    backend network: postgres (pgvector, no host port) ◄───────────┤
                    homelab-models network: Home Lab gateway (purposes) ◄──────────┘
 /data/sources (PDFs, books.toml, read-only) · /data/navigation · /data/originals
```

## Parts

| Part | What it is | Runs | Detail |
|---|---|---|---|
| `app` | The private Flask app with private routes on (`src/med_ask/__init__.py`) | Compose service, port 8100 on `APP_BIND_ADDRESS` | [search.md](docs/search.md) |
| `public` | The same image with private routes off, reached only through the tunnel | Compose service, no host port | [operations.md](docs/operations.md#public-tunnel) |
| `ingest` | The hand-run command line `python -m med_ask.ingest` (`src/med_ask/ingest.py`) | Compose service under the `ingest` profile | [operations.md](docs/operations.md#ingestion-runs) |
| `cloudflared` | The Cloudflare Tunnel connector | Compose service under the `tunnel` profile | [operations.md](docs/operations.md#public-tunnel) |
| `postgres` | Postgres 17 with pgvector: vector tables, bookkeeping and the question log | Compose service, no host port | [contracts.md](docs/contracts.md#database) |
| `frontend` | The React 19 + Vite interface (`frontend/src/`), built into the image | Served by `app` and `public` | [search.md](docs/search.md) |
| `extraction` | Page, paragraph, OCR-reading and figure extraction (`extract.py`, `ocr.py`, `figures.py`) | Inside the image | [extraction.md](docs/extraction.md), [ocr.md](docs/ocr.md), [figures.md](docs/figures.md) |
| `retrieval` | LlamaIndex ingestion and retrieval with the purpose embedding (`retrieval.py`, `embedding.py`) | Inside the image | [index-versions.md](docs/index-versions.md) |
| `generation` | Grading, answers, translation and language detection (`generation.py`) | Inside the image | [search.md](docs/search.md) |
| `evaluation` | Retrieval eval, comparison and gates (`evaluation.py`) | Inside `ingest` | [evaluation.md](docs/evaluation.md) |
| `navigation` | Model-free book navigation files (`navigation.py`) | Built in `ingest`, read by `app` and `public` | [navigation.md](docs/navigation.md) |
| `private-tools` | Resolve and label screens' APIs (`resolutions.py`, `resolve_cache.py`, `labels.py`) | Inside `app` only | [resolve.md](docs/resolve.md), [labelling.md](docs/labelling.md) |
| `service-unit` | `deploy/med-ask.service`, which pulls `main` and runs Compose | systemd on the server | [operations.md](docs/operations.md#install-the-service) |
| `export-units` | `deploy/med-ask-export.service` and its daily timer | systemd on the server | [operations.md](docs/operations.md#question-log-export) |
| `documentation` | The core documents and `docs/claims.json`, whose cited lines `tests/test_docs_links.py` checks | Repository files, checked in CI | [contracts.md](docs/contracts.md#claims) |

## What each library owns

- **LlamaIndex** owns ingestion into the vector store, the store's tables and retrieval;
  `PGVectorStore` keeps one table per index with exact search and no approximate index.
- **PyMuPDF** owns reading PDF pages, their text, bookmarks and page images.
- **The OpenAI client** owns the HTTP calls to the gateway; every call names a purpose.
- **langdetect** owns language detection, restricted to English, Spanish and Catalan.
- **Flask and Gunicorn** own HTTP; Gunicorn runs two workers.
- **Postgres** owns durable state. med-ask's own tables hold bookkeeping, the question
  log and the translation cache (`src/med_ask/database.py`).

## Flows

**Ingestion.** The operator runs `ingest ingest <book-id>`. Extraction reads the PDF and any
kept readings into passages; retrieval probes the embedding purpose for the reported model and
dimensions, picks the table, and inserts only missing passage ids through LlamaIndex.

**A question.** The browser posts to `/api/search`. The app logs the question, embeds it once,
retrieves passages (and, from v5, figures), and sends each candidate to `grade`. Only
candidates graded yes are shown, grouped by book.

**An answer.** The browser then posts to `/api/answer`. Only the numbered passing passages go
to `chat`; the reply is validated and shown as a labelled generated answer above the sources.
**Review source** fetches page images from `/api/page/...`; navigation adds contents and links.

## Where it runs

On one Linux server, from a clone of `main`, under `med-ask.service`. Data paths are
host directories set in the server's private `.env`; unset, they fall back to named volumes.
Page reading (`vision`) and bulk embedding (`embed-large-bulk`) use the same gateway as every
other purpose; Home Lab decides where each purpose runs.

## Other repositories

| Repository | Relation | Contract |
|---|---|---|
| [Home Lab](https://github.com/TelesforoAleix/homelab-v2) | `app`, `public` and `ingest` call its gateway over `homelab-models` | [model-gateway](docs/contracts.md#model-gateway) |
| [Home Lab](https://github.com/TelesforoAleix/homelab-v2) | `service-unit` starts with its data volumes and `homelab.service` | [host-units](docs/contracts.md#host-units) |
| [Factory](https://github.com/TelesforoAleix/factory) | Its `schemas` define the claims file that `documentation` keeps, and its `docs-verify` reads that file | [claims](docs/contracts.md#claims) |
