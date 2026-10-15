# Operations

## Compose details

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

All med-ask files live on that volume except the installed systemd units:

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

Install the daily question-log export separately, while the volume is mounted
and `med-ask.service` is active. This installation does not restart the app or
build images:

```sh
sudo git -C /srv/homelab/med-ask pull --ff-only
sudo systemd-analyze verify /srv/homelab/med-ask/deploy/med-ask-export.service /srv/homelab/med-ask/deploy/med-ask-export.timer
sudo install -o root -g root -m 0644 /srv/homelab/med-ask/deploy/med-ask-export.service /srv/homelab/med-ask/deploy/med-ask-export.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now med-ask-export.timer
sudo systemctl start med-ask-export.service
systemctl list-timers med-ask-export.timer
systemctl status med-ask-export.service
sudo journalctl -u med-ask-export.service
sudo stat -c '%a %u:%g %s bytes' /srv/homelab/med-ask-data/originals/questions.jsonl
sudo wc -l /srv/homelab/med-ask-data/originals/questions.jsonl
sudo docker compose -f /srv/homelab/med-ask/compose.yaml exec -T postgres psql -U medask -d medask -Atc 'SELECT count(*) FROM medask_questions'
```

The timer runs at 03:00 server-local time daily. `Persistent=true` catches up
after downtime; a locked volume or inactive app skips the export. Check status
and the journal for skips or failures; no failure notifier is configured. The
service uses the existing image, with the clone's `src` mounted read-only and
`PYTHONPATH` selecting that code, so a pull updates export behaviour without a
build. `--no-deps --pull never` prevents it starting dependencies or pulling an
image. Files are owned by UID/GID 10001 with mode 0600. Compare only row counts
and byte sizes; do not print question rows. Start the export service again to
check that the same path is replaced and the count still matches the table.

To roll back scheduling, leave the exports in place and remove only the two
new installed units:

