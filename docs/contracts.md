# Contracts

This document lists every interface med-ask depends on or offers to others: the model gateway,
the host units it starts with, the public endpoint behind the tunnel, its environment, the
question-log export, the database and the claims file Factory checks. For each it names the version, provider, users, the
source that defines it and how to change it safely. Read it before changing anything a
contract's source names; a change to a contract here is declared in the PR request's
`contract_changes`.

Every change follows the same order unless an entry says more: extend compatibly, keep the old
behaviour until every user has moved, and put incompatible changes under a new version.

## model-gateway

- **Version:** `/v1`, as Home Lab records it.
- **Provider:** Home Lab's `gateway` (`TelesforoAleix/homelab-v2:gateway`). **Users:** `app`,
  `public` and `ingest` in this repository.
- **Source:** `MODEL_BASE_URL` defaults to `http://litellm:4000/v1` and `MODEL_API_KEY` to
  `unused` (`compose.yaml`); the services join the external `homelab-models` network.
  Purposes are the `model` field: the embedding purpose from `EMBEDDING_PURPOSE`
  (`src/med_ask/embedding.py`), `grade`, `chat` and `translate` (`src/med_ask/generation.py`)
  and `vision` (`src/med_ask/ocr.py`).
- **Change safely:** a purpose's reported model selects the vector table name, and the
  reported dimensions are validated against the selected table, so a changed embedding model
  needs a new index ([index-versions.md](index-versions.md#tables)). Ask Home Lab for a new
  purpose rather than naming a model here.

## host-units

- **Version:** 1.
- **Provider:** Home Lab's `data-volumes` (`homelab-data.target` and the volume at
  `/srv/homelab`) and `homelab.service`. **Users:** `service-unit` and `export-units`.
- **Source:** `deploy/med-ask.service`, `deploy/med-ask-export.service`.
- **Change safely:** keep `ConditionPathIsMountPoint=/srv/homelab` so nothing writes to the
  locked mount point; check `systemd-analyze verify` before installing a changed unit.

## public-endpoint

- **Version:** unversioned `/api/*` routes.
- **Provider:** `public`, reached only through `cloudflared`. **Users:** the student's browser,
  signed in through Cloudflare Access.
- **Source:** `src/med_ask/__init__.py` (routes and the `Cf-Access-Authenticated-User-Email`
  identity), `compose.yaml` (`public` and `cloudflared`).
- **Change safely:** the browser and the API ship in one image, so a route change lands in
  both at once. Keep the header trusted only in `public`, and keep `public` without a host
  port, so its only external entry stays the Access-protected tunnel.

## environment

- **Version:** 1.
- **Provider:** the server's private `.env`, documented in `.env.example`. **Users:** `app`,
  `public`, `ingest`, `postgres` and `cloudflared`.
- **Source:** `.env.example`, `compose.yaml`; the table in
  [operations.md](operations.md#server-environment).
- **Change safely:** `POSTGRES_PASSWORD` is required, and Compose refuses to start without it.
  Every other variable keeps a safe unset default, listed in
  [operations.md](operations.md#server-environment); add a variable with a default that keeps
  today's behaviour.

## export-service

- **Version:** 1 (one `row_to_json` row of `medask_questions` per line).
- **Provider:** `export-units` running `ingest export-questions`. **Users:** the owner, who
  reads `/data/originals/questions.jsonl` outside this repository.
- **Source:** `deploy/med-ask-export.service`, `deploy/med-ask-export.timer`,
  `Database.export` in `src/med_ask/database.py`.
- **Change safely:** adding a column to the question log adds a field to every row; never
  remove one. What the export refuses to replace is in
  [operations.md](operations.md#question-log-export).

## database

- **Version:** unversioned; `Database.ensure` creates or extends the schema on use.
- **Provider:** `postgres`. **Users:** `app`, `public` and `ingest`.
- **Source:** `src/med_ask/database.py` (`medask_models`, `medask_books`, `medask_questions`,
  `medask_translations`), `src/med_ask/retrieval.py` (vector tables), `postgres/init.sql`.
- **Change safely:** add columns with `ADD COLUMN IF NOT EXISTS`, since two services may
  create the schema at once. Never drop, delete from or rename a vector table: switching back
  to an older index needs its rows intact.

## claims

- **Version:** 1, the contract named `claims` in Factory.
- **Provider:** Factory's `schemas` (`TelesforoAleix/factory:schemas`), which defines the
  claims-file schema. **Users:** `documentation` in this repository, whose `docs/claims.json`
  Factory's `docs-verify` (`TelesforoAleix/factory:docs-verify`) reads.
- **Source:** the claims rules in `docs/docs-verify.md` of the public repository
  [Factory](https://github.com/TelesforoAleix/factory); this repository's
  `docs/claims.json` and `tests/test_docs_links.py`, which checks that every cited path and
  line exists.
- **Change safely:** keep `version` at 1 and extend compatibly: add claims, relations or
  contracts and keep every field Factory reads. A change to a field's meaning waits for a new
  version from Factory; do not edit the file's shape alone. When a document line moves,
  update the claims that cite it in the same PR.
