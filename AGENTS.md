# Repository rules

This repository describes what exists. Work arrives in self-contained task prompts;
do not look for plans or roadmaps elsewhere.

Keep code only, ever: no PDFs, page text, page renders, figures or images, indexes,
embeddings, evaluation sets, user feedback or app state, including test fixtures.
CI enforces this rule. Tests needing a PDF must generate a synthetic one at runtime.
The books live outside this repository.

Never put secrets in git or images. Published ports bind only to loopback.

Functions in the RAG core (ingestion, page and figure extraction, chunking,
retrieval, and building the answer prompt) open with their steps in plain language
as a numbered list in the docstring. Write for a reader who knows Python but not
the libraries, and keep those steps true when changing code. Plumbing (Flask
routes, Compose, CI, configuration) and React follow ordinary conventions.

Models are reached only through an OpenAI-compatible endpoint with base URL and
key from the environment, using a purpose (`chat` or `embed`) in the `model` field.
Never name a model provider or model in this repository.

Libraries own machinery: LlamaIndex owns ingestion, the index and retrieval.
Do not hand-roll a retriever, queue or graph runtime.

Always label generated text as generated and keep it visibly apart from source
text.

Before every commit run `uv run pytest`, `uv run ruff check .`, and the front-end
build when a front end exists. Also check formatting and the no-data-files guard.

Proceed without asking except before a step that cannot be undone or carries a
real security risk: deleting data or volumes, touching a secret, making something
reachable from outside loopback, rewriting main's history or loosening its
protection, or a security concern the task prompt does not address. For those,
stop, explain the risk and how it would be recovered, and wait for the owner.