```sh
sudo systemctl disable --now med-ask-export.timer
sudo rm /etc/systemd/system/med-ask-export.timer /etc/systemd/system/med-ask-export.service
sudo systemctl daemon-reload
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
probed dimensions and `INDEX_VERSION` (default `v2`) determine the vector table.
Tables use `e_v2_<model stem>_<model hash>` (Postgres adds `data_`). A changed
model or index version needs its own new index; old tables and their rows remain.
Search and the eval runner select the current version.
`EMBEDDING_ROLES` defaults to `false`; only the exact value `true` enables roles.
Off sends the original request without an `input_type` field. On adds
`input_type: query` for questions and `input_type: passage` for passages through
the client's `extra_body`, leaving text unchanged. The gateway supplies each
purpose's role wording.

Role indexes use `e_v2r_<model stem>_<model hash>` so `embed` roles never reuse a
plain table. **Table rule:** `embed-large` always uses the plain `e_v2_` name,
with roles on or off, because its passage role leaves passage vectors unchanged.
One plain index therefore serves both of its query modes. The role mark still
fits PostgreSQL's identifier limit. The student app stays on `embed`, roles off,
with its existing table throughout the comparison. LlamaIndex owns insertion
and exact pgvector retrieval; there is no approximate vector index.

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
stored, for every recorded table, including previous versions. To rebuild into a new version, wait for every existing ingest to finish before
pulling code or rebuilding the shared image. Build with `docker compose build`;
the running app and public containers keep their existing image and index. Pass
`-e INDEX_VERSION=v3` to each new ingest, one book at a time, and confirm
stored = extracted for every book in the new tables with `status`. Set
`INDEX_VERSION=v3` in the server environment and restart `med-ask.service` only
after every book is complete and no ingest is running. Compose passes the setting
to app, public and ingest; search and eval use it too. Restoring `v2` and restarting
selects the preserved old tables. Merging code alone keeps the app on `v2`. Nothing deletes or overwrites the old
index. Ingesting on the shared CPU model slows searches in both projects; arrange
long runs overnight with the owner. Before any ingest, search returns a clear
"No index yet" response for the current model.

### The Mac's models

The Mac's login-started llama.cpp router serves both `vision` and
`embed-large-bulk`; nothing needs to be started by hand. The first call loads
its model: `vision` takes about 9 seconds from cold and then answers in about
1.5–2 seconds; a batch of 20 embeddings takes about 0.8–1.4 seconds. Each model
is released after 10 minutes idle, so a later call loads it again. The
`embed-large-bulk` and `embed-large` purposes report the same identity and
2,560 dimensions, and their `model_table` names are unchanged.

Until the Mac's usage profiles ship, both purposes are allowed. In the coming
Working profile, `embed-large-bulk` is allowed but `vision` is not; a vision
call there answers “not available until Inference”. Page reading with vision
must wait until the owner switches to Inference, typically overnight. Before
a long build or reading run, make sure the Mac is awake and its current profile
allows the purpose. A fast generic HTTP 500 (under about 1.3 seconds) means the
Mac may be off, asleep or its router stopped. For either that failure or “not
available until Inference”, stop, report and resume the job later. Builds and
readings resume by skipping work already stored.

### Reading pages with vision

The external service is used only through the `vision` purpose at `MODEL_BASE_URL`,
with `MODEL_API_KEY`. Follow **The Mac's models** subsection before and during a
reading run. Pages are sent
one at a time, as in-memory `data:` PNGs at 300 DPI, temperature 0 and at most
4,096 completion tokens. The fixed transcription prompt preserves the printed
language and spelling, joins line-end hyphenation, separates paragraphs, and
replaces figure interiors with `[FIGURE n]` followed by the printed caption.
It uses no second model and never merges two readings.

Every page is checked before a call. Born-digital publisher text is read only
when it is nearly empty while the page shows ink. Readings and inherited OCR also
get language, malformed-word and printed-number checks. Blank pages are kept.
A page whose whole text is `Hidden page` (ignoring surrounding or repeated
whitespace) is a missing-content placeholder. Its page account records `hidden`;
it produces no passage, is never read or queued, and is counted separately as
`hidden` in extraction reports, `ocr-queue`, and ingestion `status`. Missing hidden
pages prevent paragraph joins across them. Existing reading files are preserved
but ignored for these placeholders.
The inspectable thresholds in `ocr.py` are:

| Reason | Rule |
| --- | --- |
| `empty-ink` | Fewer than 40 trimmed characters on a page with ink |
| `language` | At least 1,000 characters; the app's existing detector disagrees with the book language |
| `nonwords` | More than 50% malformed tokens among at least 50 eligible tokens |
| `number-missing` | No whole decimal or Roman number (either case) in a reading's first or last non-watermark line, or an inherited text layer's margins |
| `number-order` | A printed number conflicts with nearby numbers of the same numbering family, within two PDF pages |
| `token-limit` | A vision reply finishes with `length` |
| `page-timeout` | A page request exceeds the 180-second timeout |

The ink test renders the inner page (excluding 2% at each edge) at 72 DPI in
memory; more than 0.1% of its grayscale pixels must be darker than intensity 180.
Malformed tokens are whitespace tokens after ordinary punctuation is stripped,
excluding tokens containing digits. A word needs two letters, at least 70%
alphabetic characters and an English/Spanish vowel (including accented vowels
and y). This is a shape check, not a dictionary: scientific terminology is kept.
Without comparable neighbouring numbers a visible number is provisionally kept;
extraction still requires the existing sequence verification before labelling
it as a confidently read printed page.

Read failing pages and inspect the queue without any model call:

```sh
docker compose --profile ingest run --rm ingest ocr-queue mathews passarge alberts
# A bounded first run; the limit counts new page readings, including their retry.
docker compose --profile ingest run --rm ingest ocr alberts --limit 20
# Full runs are detached, one book at a time.
docker compose --profile ingest run -d --rm ingest ocr <book-id>
docker logs --tail 20 <container-id>
```

`ocr` and `ocr-queue` need the external manifest but not Postgres. The queue lists
PDF pages, short reason codes and counts, including pages awaiting a first reading.
It reports hidden / kept / read and passing / queued totals, word-shape quantiles,
seconds for completed calls, rotation retries and rescues. It sends nothing elsewhere.
An upright reading gets one retry rotated 180 degrees only for `empty-ink` or
`nonwords` or `page-timeout`. A timeout is saved as an empty failed attempt with
`finish_reason: timeout`, then gets at most one retry; after two timeouts the
page stays queued and the run continues. A completed reply wins over a timed-out
attempt; completed replies otherwise use the fewest failed rules, upright on ties.
Number, language and token-limit failures alone queue it without a
retry. Both replies remain intact.
Roman numerals in either case use their usual ordering. A whole integer or Roman
token beside text at the start or end of an edge line counts when neighbouring
pages support its sequence. Decimal figure and section numbers do not count.
The words `Copyrighted material` are removed from readings used for extraction,
retaining a number on the same line; saved text and attempts remain untouched.
Run `ocr-recheck <book-id>` to atomically refresh kept readings' reasons and
finalize partial replies that no longer need rotation, without model calls.
After a checking correction, re-check each book before its versioned ingest and
record queue totals and reason counts before and after. An existing OCR container
keeps its original image: building the updated image does not replace that
container. Wait for embedding ingests to exit before building, leave the reader
untouched, and use the updated image for re-checks and subsequent ingests.
`ocr` also performs this re-check before resuming; the reading method version
stays unchanged when only these checking rules change.

Kept readings live only at `/data/originals/ocr/<book-id>/<pdf-page>.json`, covered
by the originals backup. Each contains the selected text, `vision` rung, endpoint's
reported model, method version, rotation, failed rules, seconds and UTC time,
plus every attempt. A synced temporary file beside the destination is renamed
atomically; failure leaves the previous file intact. `METHOD_VERSION` covers the
prompt, DPI and settings and must change when any of them changes. Older-version
records are retained inside the replacement file. Current-version attempts are
never repeated, even for queued pages. If interrupted after an upright reply,
resume performs only its pending rotation if the amended retry rules require it.
A page timeout is a durable failed reading, not an outage.
An HTTP 500 or lost connection stops
cleanly with exit code 3: no failed call is marked as a reading. Run the same command
again to continue. An already completed reply stays durable if its retry is offline.

Extraction without current readings preserves the existing paragraphs and counts.
With readings it uses blank-line blocks as paragraphs, drops the watermark line
`Copyrighted material`, and stores figure markers and their captions separately.
Readings apply the same contents/index title exclusion as PDF text. Exact kind
headings (including spaced letters) override bookmark kinds. A new chapter or
bookmark closes the prior section; repeated headers for the same chapter retain
its subsection. Standalone chapter numbers and uppercase chapter running titles
are recognised even without bookmarks. Chapter-opening contents outlines remain
content. A front-matter heading such as Acknowledgments or Preface ends a coarse
contents bookmark. Numbered section titles and matching PDF typography identify
reading headings; short uppercase or title-case lines are also recognised (at
most 14 words and 120 characters, without sentence/list punctuation). Matching
PDF heading sizes set hierarchy; headings without typography use one peer level.
A heading of the same or a higher level closes the preceding kind section.
Ordinary prose remains source text. Sustained
repetition of a 1–64-token pattern (at least eight copies, 64 tokens and 200
characters), or 200 copies of a horizontal whitespace character, cuts the tail
at its first occurrence when passages are built. Saved readings are untouched;
the original failures and queued-page marker remain on the retained prefix. The
first or last line supplies a printed number only when the sequence supports it;
the existing numbering inference remains the fallback.
Passing OCR paragraphs can join across pages. Queued pages and inherited OCR in
a book being rebuilt from readings cannot. Every passage records `born-digital`,
`inherited-ocr` or `ocr`; queued passages retain their reasons and are indexed
anyway. Their screen label says **from OCR — check the page**. Passing readings
say **from OCR**; both are transcriptions of source pages, separate from generated
answers and translations.

### Server OCR rebuild after the embedding comparison

Proceed only after the owner closes the comparison and names the app's embedding
setting. The approved setting for this rebuild is `embed-large`, roles off. First
check that no container with Compose service label `ingest` is running. In the
server's private `.env`, set `EMBEDDING_PURPOSE=embed-large`,
`EMBEDDING_ROLES=false`, and keep `INDEX_VERSION=v2`. Restart `med-ask.service`;
it pulls main and rebuilds. Confirm health and a search plus generated answer over
Tailscale, and check that the tailnet question's recorded table is the completed
plain v2 table. Keep the private address and secrets out of logs. Rollback is
`EMBEDDING_PURPOSE=embed`, roles off, followed by a restart while no ingest runs.

Check for running ingest containers again, pull the clone and run
`docker compose build`. The running app keeps its image and v2 index. Add Stryer
to the external manifest as `stryer`, language `Spanish`, with
`eval_book = "stryer-bioquimica-6-es"`. Keep its original PDF filename.

Measure before the full run. Inspect the rules for all books, then read at most
20 eligible pages per book with `ocr <book-id> --limit 20`. Born-digital pages
that pass the empty-with-ink check must keep their publisher text: if a book has
fewer than 20 eligible pages, report that count rather than forcing readings.
Report kept, hidden, read-and-passing and queued counts, rule failures, reading
seconds per page, rotation retries and rescues. Readings remain only under
`/data/originals/ocr/`. If vision is unavailable, follow **The Mac's models**
subsection. Stop and report repeated token-limit failures, atomic-write failures,
or an ingest failure; do not switch the index after a failed run.

Stop and report the completed measurement before starting any full run. Include
any upside-down Alberts sample page, its upright reading's line order and failed rules.

Run full OCR detached, one book at a time, in this order: `mathews`, `stryer`,
`alberts`. Passarge has no pages to read. Keep each container until its exit code
and log have been checked, then remove only that exited container. Never remove data or volumes.
These commands deliberately omit `--rm` so failed-run logs remain available:

```sh
docker compose --profile ingest run -d --name med-ask-ocr-<book-id> ingest ocr <book-id>
docker logs --timestamps --tail 20 med-ask-ocr-<book-id>
docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' med-ask-ocr-<book-id>
# After exit, read its completed log and verify success before removal.
docker logs med-ask-ocr-<book-id>
docker rm med-ask-ocr-<book-id>
```

Ingest each book into v3 as soon as its full reading run is final and its queue
is settled: `passarge` first, then `mathews`, `stryer`, and `alberts`. Keep exactly
one embedding ingest active on the server; a ready book waits for that ingest
to finish. Reading on the external machine may overlap a server ingest for a
different book. Never ingest a book while it is still being read. Use the app's
embedding setting. Pass `EMBEDDING_TIMEOUT=120` to **every** v3
ingest: long passages can take about 45 seconds at about 20 tokens/s on the
server's four cores. The app and eval keep their default 20-second timeout.
Expect roughly 0.14–0.26 passages/s (about 70 characters/s), around 30 hours for
the three original books plus Stryer. Leave the active ingest detached and report
each book's measured rate from timestamped progress logs; do not build or restart
while any ingest is running.

```sh
docker compose --profile ingest run -d --name med-ask-v3-<book-id> -e INDEX_VERSION=v3 -e EMBEDDING_PURPOSE=embed-large -e EMBEDDING_ROLES=false -e EMBEDDING_TIMEOUT=120 ingest ingest <book-id>
docker logs --timestamps --tail 20 med-ask-v3-<book-id>
# After exit, check the exit code, read the log, and remove the exited container.
docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' med-ask-v3-<book-id>
docker logs med-ask-v3-<book-id>
docker rm med-ask-v3-<book-id>
docker compose --profile ingest run --name med-ask-v3-status -e INDEX_VERSION=v3 ingest status stryer passarge alberts mathews
docker logs med-ask-v3-status
docker rm med-ask-v3-status
```

Confirm every current passage id of every book is stored in the selected v3
table. Only then set `INDEX_VERSION=v3` in `.env` and restart `med-ask.service`.
Confirm both `homelab.service` and `med-ask.service` are active. Probe over
Tailscale with two Spanish biochemistry questions and inspect only the caller's
`asker=tailnet` candidates to establish whether Stryer appears. Keep source text
out of probe output. Never drop, delete from or rename vector tables.

Finally, run eval once on the app's setting and compare it with the named plain
v2 baseline, without reading or printing eval questions. Keep both containers
until their logs and exit codes have been read, then remove them:

```sh
docker compose --profile ingest run --name med-ask-v3-eval ingest eval
# The log reports the newly written run filename under originals/eval/runs.
docker logs med-ask-v3-eval
docker rm med-ask-v3-eval
docker compose --profile ingest run --name med-ask-v3-compare ingest eval --compare run-20261010T152043Z-embed-large-roles-off-97cc8d.json <new-run.json>
docker logs med-ask-v3-compare
docker rm med-ask-v3-compare
```

Report only Hit@5, Hit@10 and lost/gained ids from the eval and comparison. The
scoring and gate remain unchanged. Restore `INDEX_VERSION=v2` and restart to
select the preserved pre-OCR tables if the owner requests index rollback.

### v4 correction and Mac bulk ingest

Keep the app on `v2` with `embed-large`, roles off, until the correction gate
passes. Preserve the `v2` and `v3` tables and all kept reading files; do not run
`ocr` or `ocr-recheck` for this rebuild. Before embedding, extract all four books
in memory and review counts by book, kind and text source against both existing
indexes. Report excluded page ranges, shortened passages and largest passage
lengths under `/data/originals/eval/v4-work/`. Stop for review before starting
the bulk route.

Follow **The Mac's models** subsection, then probe `embed-large-bulk` and
`embed-large` once each and require identical reported identity, dimensions
(2,560) and `model_table` names under `INDEX_VERSION=v4`. Stop on any mismatch.
The bulk purpose has no fallback; it is for ingestion only. Give its client a
300-second timeout. Questions and eval continue through `embed-large`.

Run all four books sequentially in one retained detached container:

```sh
docker compose --profile ingest run -d --name med-ask-v4-build -e INDEX_VERSION=v4 -e EMBEDDING_PURPOSE=embed-large-bulk -e EMBEDDING_ROLES=false -e EMBEDDING_TIMEOUT=300 ingest ingest alberts mathews passarge stryer
# Wait for the exit notification; do not poll. Read the final log once.
docker logs med-ask-v4-build
```

Record stored/extracted counts, duplicate-id gaps and elapsed rate. Long node
steps use retained detached containers named `med-ask-v4-<step>` and results in
`/data/originals/eval/v4-work/` or their log. Score `v2`, `v3` and `v4` through
`embed-large` on `med-ask-eval.v2.jsonl` (the new default; v1 remains untouched),
recording Hit@5 and Hit@10 overall and on the 20-question three-book cohort,
plus changed ids q001–q032. Never print question text.

Switch only if `v4` reaches Hit@5 at least 29/32 overall and 18/20 on that
cohort. Otherwise keep the app on `v2`. Before switching, record the rollback:
set only `INDEX_VERSION=v2` in the server's private `.env` without displaying
its contents, then `sudo systemctl restart med-ask.service`. On pass set only
`INDEX_VERSION=v4`, restart, check health and a Spanish Stryer probe on the node.
The app's embedding purpose remains `embed-large`, roles off.

### Embedding comparison arms

| Arm | `EMBEDDING_PURPOSE` | `EMBEDDING_ROLES` | Index |
| --- | --- | --- | --- |
| A | `embed` | `false` | Existing plain table |
| B | `embed` | `true` | Separate role table |
| C | `embed-large` | `false` | New plain table |
| D | `embed-large` | `true` | C's plain table |

After merging, check no ingest container is running and restart
`med-ask.service` to rebuild the shared image. Leave the app's settings at A.
For B and C, run the following detached commands **one at a time**, waiting for
successful completion before starting the next book or arm:

```sh
# B: repeat for passarge and alberts after mathews completes.
docker compose --profile ingest run -d --rm -e EMBEDDING_PURPOSE=embed -e EMBEDDING_ROLES=true ingest ingest mathews
# C: repeat for passarge and alberts after mathews completes.
docker compose --profile ingest run -d --rm -e EMBEDDING_PURPOSE=embed-large -e EMBEDDING_ROLES=false ingest ingest mathews
docker compose --profile ingest run --rm ingest status mathews passarge alberts
```

D needs no additional ingest. Confirm all current unique passage ids are stored
for each book in both new tables; a duplicate extracted passage is stored once.
These runs can take days on the shared CPU. Keep them detached and monitor
progress and rates. Existing tables are preserved.

After the owner chooses an arm, set **both** `EMBEDDING_PURPOSE` and
`EMBEDDING_ROLES` in the server environment to that arm's values and restart
`med-ask.service` when no ingest is running. All its books must already be fully
indexed. Compose passes these settings to app, public and ingest. Changing the
two settings back to A and restarting restores the baseline table without
changing any stored rows.

Every passage has a metadata kind: `content`, `summary`, `glossary`, `exercise`,
or `references`. The nearest recognised heading in its section path sets the kind,
reading upward through nested headings. Matching ignores case, surrounding
whitespace and spaces within letter-spaced titles; unknown headings mean content.
The vocabulary covers Summary/Resumen, Glossary/Glosario, Problems/Problemas,
Respuestas a problemas/Respuestas a los problemas, Reference/References,
Bibliografía, General References/Referencias generales, and Selected Introductory
Reading. Kind changes neither the passage hash nor its embedding input.
All kinds are stored. Vector retrieval filters to content, summary and glossary
before selecting candidates; exercises and references are never candidates,
including in retrieval eval. Summary and glossary appear beside the source label
on screen.

Unfinished content sentences are joined across pages in the same book and exact
section when the next passage starts in lower case or the first ends in a
hyphenated word. Only pages without body passages may intervene, so a figure-only
page or a caption before the continuation no longer breaks the join. A trailing
hyphen is removed and the word rejoined; PDF and verified printed page ranges
extend across the join. The additional joins exclude inherited OCR and every
other kind, and preserve the extractor's other passage boundaries.

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
**from OCR — check the page** marks inherited OCR and queued page readings. Review source fetches
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

The command writes `/data/originals/questions.jsonl` on every run, printing its
path and byte size. It streams one `row_to_json` question row per line in
`asked_at` order, excluding the translation cache. It writes a temporary file
in the same directory, then atomically replaces the previous export. An equal
or larger row count replaces it; a smaller count prints a refusal and exits
non-zero, keeping the previous file byte-identical and removing the temporary
file. With no previous file, even an empty export is written. An interrupted
write preserves the previous export; an abrupt process kill may leave a
temporary file but cannot publish a partial export. Existing uniquely named
exports stay in place. Only `app` and `ingest` mount originals; `public` has no
access. The server timer above schedules exports; ingestion remains manual.
Until the image is next rebuilt, use `sudo systemctl start med-ask-export.service`
on the server to run the updated command from the clone.

## Retrieval eval

### Figures and the v5 index

Figure entries use the extractor's separated captions: `[FIGURE n]` markers in
kept readings and paragraphs starting with Figure, Figura or Fig. in text layers.
They preserve the caption, label, book, PDF page and verified printed page. Stable
ids include source identity and occurrence, so unnumbered figures on one page
stay separate. Opening a figure uses **Review source**, showing the whole page.

Passarge uses the settled atlas rule instead of numbered captions: one entry for
each of the 198 plate pages, comprising the 193 even PDF pages 32–420 except
244 and 274, plus PDF pages 422–426. Introduction illustrations on PDF 15–18
are excluded. The label is the printed plate title, and the caption is the
facing legend's printed text, read in column order with running margins removed.
The five chromosome-map pages share the printed title from PDF 422 and the
legend on PDF 421. This rule is specific to the selected edition; a missing
plate title, legend or settled page range refuses extraction.

Passarge records keep both locations: `pdf_page` is the plate and
`legend_pdf_page` is the caption source, with independently verified printed
pages for each. Descriptions always render the plate. Figure evidence uses the
legend's PDF and printed page for citation and evaluation; separate
`figure_pdf_page` metadata makes Review source open the plate. The screen names
both locations. A passage mentioning a plate's printed title brings matching
plates from that book. Other books retain their existing stable figure ids and
kept descriptions, and ordinary same-page figures behave as before.

The current v5 index uses **labels and printed captions only**. Figure extraction,
ingestion and status do not read kept description files or require descriptions.
Figure nodes embed the label followed by the printed caption. Relevance grading
receives only that printed label and caption. Descriptions are ignored even if
supplied by an old caller or stored in node metadata. The evidence column shows
the printed caption and source location; the “found by its description” note is
absent. Answers and translations continue to receive captions only.

V5 retrieves passages and figures separately with one question embedding. The
passage query keeps its existing kind filter, count and similarity order. Figures
follow all passages and cannot displace a passage: the app requests 26 passages
and adds at most 4 distinct figures, so it grades at most 30 candidates. Container
environment variables `PASSAGE_CANDIDATES` (default 26) and `FIGURE_CANDIDATES`
(default 4) override these caps; the passage cap must stay at least 10 to retain
the measured gate. The first half of the figure slots (two by default) go to direct
figure matches, followed by same-book passage references;
unused reference slots are filled by remaining direct matches. Referenced figures
are deduplicated and kept in passage order. Passarge linking remains title-based;
legend-to-plate linking is deferred to a later panel extraction slice.

The description job remains available for a later explicitly authorized run.
It makes one `vision` request with the whole page at 300 DPI plus label and caption,
writing a generated record under `/data/originals/figures/<book>/<figure-id>.json`.
Files are synced and atomically renamed; reruns skip terminal records, including
failures and earlier methods. Timeouts are failures, while an endpoint outage
stops with resumable progress. Existing sample descriptions and the failed sample
record remain on the node, unused by v5. No description job or `vision` server is
needed for this build. Explicit `load_descriptions=True` or `require_records=True`
in the Python extraction helper is reserved for later description review; the
active indexing path uses neither.

Run node steps longer than about a minute as retained detached containers named
`med-ask-fig-<step>`, without `--rm`. Never poll them. Hand off the container name,
log location and completion command, `docker wait <container>`; after completion,
read its exit status and final log once. Summaries belong only under
`/data/originals/eval/fig-work/` or in the container log.

Count with no model calls:

```sh
docker compose --profile ingest run -d --name med-ask-fig-count ingest figures-count alberts mathews passarge stryer --output /data/originals/eval/fig-work/counts.json
```

The measured figure counts are Alberts 1,734, Mathews 914, Stryer 851 and Passarge
198 (3,697 total). Captionless and unnumbered entries remain visible in count
summaries for source review. Stop if a book's figures cannot be found reliably.
The retained description command, for a later run with explicit authorization,
follows **The Mac's models** subsection for `vision` availability:

```sh
docker compose --profile ingest run -d --name med-ask-fig-describe ingest figures-describe alberts mathews passarge stryer --output /data/originals/eval/fig-work/descriptions.json
```

Do not run it for the current captions-only index. Follow **The Mac's models**
subsection for `embed-large-bulk` availability, then build only v5, leaving v4
untouched:

```sh
docker compose --profile ingest run -d --name med-ask-fig-build -e INDEX_VERSION=v5 -e EMBEDDING_PURPOSE=embed-large-bulk -e EMBEDDING_ROLES=false -e EMBEDDING_TIMEOUT=300 ingest ingest alberts mathews passarge stryer
```

The bulk purpose's table identity is the same as `embed-large` uses for queries.
Never send questions through the bulk purpose or build through node `embed-large`.
Only v5 adds kind `figure` to ingestion and the vector-search filter. Prose
references to figure numbers or Passarge plate titles attach matching figures
from the same book, deduplicated after the original vector matches; original
top-k ranks remain intact. No model call or description file is needed to form
these links. Sample descriptions remain unchanged and unused throughout the
build, grading and browser response.

Record build counts per book and kind, stored/extracted totals, duplicate-id gaps
and elapsed passages/second. Each current unique id must be stored; a repeated
prose id accounts for an extracted/stored gap. Ingest resumes missing ids only.

Score through the node's `embed-large`, roles off. Saved v4 numbers can be reused
when its passage retrieval has not changed. Eval text must
stay on the node: never print, log, copy, send to the Mac or include it in a PR.
Result files contain ids, scores, ranks and timings only.

```sh
docker compose --profile ingest run -d --name med-ask-fig-score-v4 -e INDEX_VERSION=v4 -e EVAL_RUNS_DIR=/data/originals/eval/fig-work ingest eval --purpose embed-large --no-roles
# Start after v4 scoring finishes.
docker compose --profile ingest run -d --name med-ask-fig-score-v5 -e INDEX_VERSION=v5 -e EVAL_RUNS_DIR=/data/originals/eval/fig-work ingest eval --purpose embed-large --no-roles
docker compose --profile ingest run --name med-ask-fig-gate ingest eval --figure-gate /data/originals/eval/fig-work/<v4-result>.json /data/originals/eval/fig-work/<v5-result>.json --cohort /data/originals/eval/fig-work/cohort.json
```

Evaluation removes figures before assigning ranks or scoring: the gate counts
passages only, and reports mark `ranking_scope` as `passages`. Keep the same
20 `cohort_ids` in `fig-work/cohort.json`; select them by expected books, never by
outcomes. The figure gate reports Hit@5 and Hit@10 overall and for `cohort_ids`. It requires
matching 32 scored ids, all 20 cohort ids and the measured v4 baseline of 32/32
and 20/20 at 10. It passes only if v5 also reaches 32/32 and 20/20 at 10. The
ordinary eval comparison command still applies its original Hit@5 gate.
Write three figure questions from real printed captions, separate from eval,
run search without application question logging, and report each figure's rank.

Switch only after the gate passes. Before the switch, type the rollback in the
handover: set only `INDEX_VERSION=v4` in the server's private `.env`, without
displaying its contents, and `sudo systemctl restart med-ask.service`. On pass,
set only `INDEX_VERSION=v5` and restart. Keep `embed-large` and roles off. Check
`/api/health`, then run a figure probe through the app and check the evidence
column shows its printed caption, opens its whole page and hides the description.
If the gate fails, leave the app on v4 and report. Never alter v2, v3 or v4 tables.

The eval set measures how often search returns the pages that hold the evidence,
per embedding model, and how fast a question is embedded. It is private and lives
outside git at `/data/originals/eval/med-ask-eval.v2.jsonl` in `ingest`
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

The eval uses `INDEX_VERSION` (default `v2`) through the app's search, without
changing its scoring or gate. Pass `-e INDEX_VERSION=v3` to evaluate the rebuilt
tables before switching the running app.

`--purpose` defaults to `EMBEDDING_PURPOSE`. `--roles` and `--no-roles` override
`EMBEDDING_ROLES` for the run. Only records with status `confirmed`
are scored; `--status pages_open` (repeatable) adds others for diagnostics. The
run never grades, generates or logs a question. It prints a short summary and
writes `/data/originals/eval/runs/run-<UTC time>-<purpose>-roles-<on|off>-<suffix>.json`, never
overwriting. Both hold ids, scores, ranks, page numbers and timings only; no
question text. The file records `purpose` and boolean `roles`, and summaries
and comparisons show both. Older result files without `roles` mean roles off.

Run all four arms after B and C are complete:

```sh
docker compose --profile ingest run --rm ingest eval --purpose embed --no-roles
# Keep this result filename as A.
docker compose --profile ingest run --rm ingest eval --purpose embed --roles
# Keep this result filename as B.
docker compose --profile ingest run --rm ingest eval --purpose embed-large --no-roles
# Keep this result filename as C.
docker compose --profile ingest run --rm ingest eval --purpose embed-large --roles
# Keep this result filename as D.
docker compose --profile ingest run --rm ingest eval --compare <A.json> <B.json>
docker compose --profile ingest run --rm ingest eval --compare <A.json> <C.json>
docker compose --profile ingest run --rm ingest eval --compare <A.json> <D.json>
```

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

### Resolve queued pages over the tailnet

Only `app`, with `PRIVATE_ROUTES_ENABLED=true`, registers the resolve API and
shows **Resolve issues**. The tailnet is the lock; there are no logins or roles.
`public` returns the ordinary unknown-route 404 for every `/api/private/` path,
including all methods on the resolve routes. It does not mount originals.

`GET /api/private/resolve/queue` accepts optional `book` and `reason` filters.
`GET /api/private/resolve/page/<book-id>/<pdf-page>` returns the kept reading,
printed number and plain-language reasons. The screen shows it beside the
existing `/api/page/<book-id>/<pdf-page>` image.
`POST .../resolution` accepts `action: confirm`, `number` with `printed_page`,
`text` with replacement `text`, or `hinted` with an `attempt_id` from
`POST .../hint` (which accepts `hint`). Browser writes must have the same origin.

Resolutions live in `/data/originals/resolutions/<book-id>/<pdf-page>.json`.
Each atomic page file has an append-only `history` of timestamped decisions,
resolved text and printed numbers. These files can contain book text: never
copy them into git. Writes are serialized per book across app workers.
Every later extraction uses the latest resolution, clears the check-page marker,
and queue inspection omits the resolved page. The index changes **only at the
next rebuild**. Saving a resolution performs no ingest, embedding or database write.

A hinted reading makes exactly one `vision` request, with the owner hint added
to the transcription prompt. It has a 20-second timeout and no retry. If vision
is not serving, the screen says so; the queue and other actions still work.
The candidate is labelled generated and must be accepted or discarded. Its
hinted attempt is appended to the kept reading history without replacing the
selected reading. Discarding dismisses the candidate; the attempt stays in
history. Accepting writes a resolution. Kept reading text is otherwise untouched.

#### Build resolve diagnostics before using the screen

Resolve requests never inspect a PDF. Build the source-side diagnostic cache
once after deployment, and rebuild after PDF or kept-reading changes:

```sh
docker compose --profile ingest run -d --no-deps --name med-ask-resolve-cache ingest resolve-cache
# Wait for completion; do not poll. The retained container log has per-book totals.
docker wait med-ask-resolve-cache
docker logs med-ask-resolve-cache
```

Pass book ids after `resolve-cache` to rebuild selected books. This command
inspects sources without models, embeddings, database writes or changes to
kept readings. It writes atomic diagnostic files at
`/data/originals/resolve-cache/<book-id>/diagnostics.json`. They contain source
text and must never enter git. Old cache files survive an interrupted rebuild.
A rebuild refuses to publish if its input metadata changed during inspection.

Queue and page requests read this cache and check PDF/reading file metadata.
A missing or stale cache returns HTTP 503 with the exact rebuild command,
without attempting a scan. Worker memory reuses parsed files until they change.
Resolutions are read live and do not stale the cache. Appending a hinted attempt
advances only that reading's cache metadata, preserving the selected text and
all other input fingerprints; unrelated changes still require a rebuild.
The existing page-image route and a hinted reading may render their one requested
page. Queue, page detail and saving resolutions never open a PDF.

## Passarge panels and the v6 index

Figures are enabled for v5 and every later numeric version. v5 keeps its whole
Passarge plates and stored title links. v6 splits the 192 ordinary plates with
lettered legends into one entry per panel. The unlettered plate on PDF 200 and
the five maps on PDF 422–426 remain whole entries. Panels use the printed plate
title plus letter, cite the legend, and open the whole plate. No descriptions,
crops or model calls are involved in extraction. Other books are unchanged.

Read the two legend columns left to right and split at bold `A.`, `B.`, etc.
headings, stopping before the printed references. Opening text is included only
when it has at most 200 characters and at most one third of that panel's text
length; the panel therefore supplies at least three quarters of the caption.
Legend passage locations select the panel containing their paragraph. If a
passage spans panels or belongs to none, it brings that plate's panels in letter
order. Source text, passage ids, kinds and kept readings remain unchanged.

Leave the app on v5 throughout deployment, counting, building and scoring.
Initialize the v6 vector table with LlamaIndex, copy the unchanged Alberts,
Mathews and Stryer rows with **one SQL INSERT**, and use existing bookkeeping
methods. This needs no embedding endpoint. Save this code on the node under
`/data/originals/eval/fig-work/copy-v6.py`; never print rows or credentials:

```python
import os
from psycopg import sql
from med_ask.database import Database
from med_ask.retrieval import managed_store, vector_store

