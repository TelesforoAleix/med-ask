# med-ask

med-ask searches original textbook passages, with their book, PDF page, printed
page when known, and neighbouring source context. It checks relevance, then
shows a short generated answer separately from the original passages.
Results come from a provisional search model and may change. Source pages render
on request in a side panel; no page images are stored.

The repository contains code only. Books, the book manifest, extracted passages,
vectors, question logs and feedback live outside git.

## Local checks

Use Python 3.13 with uv and a Node version supported by Vite (CI uses Node 24).
No container runtime is needed on the Mac.

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_no_data.py
npm --prefix frontend ci
npm --prefix frontend run build
gitleaks git --redact --log-opts=--all
```

The guard checks tracked files only. Tests generate synthetic content in temporary
directories, never in committed fixtures. Front-end build output is ignored.

## Compose on a Linux host

Use an x86-64 Linux host with Docker and the Compose plugin. Copy `.env.example`
to `.env` and replace the placeholder with a random URL-safe password before
starting. The environment file is ignored and excluded from the image build.

```sh
cp .env.example .env
# Edit .env to set POSTGRES_PASSWORD.
docker network inspect homelab-models >/dev/null
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8100/
curl --fail http://127.0.0.1:8100/api/health
docker compose down
```

Open `http://127.0.0.1:8100/` on that host. Flask serves the compiled React app
and `/api/` from one container and origin, using Gunicorn as the WSGI server.
`/api/health` returns
`{"postgres":"ok","vector":"ok"}` when Postgres is reachable and its vector
extension exists; otherwise it returns HTTP 503. Unknown API routes return 404.

Postgres publishes no port and, by default, stores its data in the `postgres-data`
named volume. Its initialization script creates only the vector extension on a new
volume. `docker compose down` preserves the volume. The Compose CI job builds,
starts, fetches both routes, and removes its throwaway volume on every push and
PR; no stack is run locally on the Mac.

For a non-Compose environment, the app reads `DATABASE_URL` and optionally
`FRONTEND_DIST` (the built assets directory) from the environment. Database
credentials are runtime configuration and must never enter git or an image.
Private routes are disabled unless `PRIVATE_ROUTES_ENABLED` is exactly `true`;
unset, empty and malformed values leave them disabled.

## Running on the server

The server uses an encrypted volume mounted at `/srv/homelab`, unlocked by hand
after each boot. Unlocking starts `homelab-data.target`, which pulls in
`med-ask.service`. The unit follows that target and orders itself after Docker
and Tailscale, and wants and follows `homelab.service`, which creates the external
`homelab-models` network. It does not start the target or unlock the volume. Failures appear
in `systemctl status med-ask.service`; there is no failure notifier configured.

All med-ask files live on that volume except the installed systemd unit:

| Host path | Purpose | Container path |
| --- | --- | --- |
| `/srv/homelab/med-ask` | Root-owned clone of public `main`, including private `.env` | Image build context |
| `/srv/homelab/med-ask-data/sources` | Textbook PDFs, owned by root and readable by the app | `/data/sources`, read-only |
| `/srv/homelab/med-ask-data/originals` | Question-log exports and other non-rebuildable application records | `/data/originals`, read-write |
| `/srv/homelab/med-ask-data/postgres` | Postgres data | `/var/lib/postgresql/data`, read-write |

Prepare only while the volume is mounted. Stop if either deployment directory
already exists; do not overwrite an existing deployment. On the server:

```sh
mountpoint /srv/homelab
sudo install -d -o root -g root -m 0755 /srv/homelab/med-ask-data
sudo install -d -o root -g root -m 0755 /srv/homelab/med-ask-data/sources
sudo install -d -o root -g root -m 0750 /srv/homelab/med-ask-data/originals
sudo chown root:10001 /srv/homelab/med-ask-data/originals
sudo chmod 2770 /srv/homelab/med-ask-data/originals
sudo install -d -o root -g root -m 0700 /srv/homelab/med-ask-data/postgres
sudo git -c credential.helper= clone --branch main https://github.com/TelesforoAleix/med-ask /srv/homelab/med-ask
```

