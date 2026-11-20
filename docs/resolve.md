# Resolving queued pages

This document covers the private **Resolve issues** screen: its cache, its routes, where
resolutions are kept and when they take effect. Read it before changing
`src/med_ask/resolutions.py` or `resolve_cache.py`, and before the first use of the screen
after a deployment or a change to PDFs or kept readings.

## Who can reach it

Only `app`, with `PRIVATE_ROUTES_ENABLED=true`, registers the resolve routes and shows
**Resolve issues**. Reaching `app` over Tailscale is the lock; there are no logins or roles.
`public` answers every `/api/private/` path, for every method, with the unknown-route 404 and
has no originals mount. Browser writes must come from the same origin.

## Build the diagnostics cache first

Resolve requests never inspect a PDF; they read a source-side diagnostics cache. Build it once
after deployment, and again after a PDF or kept reading changes:

```sh
docker compose --profile ingest run -d --no-deps --name med-ask-resolve-cache ingest resolve-cache
docker wait med-ask-resolve-cache
docker logs med-ask-resolve-cache
```

Name book ids after `resolve-cache` to rebuild only those. The command uses no models,
embeddings or database writes and leaves kept readings unchanged. It writes
`/data/originals/resolve-cache/<book-id>/diagnostics.json` atomically; these files hold source
text and never enter git. A rebuild refuses to publish if its inputs changed meanwhile.

A missing or stale cache makes the queue and page requests answer 503 with the exact rebuild
command. Resolutions are read live and never make the cache stale, and appending a hinted
attempt advances only that reading's entry. Each worker reuses the parsed cache until the file
changes.

## Routes

- `GET /api/private/resolve/queue`, with optional `book` and `reason` filters, lists queued
  pages without a resolution.
- `GET /api/private/resolve/page/<book-id>/<pdf-page>` returns the kept reading, printed number
  and plain-language reasons; the screen shows it beside the `/api/page/...` image.
- `POST .../resolution` takes `action`: `confirm`, `number` with `printed_page`, `text` with
  replacement `text`, or `hinted` with an `attempt_id`.
- `POST .../hint` takes a `hint` and makes one `vision` request with it added to the
  transcription prompt, a 20-second timeout and no retry. If vision is not serving the screen
  says so, and the queue and other actions still work.

A hinted candidate is labelled generated and is accepted or discarded by the owner. It is
appended to the kept reading's history without replacing the selected reading; accepting it
writes a resolution.

## Resolutions

Resolutions live in `/data/originals/resolutions/<book-id>/<pdf-page>.json`, each with an
append-only `history` of timestamped decisions, text and printed numbers. They can contain
book text: never copy them into git. Writes are serialized per book across workers. Every
later extraction uses the latest resolution and clears the check-page marker, and the queue
omits the page. The index changes only at the next rebuild: saving performs no ingest,
embedding or database write.
