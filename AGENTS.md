# Working in this repository

med-ask searches a student's medical textbooks and returns the original passages, with book
and page, before a short generated answer. It is a Flask app with a React front end, a
LlamaIndex index on Postgres with pgvector, and models reached through Home Lab's gateway.

## CI commands

CI (`.github/workflows/ci.yml`) runs these; run them before every commit:

```sh
uv sync --locked
(cd frontend && npm ci && npm run build)
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python scripts/check_no_data.py
gitleaks git --redact --log-opts=--all
```

CI also verifies the export units with `systemd-analyze verify` and runs a Compose job that
builds the stack and checks the page, health and the private/public split.

## Hard rules

- This repository describes what exists. Work arrives in self-contained task prompts; do not
  look for plans or roadmaps elsewhere.
- Keep code only, ever: no PDFs, page text, page renders, figures or images, indexes,
  embeddings, evaluation sets, user feedback or app state, including test fixtures. CI
  enforces this rule. Tests needing a PDF must generate a synthetic one at runtime. The books
  live outside this repository.
- Never put secrets in git or images. Published ports bind only to loopback.
- Functions in the RAG core (ingestion, page and figure extraction, chunking, retrieval, and
  building the answer prompt) open with their steps in plain language as a numbered list in
  the docstring. Write for a reader who knows Python but not the libraries, and keep those
  steps true when changing code. Plumbing (Flask routes, Compose, CI, configuration) and React
  follow ordinary conventions.
- Models are reached only through an OpenAI-compatible endpoint with base URL and key from the
  environment, using a purpose (such as `chat`, `embed`, `grade` or `vision`) in the `model`
  field. Never name a model provider or model in this repository.
- Libraries own machinery: LlamaIndex owns ingestion, the index and retrieval. Do not
  hand-roll a retriever, queue or graph runtime.
- Always label generated text as generated and keep it visibly apart from source text.
- This repository is public: no host addresses, private network names, private paths,
  account names, secret values or private repository links in any file, PR or log.
- Proceed without asking except before a step that cannot be undone or carries a real
  security risk: deleting data or volumes, touching a secret, making something reachable from
  outside loopback, rewriting main's history or loosening its protection, or a security
  concern the task prompt does not address. For those, stop, explain the risk and how it would
  be recovered, and wait for the owner.

## Document map

When you do the first thing, read the document, and keep it current in the same PR.

- Start any work, or change these rules → [AGENTS.md](AGENTS.md)
- Change a boundary, add a part or move code between parts → [ARCHITECTURE.md](ARCHITECTURE.md)
- Change anything another system calls or reads, or anything med-ask uses from one → [docs/contracts.md](docs/contracts.md)
- Take a step on the server: install, deploy, health, tunnel, rollback, recovery, ingest → [docs/operations.md](docs/operations.md)
- Change a fact in a core document, or the code or config it cites → [docs/claims.json](docs/claims.json)
- Make a choice that was not obvious → append to [DECISIONS.md](DECISIONS.md); never edit old entries
- Change the embedding purpose, roles, table names or passage ids, or build and switch an index → [docs/index-versions.md](docs/index-versions.md)
- Change how PDFs become passages: kinds, headings, joins → [docs/extraction.md](docs/extraction.md)
- Change the OCR rules, or run `ocr`, `ocr-queue` or `ocr-recheck` → [docs/ocr.md](docs/ocr.md)
- Change figure extraction or Passarge plates, or run `figures-count` or `figures-describe` → [docs/figures.md](docs/figures.md)
- Change search, grading, answers, translation, the question log or feedback → [docs/search.md](docs/search.md)
- Change the eval, or score an index before switching → [docs/evaluation.md](docs/evaluation.md)
- Change navigation, or rebuild it after a source, reading or code change → [docs/navigation.md](docs/navigation.md)
- Change the resolve screen, or build its cache after a deploy or source change → [docs/resolve.md](docs/resolve.md)
- Change the label screen, or label logged questions → [docs/labelling.md](docs/labelling.md)
- Change what the visitor-facing summary says → [README.md](README.md)

## Update rule

A change that alters a fact updates the document that owns it, in the same PR. A change to a
contract in `docs/contracts.md` is declared in the PR request's `contract_changes`.

Every document opens with what it covers and when to read it. Record each fact once, in its
owning document, and point to it elsewhere; do not restate what code, configuration or
`--help` already shows. Documents describe the current state; history belongs to git and
`DECISIONS.md`. `docs/claims.json` lists every claim the core documents make with the lines
that prove it: when you move a cited line, update its range. `uv run pytest` includes
`tests/test_docs_links.py`, which checks this map, every relative link and anchor, and the
paths and lines `docs/claims.json` cites.
