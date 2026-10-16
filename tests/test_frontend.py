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
            import {{ sourceLabel, reviewLocation }} from '{module}';
            const label = 'Synthetic book: pdf page 1';
            for (const kind of ['summary', 'glossary'])
              assert.equal(sourceLabel({{label, kind}}), `${{label}} · ${{kind}}`);
            assert.equal(sourceLabel({{label, kind: 'content'}}), label);
            assert.equal(sourceLabel({{label}}), label);
            const plate = {{book_id: 'passarge', label: 'Caption: pdf page 31',
              pdf_pages: [31,31], figure_pdf_page: 32, figure_printed_page: '21'}};
            assert.deepEqual(reviewLocation(plate), {{pdf_pages: [32,32],
              label: 'passarge: pdf page 32 <print page: 21>'}});
            assert.equal(sourceLabel(plate), plate.label);
            assert.deepEqual(reviewLocation({{label, pdf_pages: [1,2]}}),
              {{label, pdf_pages: [1,2]}});
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


def test_figure_heading_never_renders_description_or_discovery_note():
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
          pdf_pages: [31,31], figure_pdf_page: 32,
          description: 'Hidden generated synthetic spindle.'};
        const render = value => renderToStaticMarkup(
          React.createElement(component.EvidenceHeading, {item: value}));
        const html = render(item);
        assert.ok(html.includes('Figura 3-12 [2]'));
        assert.ok(html.includes('Printed caption'));
        assert.ok(html.includes('Plate: PDF page 32'));
        assert.ok(html.includes('Caption cited from PDF page 31'));
        assert.ok(!html.includes('Found by its description'));
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


def run_frontend_script(script, output):
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
        cwd="frontend",
        check=True,
        capture_output=True,
    )
    subprocess.run(["node", str(output)], check=True, capture_output=True)


def test_book_navigation_logic_and_contents_cache(tmp_path):
    run_frontend_script(
        """
        import assert from 'node:assert/strict';
        import { linkSegments, pageBounds, stepPage, currentContentsEntry,
          bookContents, navigationJSON } from './src/book-navigation.js';
        const text = '😀Fig AFig B.';
        const a = {start: 1, end: 6, target_pdf_page: 4, kind: 'figure'};
        const b = {start: 6, end: 11, target_pdf_page: 7, kind: 'figure'};
        assert.deepEqual(linkSegments(text, [b, a]), [
          {text: '😀'}, {text: 'Fig A', link: a},
          {text: 'Fig B', link: b}, {text: '.'}]);
        const overlap = {start: 3, end: 8};
        const duplicate = {...a};
        const invalid = [{start: -1, end: 2}, {start: 0, end: 99},
          {start: 2, end: 2}, {start: 1.5, end: 3}];
        const segments = linkSegments(text, [overlap, b, a, duplicate, ...invalid]);
        assert.equal(segments.map(s => s.text).join(''), text);
        assert.deepEqual(segments.filter(s => s.link).map(s => s.link), [a,b]);
        const edge = {start: 0, end: 12};
        assert.deepEqual(linkSegments(text, [edge]), [{text, link: edge}]);
        assert.deepEqual(linkSegments(text), [{text}]);
        assert.deepEqual(linkSegments(''), []);
        assert.deepEqual(linkSegments('ab', [{start:0,end:1},{start:1,end:2}])
          .map(s => s.text), ['a','b']);
        for (const [nav, expected] of [[null,[30,31]], [{page_count:900},[1,900]]]) {
          const bounds = pageBounds([30,31], nav);
          assert.deepEqual(bounds, expected);
          assert.equal(stepPage(bounds[0], -1, bounds), bounds[0]);
          assert.equal(stepPage(bounds[1], 1, bounds), bounds[1]);
        }
        assert.equal(stepPage(31, 1, pageBounds([30,31], {page_count:900})), 32);
        assert.equal(stepPage(30, -1, pageBounds([30,31], {page_count:900})), 29);
        assert.equal(stepPage(1, 1, [1,1]), 1);
        const child = {title:'Section', pdf_page:3, children:[]};
        const figure = {title:'Figure A', pdf_page:3, children:[]};
        const chapter = {title:'Chapter', pdf_page:2, children:[child,figure]};
        const other = {title:'Chapter', pdf_page:9, children:[]};
        const contents = [chapter,other];
        assert.equal(currentContentsEntry(contents, [chapter,child]), child);
        assert.equal(currentContentsEntry(contents, [chapter]), chapter);
        assert.equal(currentContentsEntry(contents, [other]), other);
        assert.equal(currentContentsEntry(contents, []), null);
        assert.equal(currentContentsEntry([], [chapter]), null);
        assert.equal(currentContentsEntry(contents,
          [{title:'Unknown',pdf_page:2}]), null);
        assert.equal(currentContentsEntry(contents,
          [chapter,{title:'Unknown',pdf_page:3}]), chapter);
        (async () => {
          let calls = 0;
          globalThis.fetch = async path => { calls++; return {ok:true,
            json: async () => ({contents:[]})}; };
          const first = bookContents('synthetic/book');
          assert.equal(bookContents('synthetic/book'), first);
          await Promise.all([first, bookContents('synthetic/book')]);
          assert.equal(calls, 1);
          await bookContents('other');
          assert.equal(calls, 2);
          globalThis.fetch = async () => ({status:404});
          assert.equal(await navigationJSON('/missing'), null);
          assert.equal(await bookContents('missing'), null);
          globalThis.fetch = async () => { throw new Error('network'); };
          assert.equal(await bookContents('missing'), null);
          await assert.rejects(navigationJSON('/failed'));
          globalThis.fetch = async () => ({status:503,ok:false});
          await assert.rejects(navigationJSON('/failed'));
        })().catch(error => { console.error(error); process.exitCode = 1; });
        """,
        tmp_path / "navigation.cjs",
    )


