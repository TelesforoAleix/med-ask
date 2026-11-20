# Operations

This document covers running med-ask on its server: install, deploy, health, the public
tunnel, rollback, recovery and ingestion runs. Read it before any step on the server. Index
rebuilds, page reading, evaluation, navigation and the private screens have their own
documents in the [AGENTS.md document map](../AGENTS.md#document-map). Local checks are in
[AGENTS.md](../AGENTS.md#ci-commands).

## Compose

Postgres publishes no port and, unless `POSTGRES_DATA_PATH` is set, stores its data in the
`postgres-data` named volume. Its initialization script only creates the vector extension, and
only on a new volume. `docker compose down` preserves volumes. CI's Compose job builds, starts,
checks the page, health and the private/public split, then removes its throwaway volume.

Outside Compose, the app reads `DATABASE_URL` and optionally `FRONTEND_DIST` from the
environment. Private routes are on only when `PRIVATE_ROUTES_ENABLED` is exactly `true`.

## Install

The server keeps med-ask on the encrypted volume mounted at `/srv/homelab`, unlocked by hand
after each boot. Only the installed systemd units live elsewhere.

| Host path | Holds | Container path |
| --- | --- | --- |
| `/srv/homelab/med-ask` | Root-owned clone of public `main`, with the private `.env` | Build context |
| `/srv/homelab/med-ask-data/sources` | Textbook PDFs and `books.toml`, root-owned | `/data/sources`, read-only |
| `/srv/homelab/med-ask-data/originals` | Non-rebuildable records: readings, resolutions, exports, eval files | `/data/originals`, `app` and `ingest` only |
| `/srv/homelab/med-ask-data/navigation` | Rebuildable navigation files | `/data/navigation`, writable only in `ingest` |
| `/srv/homelab/med-ask-data/postgres` | Postgres data | `/var/lib/postgresql/data` |

### Prepare the directories

Prepare only while the volume is mounted. Stop if either deployment directory already exists;
never overwrite a deployment. The app runs as UID/GID 10001, so `originals` and `navigation`
are writable by that group:

```sh
mountpoint /srv/homelab
sudo install -d -o root -g root -m 0755 /srv/homelab/med-ask-data
sudo install -d -o root -g root -m 0755 /srv/homelab/med-ask-data/sources
sudo install -d -o root -g root -m 0750 /srv/homelab/med-ask-data/originals
sudo chown root:10001 /srv/homelab/med-ask-data/originals
sudo chmod 2770 /srv/homelab/med-ask-data/originals
sudo install -d -o root -g 10001 -m 2770 /srv/homelab/med-ask-data/navigation
sudo install -d -o root -g root -m 0700 /srv/homelab/med-ask-data/postgres
sudo git -c credential.helper= clone --branch main https://github.com/TelesforoAleix/med-ask /srv/homelab/med-ask
```

Postgres sets the ownership it needs inside its data directory on first start.

### Copy the books

Copy the PDFs from the Mac without putting them in the clone; substitute your directory and
SSH destination:

```sh
rsync -a --include='*.pdf' --exclude='*' --rsync-path='sudo rsync' <mac-sources-dir>/ <server>:/srv/homelab/med-ask-data/sources/
```

Then, on the server:

```sh
sudo chown -R root:root /srv/homelab/med-ask-data/sources
sudo find /srv/homelab/med-ask-data/sources -type d -exec chmod 0755 {} +
sudo find /srv/homelab/med-ask-data/sources -type f -exec chmod 0644 {} +
```

Verify there are 13 PDFs and compare their SHA-256 sums with the Mac originals.

### The book manifest

`books.toml` in the sources directory lists the books (root:root, mode 0644). Its format is
`src/med_ask/books.example.toml`, whose books are invented. Ids are unique lowercase URL-safe
identifiers; filenames are PDF basenames inside the sources directory, and paths escaping it
are refused. The optional `eval_book` names the book's id in the eval set. Requests never
choose a book by a filesystem path.

### Server environment

The ignored `.env` in the clone is owned by `root:root`, mode `0600`. `.env.example` documents
the variables; the server sets these:

| Variable | Unset or empty default | Server setting |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | Required; no default | Random password generated on the server |
| `APP_BIND_ADDRESS` | Loopback | The server's private Tailscale IPv4 address |
| `POSTGRES_DATA_PATH` | Named volume `postgres-data` | `/srv/homelab/med-ask-data/postgres` |
| `SOURCES_PATH` | Empty named volume `sources-empty` | `/srv/homelab/med-ask-data/sources` |
| `ORIGINALS_PATH` | Empty named volume `originals-empty` | `/srv/homelab/med-ask-data/originals` |
| `NAVIGATION_PATH` | Empty named volume `navigation-empty` | `/srv/homelab/med-ask-data/navigation` |
| `COMPOSE_PROFILES`, `TUNNEL_TOKEN` | Empty: no tunnel | See [Public tunnel](#public-tunnel) |
| `INDEX_VERSION`, `EMBEDDING_PURPOSE`, `EMBEDDING_ROLES` | `v2`, `embed`, `false` | The served index; see [index-versions.md](index-versions.md) |

Create the file without displaying the password or address; this refuses to overwrite an
existing `.env`:

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
    printf 'NAVIGATION_PATH=/srv/homelab/med-ask-data/navigation\n'
} > /srv/homelab/med-ask/.env
chown root:root /srv/homelab/med-ask/.env
chmod 0600 /srv/homelab/med-ask/.env
SH
```

Keep the address and password out of git, images, PRs and shared logs. Publish port 8100 only
on the Tailscale address, never the wildcard, LAN or loopback address. Change a setting by
editing only its line, without displaying the file, then restart `med-ask.service`.

### Install the service

`med-ask.service` is part of `homelab-data.target`, which unlocking the volume starts. It
orders itself after Docker, Tailscale and `homelab.service`, which creates the external
`homelab-models` network; it never unlocks the volume itself. After a deployment change is
merged into `main`:

```sh
sudo git -C /srv/homelab/med-ask pull --ff-only
sudo systemd-analyze verify /srv/homelab/med-ask/deploy/med-ask.service
sudo install -o root -g root -m 0644 /srv/homelab/med-ask/deploy/med-ask.service /etc/systemd/system/med-ask.service
sudo systemctl daemon-reload
sudo systemctl enable med-ask.service
sudo systemctl start med-ask.service
systemctl status med-ask.service
```

Failures appear in `systemctl status med-ask.service`; no failure notifier is configured.

### Question-log export

Install the daily export while the volume is mounted and `med-ask.service` is active; this does
not restart the app or build images:

```sh
sudo git -C /srv/homelab/med-ask pull --ff-only
sudo systemd-analyze verify /srv/homelab/med-ask/deploy/med-ask-export.service /srv/homelab/med-ask/deploy/med-ask-export.timer
sudo install -o root -g root -m 0644 /srv/homelab/med-ask/deploy/med-ask-export.service /srv/homelab/med-ask/deploy/med-ask-export.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now med-ask-export.timer
sudo systemctl start med-ask-export.service
systemctl list-timers med-ask-export.timer
sudo journalctl -u med-ask-export.service
sudo stat -c '%a %u:%g %s bytes' /srv/homelab/med-ask-data/originals/questions.jsonl
sudo wc -l /srv/homelab/med-ask-data/originals/questions.jsonl
sudo docker compose -f /srv/homelab/med-ask/compose.yaml exec -T postgres psql -U medask -d medask -Atc 'SELECT count(*) FROM medask_questions'
```

The timer fires daily at 03:00 server time and catches up after downtime. A locked volume or
an inactive app skips the run. The service uses the existing image with the clone's `src`
mounted read-only and selected by `PYTHONPATH`, so a pull changes export behaviour without a
build; it neither starts dependencies nor pulls images. Compare only row counts and byte sizes;
never print question rows. A hand run is `docker compose --profile ingest run --rm ingest
export-questions`.

The export writes a temporary file beside `questions.jsonl` and atomically replaces it. A run
with fewer rows than the previous file refuses, exits non-zero and leaves that file unchanged.
The file excludes the translation cache.

## Deploy

Each start of `med-ask.service` attempts an anonymous fast-forward pull of `main`, then builds
and starts Compose; a failed pull still builds the existing checkout. So deploying is:

```sh
sudo systemctl restart med-ask.service
```

Stopping the unit runs `docker compose down` and preserves data. Never restart or build while
an ingest container runs: they share the image (check with `docker ps --filter
label=com.docker.compose.service=ingest`).

## Health

From the Mac over Tailscale, `http://<server-tailscale-address>:8100/` shows the search page
and `/api/health` returns `{"postgres":"ok","vector":"ok"}`; anything else is HTTP 503. On the
server, `curl http://127.0.0.1:8100/` must fail and `sudo ss -ltn` must show port 8100 only on
the Tailscale address; never paste that output. Confirm Home Lab still answers with
`systemctl is-active homelab.service`.

## Public tunnel

`app` and `public` run the same image and both serve the search page and `/api/health`.
`public` turns private routes off, so every `/api/private/` path gives the unknown-route 404;
it has no originals mount and no host port. `cloudflared` joins only the tunnel network shared
with `public`, so it cannot reach `app` or Postgres, and it connects outward, so no listener
or firewall change is needed. It runs only under the `tunnel` Compose profile, so it is absent
in CI.

The owner creates a dashboard-managed tunnel named `med-ask` under Zero Trust → Networks →
Tunnels, copies the token from the displayed install command without running it, and adds a
published application route for their hostname with service type HTTP and URL `public:8100`.
Keep the hostname, tunnel ID and token out of git, PRs and logs.

Only the owner writes `TUNNEL_TOKEN` into the server `.env`, without displaying it, then adds
`COMPOSE_PROFILES=tunnel` and restarts `med-ask.service`. The connector receives the token from
its environment, never the command line. Cloudflare Access must protect the hostname: `public`
trusts the Access email header as the asker's identity (see [search.md](search.md#identity)).

To turn the tunnel off, remove `COMPOSE_PROFILES` and restart; `app` stays available over
Tailscale and no data changes. The owner may also remove `TUNNEL_TOKEN`.

## Rollback

- **A bad deploy:** each start pulls `main`, so revert the change on `main` through a PR and
  restart; do not pin the clone to an older commit.
- **A served index or embedding setting:** set the previous `INDEX_VERSION`,
  `EMBEDDING_PURPOSE` and `EMBEDDING_ROLES` lines and restart while no ingest runs; old tables
  are never removed ([index-versions.md](index-versions.md#switch-and-roll-back)).
- **Navigation:** delete only the `NAVIGATION_PATH` line and restart; the mount falls back to
  the empty volume and the files stay on the server.
- **The export schedule:** keep the exports and remove only the two units:

  ```sh
  sudo systemctl disable --now med-ask-export.timer
  sudo rm /etc/systemd/system/med-ask-export.timer /etc/systemd/system/med-ask-export.service
  sudo systemctl daemon-reload
  ```

- **The service unit**, preserving all data:

  ```sh
  sudo systemctl disable --now med-ask.service
  sudo rm /etc/systemd/system/med-ask.service
  sudo systemctl daemon-reload
  ```

## Recovery

After a reboot, the owner unlocks the volume (`sudo data-volume.sh unlock`), which starts
`homelab-data.target` and with it `med-ask.service`. Repeat the [health](#health) checks
without starting med-ask by hand. If binding or the boot order fails, stop and investigate
with the owner; do not change other units or networking.

Removing the deployment is permanent and needs the owner's authorization. First roll back the
service unit, then back up `originals`, any needed database data and the private `.env`. Only
after authorization, `sudo rm -rf -- /srv/homelab/med-ask /srv/homelab/med-ask-data` removes
med-ask's clone and data, leaving the volume and Home Lab untouched.

## Ingestion runs

Home Lab's model stack owns the `homelab-models` network, so start it before med-ask. Run one
embedding ingest at a time, by hand and detached, with ids from the manifest:

```sh
docker compose --profile ingest run -d --rm ingest ingest <book-id>
docker logs --tail 20 <container-id>
docker compose --profile ingest run --rm ingest status <book-id>
# With no ids, status reports every manifest book against every recorded table.
docker compose --profile ingest run --rm ingest status
```

A repeated ingest skips stored passages, and each passage is committed as it is stored, so an
interruption loses nothing committed. `status` counts current extracted ids against stored
ones for every recorded table, so a gap shows what a rerun will add; a repeated passage id is
stored once and appears as `duplicates`. Before any ingest, search answers "No index yet".
Ingesting on the shared CPU embedding slows searches in both projects; arrange long runs
overnight with the owner. New indexes are built as in [index-versions.md](index-versions.md).

### Long runs

Run every job longer than about a minute as a retained detached container with a name, without
`--rm`, so its log survives a failure. Do not poll it: hand off the name and `docker wait
<name>`, then read its exit status and final log once, and remove only that exited container.
Never remove data or volumes. Summaries go under `/data/originals/eval/<work-dir>/` or the
container log, never into git.

### Mac-served purposes

Before a long `vision` reading or `embed-large-bulk` build, read Home Lab's `docs/mac.md` for
when those purposes are available, and make sure the Mac is awake. When such a call fails or
answers that the purpose is not available, stop, report and resume later; never start, stop or
probe a Mac server. Readings, figure descriptions and ingests all resume by skipping work
already stored.
