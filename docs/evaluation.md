# Retrieval evaluation

This document covers the private eval set, how a run is scored, the comparison and figure
gates a new index must pass, and the two real-case modes over labelled questions. Read it
before changing `src/med_ask/evaluation.py`, before scoring a new index version, and before
switching the served index. Labelling real questions is in [labelling.md](labelling.md).

## The eval set

The gate set measures how often search returns the pages that hold the evidence. It is private
and lives outside git at `/data/originals/eval/med-ask-eval.v2.jsonl` (`EVAL_FILE` overrides
it); results go to `runs/` beside it (`EVAL_RUNS_DIR` overrides). Never copy it, print its
questions, or put any part of it in tests or the repository. The manifest's `eval_book` maps a
book to its eval id; evidence in an eval book that no manifest entry names counts as not
indexed.

## A run

```sh
docker compose --profile ingest run --rm ingest eval
docker compose --profile ingest run --rm -e INDEX_VERSION=<version> ingest eval --purpose embed-large --no-roles
```

A run uses the app's own search on the current `INDEX_VERSION`, requesting
`PASSAGE_CANDIDATES` passages. `--purpose` defaults to `EMBEDDING_PURPOSE`; `--roles` and
`--no-roles` override `EMBEDDING_ROLES`. Only records with status `confirmed` are scored, and
`--status` (repeatable) adds others for diagnostics. The run never grades, generates or logs a
question. It writes `run-<UTC time>-<purpose>-roles-<on|off>-<suffix>.json`, never overwriting,
holding ids, scores, ranks, page numbers and timings only. A result without `roles` means roles
off.

Scoring:

- Figures are removed before ranks are assigned, so a run ranks passages only
  (`ranking_scope: passages`).
- A passage hits when any page of its PDF range is a confirmed page of that eval book; there is
  no ±1 tolerance.
- Only `confirmed` evidence counts. Pages with `text: "missing"`, other evidence states and
  books not indexed are left out; an answerable record with nothing scoreable is skipped.
- The gate measure is Hit@5 on answerable questions. Hit@10, Hit at the candidate cut, work
  coverage@10 (two editions of one work count once) and Hit@5 by language, type, subject and
  book are diagnostics.
- Embedding time is the median and slowest of the first 20 questions, after one warm-up
  request with fixed text.
- Not-found questions stay out of the gate; their top similarity is reported beside the
  answerable questions'.
- Malformed records are reported by id and skipped; retired records are not run.

## Comparison gate

```sh
docker compose --profile ingest run --rm ingest eval --compare <baseline.json> <candidate.json>
```

Bare file names are looked up in the runs directory. The candidate passes only if total Hit@5
does not drop and any question lost is balanced by one gained. The command names lost and
gained ids and exits 0 on pass, 1 on fail and 2 when it refuses, for example for runs on
different `set_version`s.

## Figure gate

A version that adds or changes figures must also keep passage recall perfect at 10:

```sh
docker compose --profile ingest run --name med-ask-<version>-gate ingest eval --figure-gate <baseline-run.json> <candidate-run.json> --cohort /data/originals/eval/fig-work/cohort.json
```

It accepts only v4/v5 or v5/v6 pairs, eval set 2, `embed-large` and roles off. Both runs must
score the same 32 ids, and the cohort file's 20 `cohort_ids` must be among them; select the
cohort by expected books, never by outcomes. The baseline must reach Hit@10 32/32 and 20/20 on
the cohort, and the candidate passes only by reaching both too. It reports Hit@5 and Hit@10
overall and for the cohort. Also check a few figure probes written from real printed captions:
run search without question logging and report each target's rank, never its text.

## Real-case modes

Both modes read the labels at `/data/originals/eval/med-ask-eval-real.v1.jsonl`
(`REAL_EVAL_FILE` overrides) and report ids and numbers only, separately for in-scope and
out-of-sources questions. With no labels they exit cleanly.

```sh
docker compose --profile ingest run --rm ingest eval --served
docker compose --profile ingest run --rm ingest eval --end-to-end
```

`--served` makes no model calls. It recovers each logged candidate's book and PDF range by node
id from the question's recorded `vector_table`, read-only, in logged order. A question logged
before grading is reported as unscorable (`logged before grading`) and left out of the
measures; empty grades never mean "nothing passed". Candidates whose pages cannot be recovered
are counted as `unresolved`, so a miss with unresolved candidates is incomplete evidence.

`--end-to-end` repeats today's search and grading through the endpoint, without generating
answers, logging questions or changing labels. Run it when the endpoint is serving.

For in-scope questions, `returned` means a candidate's PDF range meets an expected page,
`passed` that such a candidate was graded yes, and `Hit@10` and `Hit@<cut>` that one appeared
within those ranks (served mode counts all logged candidates). For out-of-sources questions,
`correct_not_found` means nothing was graded yes and `wrongly_passed` lists what was.
`ungraded` counts candidates with an unknown grade, which is not a no. Each result names its
table, so results from different served versions stay apart.

To score plain retrieval on the in-scope labels instead of the gate set, run `eval` with
`-e EVAL_FILE=/data/originals/eval/med-ask-eval-real.v1.jsonl`.