The app runs as UID/GID 10001. The originals directory is root-owned and writable
by that group. Postgres sets the ownership it needs inside its data directory
when it first starts.

Copy the books from the Mac without putting them in the clone. Substitute your
SSH destination for `<server>`:

```sh
rsync -a --include='*.pdf' --exclude='*' --rsync-path='sudo rsync' ~/Code/med-ask-sources/ <server>:/srv/homelab/med-ask-data/sources/
```

On the server, set their ownership and permissions:

```sh
sudo chown -R root:root /srv/homelab/med-ask-data/sources
sudo find /srv/homelab/med-ask-data/sources -type d -exec chmod 0755 {} +
sudo find /srv/homelab/med-ask-data/sources -type f -exec chmod 0644 {} +
```

Verify there are 13 PDFs and compare their SHA-256 sums with the Mac originals.
The transfer leaves `.DS_Store` behind and starts no service.

The ignored `.env` in the clone must be owned by `root:root`, mode `0600`.
`.env.example` documents these variables:

| Variable | Unset or empty default | Server setting |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | Required; no default | Random URL-safe password generated on the server |
| `APP_BIND_ADDRESS` | Loopback, for local use and CI | Server's private Tailscale IPv4 address |
| `POSTGRES_DATA_PATH` | Named volume `postgres-data` | `/srv/homelab/med-ask-data/postgres` |
| `SOURCES_PATH` | Empty named volume `sources-empty` | `/srv/homelab/med-ask-data/sources` |
| `ORIGINALS_PATH` | Empty named volume `originals-empty` | `/srv/homelab/med-ask-data/originals` |

Create the file on the server without displaying the password or address. This
refuses to overwrite an existing `.env`:

```sh
sudo sh -s <<'SH'
set -eu
bind_address=$(tailscale ip -4)
test -n "$bind_address"
umask 077
set -C
{
    printf 'POSTGRES_PASSWORD='
    openssl rand -hex 32
    printf 'APP_BIND_ADDRESS=%s\n' "$bind_address"
    printf 'POSTGRES_DATA_PATH=/srv/homelab/med-ask-data/postgres\n'
    printf 'SOURCES_PATH=/srv/homelab/med-ask-data/sources\n'
    printf 'ORIGINALS_PATH=/srv/homelab/med-ask-data/originals\n'
} > /srv/homelab/med-ask/.env
chown root:root /srv/homelab/med-ask/.env
chmod 0600 /srv/homelab/med-ask/.env
SH
```

Keep the address and password out of git, images, PRs and shared logs. On the
server, publish port 8100 only on the Tailscale address; do not use the wildcard,
LAN or loopback address. Postgres publishes no host port. The local and CI
defaults remain on loopback. CI needs only its throwaway `POSTGRES_PASSWORD`.

After the deployment change is merged into `main`, pull, validate and install
the unit on the server:

```sh
sudo git -C /srv/homelab/med-ask pull --ff-only
sudo systemd-analyze verify /srv/homelab/med-ask/deploy/med-ask.service
sudo install -o root -g root -m 0644 /srv/homelab/med-ask/deploy/med-ask.service /etc/systemd/system/med-ask.service
sudo systemctl daemon-reload
sudo systemctl enable med-ask.service
sudo systemctl start med-ask.service
systemctl status med-ask.service
```

Each start attempts an anonymous, fast-forward-only pull of `main`, then builds
and starts Compose. A failed pull leaves the existing checkout available for the
build. Stopping the unit runs `docker compose down` and preserves data. Enabling
adds it to `homelab-data.target`, rather than starting it automatically while the
volume is locked. Ordering after Tailscale still needs the reboot check below
to confirm that its address is ready on this server.