books = ["alberts", "mathews", "stryer"]
database = Database(os.environ["DATABASE_URL"])
models = [row for row in database.models() if row[0].startswith("e_v5_")]
assert len(models) == 1
source, model, dimensions = models[0]
target = source.replace("e_v5_", "e_v6_", 1)
with managed_store(vector_store, database.url, target, dimensions, setup=True) as store:
    # The library owns table creation, column types, defaults and indexes.
    assert not store.get_nodes(node_ids=["v6-table-initialization"])
with database.connect() as connection:
    connection.execute(
        sql.SQL("""
        INSERT INTO {} (text, metadata_, node_id, embedding)
        SELECT s.text, s.metadata_, s.node_id, s.embedding FROM {} s
        WHERE s.metadata_->>'book_id' = ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM {} t WHERE t.node_id = s.node_id)
    """).format(
            sql.Identifier("data_" + target),
            sql.Identifier("data_" + source),
            sql.Identifier("data_" + target),
        ),
        (books,),
    )
    counts = connection.execute(
        "SELECT book_id, extracted FROM medask_books "
        "WHERE table_name=%s AND book_id=ANY(%s)",
        (source, books),
    ).fetchall()
assert len(counts) == 3
database.register(target, model, dimensions)
for book, count in counts:
    database.book_count(target, book, count)
