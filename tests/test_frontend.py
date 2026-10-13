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


def test_figure_heading_renders_discovery_note_without_description():
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
        import assert from 'node:assert/strict';
        import { build } from 'esbuild';
        const compiled = await build({
          entryPoints: ['src/evidence-heading.jsx'], bundle: true,
          write: false, platform: 'node', format: 'esm',
        });
        const component = await import('data:text/javascript;base64,' +
          Buffer.from(compiled.outputFiles[0].text).toString('base64'));
        const React = await import('react');
        const { renderToStaticMarkup } = await import('react-dom/server');
        const item = {kind: 'figure', figure_label: 'Figura 3-12',
          number: 2, language: 'Spanish', found_by_description: true,
          description: 'Hidden generated synthetic spindle.'};
        const render = value => renderToStaticMarkup(
          React.createElement(component.EvidenceHeading, {item: value}));
        const html = render(item);
        assert.ok(html.includes('Figura 3-12 [2]'));
        assert.ok(html.includes('Printed caption'));
        assert.ok(html.includes('Found by its description'));
        assert.ok(!html.includes(item.description));
        assert.ok(!render({...item, found_by_description: false})
          .includes('Found by its description'));
        assert.ok(render({...item, kind: 'content'}).includes('Passage [2]'));
        """,
        ],
        cwd="frontend",
        check=True,
        capture_output=True,
        text=True,
    )