From the Mac over Tailscale, check `http://<tailnet-address>:8100/` for the search
page and `/api/health` for `{"postgres":"ok","vector":"ok"}`. On the server,
`curl http://127.0.0.1:8100/` must fail; `sudo ss -ltn` must show port 8100 only
on the Tailscale address. Confirm the database files exist under
`/srv/homelab/med-ask-data/postgres`. Do not paste the address-bearing output.
Confirm the existing project remains active with
`systemctl is-active homelab.service` and that
`curl -s http://127.0.0.1:8000/health` still answers.

The owner performs the reboot check:

```sh
sudo systemctl reboot
```

Once it is back, on the server:

```sh
sudo data-volume.sh unlock
systemctl status med-ask.service
```

Repeat the page, health, listener, database and existing-project checks after
unlocking, without manually starting med-ask. Stop and investigate with the owner
if binding or this boot sequence fails; do not change other units or networking.

If installation or startup fails, roll back the unit while preserving all data:

```sh
sudo systemctl disable --now med-ask.service
sudo rm /etc/systemd/system/med-ask.service
sudo systemctl daemon-reload
```

To remove the deployment entirely, use that same unit rollback first. Back up
`originals`, any needed database data and the private configuration before asking
the owner to authorize permanent deletion. Only after that authorization:

```sh
sudo rm -rf -- /srv/homelab/med-ask /srv/homelab/med-ask-data
```

This removes only med-ask's clone and data. It leaves the encrypted volume, its
unlock mechanism and the other project's files, services and containers intact.

### Optional public tunnel

Compose runs `app` and `public` from the same image. Both serve the search page
and `/api/health`. `app` enables private routes, including the placeholder
`GET /api/private/ping`, and keeps port 8100 on the configured Tailscale address.
`public` disables private routes: every `/api/private/` path gives the same 404
as an unknown API route. It mounts sources read-only and has no originals mount.
Neither `public` nor the tunnel connector publishes a host port.

Both application services can reach Postgres on the backend network and the
embedding endpoint on `homelab-models`. The hand-run `ingest` service joins those
same two networks and mounts sources read-only and originals read-write. The
`cloudflared` connector joins only the tunnel network shared with `public`; it
cannot connect directly to `app` or Postgres. It connects outward to Cloudflare,
so no new listener, firewall change or tailnet port is needed.

The `cloudflared` service uses the `tunnel` Compose profile. It stays absent on
hosts without that profile, including CI. The owner creates a dashboard-managed
Cloudflared tunnel named `med-ask` under Zero Trust → Networks → Tunnels, copies
the token from the displayed install command without running that command, and
adds a published application route for their chosen hostname. Select service type
HTTP and URL `public:8100` (equivalently `http://public:8100`). Keep the hostname,
tunnel ID and token out of git, PRs and shared logs.

Only the owner writes `TUNNEL_TOKEN` into the existing root-owned, mode `0600`
server `.env`, without displaying it. After setup and merge, add
`COMPOSE_PROFILES=tunnel` there and restart `med-ask.service`. The connector runs
`tunnel --no-autoupdate run` and receives the token through its environment;
never put it on the command line or print the environment file. The public URL
requires Cloudflare Access for search and feedback; the tunnel supplies the signed-in
email header. The app trusts that header only in `public`, whose only external
entry is the tunnel. Private `app` ignores it and records the asker as `tailnet`.

To turn the tunnel off, remove `COMPOSE_PROFILES` from the server `.env` and
restart `med-ask.service`. The unit stops the stack before starting it again, so
the connector is removed and `app` remains available over Tailscale. No data or
volumes need to be removed. The owner may also remove `TUNNEL_TOKEN` to revoke
the local configuration.

## Books, indexing and search

Create `/data/sources/books.toml` outside the repository. The corresponding server
path is `/srv/homelab/med-ask-data/sources/books.toml` (root:root, mode 0644).
Use the format in `src/med_ask/books.example.toml`; its invented books are only an
example:

```toml
[[books]]
id = "sample-biology"
filename = "Example - Biology.pdf"
title = "Example - Biology"
language = "English"
```

Ids are unique lowercase URL-safe identifiers. Filenames are PDF basenames in the
sources directory; escaping paths and symlinks outside it are refused. The loader
never chooses a book from a request-supplied filesystem path.