print("Copied and registered three unchanged books", flush=True)
```

Run node commands via `ssh homelab-agent 'sudo …'` from
`/srv/homelab/med-ask`. The library's table initialization and the single INSERT
are the copy plan; no manual DDL or old-table writes are needed. If the inspected
schema cannot support this plan, use a full detached v6 ingest instead (about
three hours), and report that choice before booking the GPU.

Before embedding, extract Passarge and record panels, whole plates, maps,
passages, unique ids, duplicate ids and legend passages gaining links. Read v5's
per-book/per-kind copy row counts without printing text. Save only these counts
under `/data/originals/eval/fig-work/`. For work over a minute, start a retained
named detached container and hand off its log and `docker wait` command. Never
poll a running job.

The chosen copy plus Passarge build needs approximately 20–30 minutes of the
shared GPU, compared with about three hours for all four books. Request the GPU
and end the turn waiting. Never start, stop or probe a Mac server. When resumed,
follow **The Mac's models** subsection, then copy and ingest Passarge in one
detached container, saving the copy script beforehand:

```sh
docker compose --profile ingest run -d --name med-ask-v6-build -e INDEX_VERSION=v6 -e EMBEDDING_PURPOSE=embed-large-bulk -e EMBEDDING_ROLES=false -e EMBEDDING_TIMEOUT=300 --entrypoint sh ingest -c 'python /data/originals/eval/fig-work/copy-v6.py && python -m med_ask.ingest ingest passarge'
# Completion command, run by whoever resumes the executor:
docker wait med-ask-v6-build
# Read once after completion:
docker logs med-ask-v6-build
```

All v2–v5 tables and their bookkeeping rows remain untouched. Check the stored
v6 ids against extraction, accounting for every count gap with repeated ids.
Score through node `embed-large`, roles off; question text never leaves the node
or enters output. Passage-only ranks must reach Hit@10 32/32 overall and 20/20
on the existing cohort. The v5 baseline is Hit@5 28/32, Hit@10 32/32, cohort
Hit@10 20/20. Do not substitute the ordinary Hit@5 comparison gate.

```sh
docker compose --profile ingest run -d --name med-ask-v6-score -e INDEX_VERSION=v6 -e EVAL_RUNS_DIR=/data/originals/eval/fig-work ingest eval --purpose embed-large --no-roles
# After completion, compare the private v5 passage run and v6 result:
docker compose --profile ingest run --name med-ask-v6-gate ingest eval --figure-gate /data/originals/eval/fig-work/<v5-passage-run>.json /data/originals/eval/fig-work/<v6-passage-run>.json --cohort /data/originals/eval/fig-work/cohort.json
```

Write three new questions from three different plates' panel paragraphs, keeping
the questions only under `/data/originals/eval/fig-work/`. Run the app's 26-passage,
four-figure candidate retrieval and grading without question logging. Each target
panel must appear among the 30 graded candidates. Report ids, ranks, grades and
plate/legend pages only, including the old PDF-228 Passarge probe's rank on v6.
Never print or log probe text, book paragraphs or eval questions.

Switch only after both the passage and panel gates pass. **Print this rollback
before switching:** set `INDEX_VERSION=v5` in `/srv/homelab/med-ask/.env`, then
`sudo systemctl restart med-ask.service`. Change only the index-version line;
never display the private file. On pass set `INDEX_VERSION=v6`, restart, check
that `med-ask.service` and `/api/health` are healthy, and verify a panel probe's
passing evidence shows its printed panel label and opens its plate page. If
any gate fails, leave the reader on v5 and report the measured failure.