def test_book_viewer_and_reference_rendering(tmp_path):
    run_frontend_script(
        r"""
        import React from 'react';
        import { renderToStaticMarkup as render } from 'react-dom/server';
        import assert from 'node:assert/strict';
        import { ViewerPage, BookViewer } from './src/book-viewer.jsx';
        import { LinkedPassage, QuickLookPage } from './src/linked-passage.jsx';
        const noop = () => {};
        const child = {title:'Synthetic section',pdf_page:30,
          printed_page:'xx',children:[]};
        const root = {title:'Synthetic chapter',pdf_page:10,
          printed_page:null,children:[child]};
        const page = {book_id:'synthetic/book', title:'Synthetic book',
          label:'Synthetic source', pdf_pages:[30,31], number:30};
        const navigation = {pdf_page:30,printed_page:'xx',page_count:900,
          section_path:[root,child]};
        const props = {page,number:30,navigation,contents:[root],jump:noop};
        const html = render(<ViewerPage {...props} />);
        for (const text of ['PDF page 30', 'Printed page xx', 'Book section',
          'Contents', 'aria-current="location"', '/api/page/synthetic%2Fbook/30'])
          assert.ok(html.includes(text), text);
        assert.ok(!html.includes('disabled=""'));
        assert.ok(!html.includes('<input'));
        assert.ok(!html.includes('class="book-contents" open'));
        assert.equal((html.match(/aria-current="location"/g)||[]).length, 1);
        const fallback = render(<ViewerPage {...props} navigation={null} />);
        assert.ok(!fallback.includes('Contents'));
        assert.ok(!fallback.includes('Book section'));
        assert.ok(!fallback.includes('Printed page xx'));
        assert.equal((fallback.match(/disabled=""/g)||[]).length, 1);
        const start = render(<ViewerPage {...props} number={1} />);
        assert.equal((start.match(/disabled=""/g)||[]).length, 1);
        const end = render(<ViewerPage {...props} number={900} />);
        assert.equal((end.match(/disabled=""/g)||[]).length, 1);
        const noPrinted = render(<ViewerPage {...props}
          navigation={{...navigation,printed_page:null}} />);
        assert.ok(noPrinted.includes('PDF page 30'));
        assert.ok(!noPrinted.includes('Printed page null'));
        const loading = render(<ViewerPage {...props}
          navigation={undefined} loading />);
        assert.equal((loading.match(/disabled=""/g)||[]).length, 2);
        const paging = render(<ViewerPage {...props} navigation={undefined}
          loading navigationAvailable />);
        assert.ok(paging.includes('class="book-contents"'));
        assert.ok(paging.includes('Synthetic section'));
        const failure = render(<ViewerPage {...props} navigation={undefined}
          navigationError retry={noop} />);
        assert.ok(failure.includes('Retry navigation'));
        const dialog = render(<BookViewer page={page}
          question="Synthetic question" close={noop} />);
        assert.ok(dialog.includes('role="dialog"'));
        assert.ok(dialog.includes('aria-modal="true"'));
        const text = 'See Fig A.\nThen continue.';
        const link = {start:4,end:9,kind:'figure',target_pdf_page:90,
          label:'unused label'};
        const item = {...page,text,links:[link],
          description:'Hidden generated description'};
        const linked = render(<LinkedPassage item={item} openBook={noop} />);
        assert.ok(linked.includes('See <button'));
        assert.ok(linked.includes('>Fig A</button>.\nThen continue.'));
        const plain = render(<LinkedPassage item={{...item,links:[]}}
          openBook={noop} />);
        assert.ok(plain.includes(text));
        assert.ok(!plain.includes('<button'));
        const quick = render(<QuickLookPage item={item} link={link} text="Fig A"
          navigation={navigation} close={noop} openBook={noop} />);
        for (const value of ['Fig A', '(figure)', 'Synthetic section',
          '/api/page/synthetic%2Fbook/90', 'Open in the book', 'Close quick look'])
          assert.ok(quick.includes(value), value);
        assert.ok(!quick.includes(item.description));
        assert.ok(!quick.includes('class="generated'));
        """,
        tmp_path / "viewer.cjs",
    )


def test_label_question_and_viewer_action(tmp_path):
    run_frontend_script(
        """
        import React from 'react';
        import { renderToStaticMarkup as render } from 'react-dom/server';
        import assert from 'node:assert/strict';
        import { LabelQuestion } from './src/label.jsx';
        import { BookViewer } from './src/book-viewer.jsx';
        const noop = () => {};
        const page = {book_id:'bio', title:'Synthetic book', pdf_pages:[2,2],
          number:2, label:'PDF 2'};
        const detail = {question:'Synthetic question?', asked_at:'2026-10-16',
          language:'en', label:'in scope', answer:'Synthetic generated answer.',
          expected:[{book_id:'bio', pdf_page:2}], unresolved:1,
          suggestions:[{...page,pdf_page:2,passed:true}]};
        const html = render(<LabelQuestion {...{detail}} busy={false}
          act={noop} open={noop} />);
        for (const text of ['Out of the sources','Clear','Remove','Expected pages',
          'Suggested pages','Passed grading','App answer — generated, as logged',
          '1 logged candidates have unresolved pages.','Synthetic question?'])
          assert.ok(html.includes(text), text);
        assert.ok(html.includes('class="generated"'));
        const plain = render(<BookViewer {...{page}} close={noop} />);
        assert.ok(!plain.includes('This page answers'));
        const labelling = render(<BookViewer {...{page}} close={noop}
          onAnswerPage={noop} />);
        assert.ok(labelling.includes('This page answers'));
        """,
        tmp_path / "labels.cjs",
    )