The existing model stack owns the external `homelab-models` Docker network. Start
that stack before med-ask. CI creates the network itself and tests search using
synthetic PDFs and fake embeddings with a real pgvector database. Nothing calls
the actual embedding endpoint in CI.

`MODEL_BASE_URL` and `MODEL_API_KEY` configure the compatible endpoint, and
`EMBEDDING_PURPOSE` defaults to `embed`. The endpoint's reported identity and
probed dimensions determine the vector table. A changed model needs its own new
index. Ingestion and questions currently embed plain text; separate passage and
query functions allow a future role contract. LlamaIndex owns insertion and exact
pgvector retrieval; there is no approximate vector index.

Run one ingest at a time, manually and detached, choosing ids from the manifest:

```sh
docker compose --profile ingest run -d --rm ingest ingest sample-biology
# The command prints a container id; progress and the final summary go to its log.
docker logs --tail 20 <container-id>
docker compose --profile ingest run --rm ingest status sample-biology
# With no ids, status reports every manifest book against every recorded model.
docker compose --profile ingest run --rm ingest status
```

Each passage has a hash of book id, PDF pages and original text. A repeated command
skips stored passages. Each completed passage is committed, so interruption loses
no committed progress. `status` counts current extracted passage ids against those
stored. Ingesting on the shared CPU model slows searches in both projects; arrange
long runs overnight with the owner. Before any ingest, search returns a clear
"No index yet" response for the current model.

`POST /api/search` accepts `{"question":"…"}`. The app retrieves 30 similarity
candidates through LlamaIndex, then sends the question and one original passage
per `grade` call. Calls run in parallel, capped at 15 per search by
`GRADE_CONCURRENCY`, with a six-second `GRADE_TIMEOUT`. Only complete yes/no
replies count; errors, timeouts and other replies leave a candidate ungraded.
There are no retries or fallback purposes. These settings are read from the
application process environment; Compose uses the code defaults.

The screen says “checking which passages answer this…” until grading finishes.
Only passing passages appear, grouped by book. Passages within a book follow
similarity order, and books follow their best passage. Numbers run across books.
The response includes `groups`, flat numbered `evidence`, `language`,
`ungraded_count`, `not_found`, and embedding, grading and total search seconds.
Candidates that could not be checked are counted and excluded. When none passes,
the screen says “Not found. These books don't cover the question” and requests
no answer. If checks failed, that limitation is displayed beside the result.

Each source includes labels, language, section, OCR provenance and at most one
neighbour on either side in the same section. Neighbours are secondary source
context capped at 120 words each; they are not answer inputs or numbered evidence.
**from OCR — check the page** marks inherited OCR. Review source fetches
`GET /api/page/<book-id>/<one-based-pdf-page>` as an approximately 1,000-pixel-wide
PNG held only in memory, with private one-hour cache headers.

After evidence appears, the browser separately requests `POST /api/answer` with
`question_id`. The `chat` purpose receives only the question and numbered passing
original passages. Its prompt requires at most 150 words in the question's
language, a citation on every sentence, explicit gaps, disagreements, and thin
support. Sentence JSON is validated before attaching citations: empty citations,
unknown numbers, multiple sentences in one item, malformed output and answers
over 150 words are rejected. A generation failure leaves the evidence visible.
The **Generated answer** panel sits above, visibly apart from the authors' text.
Citations identify source passages; factual support still needs source review.

Language detection runs locally with only English, Spanish and Catalan profiles.
Text without detectable letters defaults to English; short ambiguous questions
can be misclassified. **Open translation** appears only across languages.
`POST /api/translate` accepts `question_id` and a displayed `passage_id`, sends
that original alone to `translate`, and returns generated text alongside the
original. Postgres caches it by stable passage id and target language; repeat
requests return `cached: true` without a model call. The browser can also reopen
an already loaded translation immediately. The rebuildable cache is excluded
from question exports and originals. `GENERATION_TIMEOUT` defaults to 20 seconds
for answers and translations. All purposes use `MODEL_BASE_URL` and
`MODEL_API_KEY`; no asker identity is sent to any purpose.

