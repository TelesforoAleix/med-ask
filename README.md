# med-ask

med-ask is an evidence search across one medical student's textbooks, returning
original passages with their book and page, referenced figures, and a short
generated explanation visibly separated from the authors' words. This repository
currently serves an empty search page and a database health route; it holds no
books, extracted content, indexes, evaluation data or application state.

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
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8100/
curl --fail http://127.0.0.1:8100/api/health
docker compose down
```

Open `http://127.0.0.1:8100/` on that host. Flask serves the compiled React app
and `/api/` from one container and origin, using Gunicorn as the WSGI server.
Search is not connected yet. `/api/health` returns
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

## Running on the server

The server uses an encrypted volume mounted at `/srv/homelab`, unlocked by hand
after each boot. Unlocking starts `homelab-data.target`, which pulls in
`med-ask.service`. The unit follows that target and orders itself after Docker
and Tailscale. It does not start the target or unlock the volume. Failures appear
in `systemctl status med-ask.service`; there is no failure notifier configured.

All med-ask files live on that volume except the installed systemd unit:

| Host path | Purpose | Container path |
| --- | --- | --- |
| `/srv/homelab/med-ask` | Root-owned clone of public `main`, including private `.env` | Image build context |
| `/srv/homelab/med-ask-data/sources` | Textbook PDFs, owned by root and readable by the app | `/data/sources`, read-only |
| `/srv/homelab/med-ask-data/originals` | Empty initially; future review resolutions, feedback and evaluation data that cannot be rebuilt | `/data/originals`, read-write |
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
