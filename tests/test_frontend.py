"""Check the source label used beside passages on screen with synthetic metadata."""

import subprocess
from pathlib import Path


def test_screen_source_label():
    module = Path("frontend/src/source-label.js").resolve().as_uri()
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            f"""import assert from 'node:assert/strict';
            import {{ sourceLabel }} from '{module}';
            const label = 'Synthetic book: pdf page 1';
            for (const kind of ['summary', 'glossary'])
              assert.equal(sourceLabel({{label, kind}}), `${{label}} · ${{kind}}`);
            assert.equal(sourceLabel({{label, kind: 'content'}}), label);
            assert.equal(sourceLabel({{label}}), label);
            """,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
