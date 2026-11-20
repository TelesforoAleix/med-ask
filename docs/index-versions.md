# Index versions

This document covers how vector tables are named and versioned, what each index version
contains, and the procedure to build a new version, copy unchanged books into it, switch the
app to it and roll back. Read it before changing the embedding purpose, roles, the table
rule or anything that changes passage ids, and before every rebuild. The gates a new version
must pass are in [evaluation.md](evaluation.md).

## Tables

Each index is one LlamaIndex table named `e_<version>_<model stem>_<model hash>`; the
store adds the prefix `data_`. The stem and hash come from the model the endpoint reports, so a
changed model lands in a new table on its own. `INDEX_VERSION` (default `v2`) must be `v`
followed by one to four digits. `medask_models` records each table's model and dimensions;
ingest refuses a table whose recorded model differs, and search refuses one that is missing
or differs ("No index yet" / "A new index is required").

`EMBEDDING_ROLES` is on only when exactly `true`. On, questions are sent with
`input_type: query` and passages with `input_type: passage` through the client's
`extra_body`; text is unchanged. Role tables add `r` after the version (`e_v2r_...`), except
for `embed-large` and `embed-large-bulk`, which always use the plain name.

A passage id is a hash of the book id, its PDF page range and its unchanged text, so changing
extraction changes ids, and a rebuild into a new version is how such a change is served.
Nothing deletes or overwrites a table; never drop, delete from or rename one.

## What each version contains

| Version | Index input |
| --- | --- |
| below `v5` | Passages only, of kinds content, summary, glossary, exercise and references |
| `v5` | Adds kind `figure`: printed labels and captions, with whole Passarge plates |
| `v6` and later | As v5, with Passarge plates split into lettered panels |

Search filters to content, summary and glossary in every version. Figure details are in
[figures.md](figures.md).

## Build a new version

1. Wait until no ingest container runs, then pull the clone and run `docker compose build`.
   The running `app` and `public` keep their image and index.
2. Before a long `embed-large-bulk` build, follow
   [Mac-served purposes](operations.md#mac-served-purposes). Probe `embed-large-bulk` and
   `embed-large` once each and stop unless both give the same `model_table` name under the new
   `INDEX_VERSION`: questions use `embed-large`, so the bulk purpose is for ingestion only.
3. Build every book in one retained detached container (see
   [Long runs](operations.md#long-runs)):

   ```sh
   docker compose --profile ingest run -d --name med-ask-<version>-build -e INDEX_VERSION=<version> -e EMBEDDING_PURPOSE=embed-large-bulk -e EMBEDDING_ROLES=false -e EMBEDDING_TIMEOUT=300 ingest ingest <book-id> ...
   docker wait med-ask-<version>-build
   docker logs med-ask-<version>-build
   ```

   `EMBEDDING_TIMEOUT` defaults to 20 seconds, which the app and eval keep.
4. Check `status` with `-e INDEX_VERSION=<version>`: every current unique id must be stored,
   and a gap must be accounted for by repeated ids. Record counts per book and kind and the
   elapsed rate.
5. Score the new version and apply its gate ([evaluation.md](evaluation.md)), then switch.

### Copy unchanged books

When only some books change between versions, copy the others' rows instead of embedding them
again. LlamaIndex creates the new table; one SQL `INSERT` copies the rows; the existing
bookkeeping methods register it. No embedding endpoint is needed. Save this on the server
under `/data/originals/eval/<work-dir>/copy.py`, editing the three names; never print rows or
credentials:

```python
import os
from psycopg import sql
from med_ask.database import Database
from med_ask.retrieval import managed_store, vector_store

books = ["<unchanged-book-id>", "..."]
old, new = "e_<old-version>_", "e_<new-version>_"
database = Database(os.environ["DATABASE_URL"])
models = [row for row in database.models() if row[0].startswith(old)]
assert len(models) == 1
source, model, dimensions = models[0]
target = source.replace(old, new, 1)
with managed_store(vector_store, database.url, target, dimensions, setup=True) as store:
    # The library owns table creation, column types, defaults and indexes.
    assert not store.get_nodes(node_ids=["table-initialization"])
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
assert len(counts) == len(books)
database.register(target, model, dimensions)
for book, count in counts:
    database.book_count(target, book, count)
print("Copied and registered unchanged books", flush=True)
```

Then run the copy and the changed books' ingest in one container:

```sh
docker compose --profile ingest run -d --name med-ask-<version>-build -e INDEX_VERSION=<version> -e EMBEDDING_PURPOSE=embed-large-bulk -e EMBEDDING_ROLES=false -e EMBEDDING_TIMEOUT=300 --entrypoint sh ingest -c 'python /data/originals/eval/<work-dir>/copy.py && python -m med_ask.ingest ingest <changed-book-id>'
```

If the old table's schema cannot take this plan, build every book instead and report that
choice.

## Switch and roll back

Compose passes `INDEX_VERSION`, `EMBEDDING_PURPOSE` and `EMBEDDING_ROLES` to `app`, `public`
and `ingest`, so search and eval follow the server `.env`. Merging code alone never changes the
served index.

1. Write the rollback in the handover before switching: set only the previous
   `INDEX_VERSION` line in `/srv/homelab/med-ask/.env`, without displaying the file, then
   `sudo systemctl restart med-ask.service`.
2. Switch only after the gate passes and while no ingest runs: set only the new
   `INDEX_VERSION` line and restart.
3. Check `med-ask.service`, `/api/health`, and a search probe whose evidence shows the change.

If any gate fails, leave the app on the current version and report the measured failure.
Changing an embedding purpose or roles follows the same steps with those two lines, and every
book must already be fully indexed in the target table.
