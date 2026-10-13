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
            assert.equal(sourceLabel({{label, text_source: 'ocr'}}),
              `${{label}} · from OCR`);
            for (const metadata of [{{check_page: true}}, {{inherited_ocr: true}}])
              assert.equal(sourceLabel({{label, ...metadata}}),
                `${{label}} · from OCR — check the page`);
            """,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_resolve_page_shows_source_reasons_and_four_actions(tmp_path):
    frontend = Path("frontend").resolve()
    output = tmp_path / "resolve-check.cjs"
    script = """
      import React from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { ResolvePage } from './src/resolve.jsx';
      import assert from 'node:assert/strict';
      const noop = () => {};
      const page = { title: 'Synthetic book', book_id: 'synthetic', pdf_page: 2,
        text: 'Synthetic original source.', generated: true,
        reason_messages: ['The printed page number could not be read.'] };
      const candidate = { text: 'Synthetic hinted text.',
        attempt_id: 'synthetic-attempt',
        reason_messages: [] };
      const html = renderToStaticMarkup(<ResolvePage {...{
        page, candidate, busy: false, imageError: false, number: '', text: page.text,
        hint: '', setImageError: noop, act: noop, setNumber: noop, setText: noop,
        setHint: noop, rerun: noop, setCandidate: noop, setNote: noop
      }} />);
      for (const text of ['Synthetic original source.', 'Synthetic hinted text.',
        'The printed page number could not be read.', '/api/page/synthetic/2',
        'Confirm reading as correct', 'Save printed number', 'Save replacement text',
        'Re-read with hint', 'Accept hinted reading', 'Discard candidate',
        'Kept reading — generated OCR', 'Hinted reading — generated OCR'])
        assert.ok(html.includes(text), text);
      assert.equal((html.match(/class="generated"/g) || []).length, 2);
    """
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
          import { build } from 'esbuild';
          await build({ stdin: { contents: process.argv[1], resolveDir: process.cwd(),
            loader: 'jsx' }, bundle: true, platform: 'node', format: 'cjs',
            outfile: process.argv[2] });
        """,
            script,
            str(output),
        ],
        cwd=frontend,
        check=True,
        capture_output=True,
    )
    subprocess.run(["node", str(output)], check=True, capture_output=True)