Every valid question is logged before search, including attempts with no index.
The app creates bookkeeping tables and adds missing log columns automatically.
The log stores the NUL-stripped question, asker, timestamp, detected language,
vector table, every candidate's id/score/label and yes/no/ungraded flag, ungraded
count, numbered passing evidence snapshots, generated answer, and optional
feedback. Old ungraded log rows cannot generate answers. Question exports include
these fields but exclude the translation cache.

Thumbs and comments judge the whole response and sit below answer and evidence.
NUL characters are removed from questions and comments before storage.
`POST /api/feedback`, answer and translation requests are limited to the
requester's existing question id; private users share the literal `tailnet`
identity. In `public` a missing Access email header is rejected, and two Access
identities cannot read or update one another's response through these routes.
Keep `public` reachable only through the Access-protected tunnel; its email header
is trusted on that boundary.

Export the whole question log by hand:

```sh
docker compose --profile ingest run --rm ingest export-questions
```

The command creates a uniquely named JSON Lines file under `/data/originals/` and
prints its path and byte size. It overwrites nothing. Only `app` and `ingest` mount
originals; `public` has no access. No exports or ingests are scheduled.

## Retrieval eval

The eval set measures how often search returns the pages that hold the evidence,
per embedding model, and how fast a question is embedded. It is private and lives
outside git at `/data/originals/eval/med-ask-eval.v1.jsonl` in `ingest`
(`/srv/homelab/med-ask-data/originals/eval/` on the server). Never copy it, print
its questions, or put any part of it in tests or the repository. `EVAL_FILE`
overrides the path and `EVAL_RUNS_DIR` the results directory.

Map each manifest book to its eval book id with an optional `eval_book` key, as in
`src/med_ask/books.example.toml`. Evidence for an eval book that no manifest entry
names counts as not indexed.

Run one embedding purpose against the eval set, using the app's own search:

```sh
docker compose --profile ingest run --rm ingest eval
# Another indexed model, by naming its purpose:
docker compose --profile ingest run --rm ingest eval --purpose <purpose>
```

`--purpose` defaults to `EMBEDDING_PURPOSE`. Only records with status `confirmed`
are scored; `--status pages_open` (repeatable) adds others for diagnostics. The
run never grades, generates or logs a question. It prints a short summary and
writes `/data/originals/eval/runs/run-<UTC time>-<purpose>-<suffix>.json`, never
overwriting. Both hold ids, scores, ranks, page numbers and timings only; no
question text.

What is scored:

- A retrieved passage hits if any of its PDF pages is a confirmed page of that
  eval book. A range counts every page in it, with no ±1 tolerance.
- Only `confirmed` evidence on selected records counts. Pages with
  `text: "missing"`, unconfirmed or `ocr_missing` evidence, and books not indexed
  are left out. An answerable record with nothing scoreable is reported as skipped.
- The gate is Hit@5 on answerable questions. Hit@10, work coverage@10 (for
  questions with evidence in two or more works, the share of works with a top-10
  hit; two editions of one work count once), and Hit@5 per language, type,
  subject and book are diagnostics.
- Embedding time is the median and slowest of the first 20 questions in file
  order, after one warm-up request with fixed text.
- Not-found questions stay out of the gate; their top similarity is reported
  beside the answerable questions' top similarity.
- Records with a wrong schema, an unknown status or evidence state, or a page
  item that is neither `pdf` nor a `pdf_from`/`pdf_to` range are reported by id
  and skipped. Retired records are not run.

Compare a baseline run with a candidate run. Bare file names are looked up in the
runs directory:

```sh
docker compose --profile ingest run --rm ingest eval --compare <baseline.json> <candidate.json>
```

The candidate passes only if total Hit@5 does not drop and no question that hit
before is lost unless another question gains. The command names lost and gained
questions and exits 0 on pass, 1 on fail, and 2 when it refuses, for example when
the runs used different `set_version`s.
