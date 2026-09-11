"""Benchmark a private, read-only-sourced library snapshot with the HTTP protocol.

The frozen snapshot/manifest stay local; result JSON contains query hashes only.
See docs/performance-baseline.md for preparation and repeat commands.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import bench_responsiveness as harness  # noqa: E402


def digest(path: Path) -> str:
    """Hash large files without loading them into memory."""
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare_snapshot(library: Path, snapshot: Path, *, export_source: str,
                     group: str, pivot: str, target: str, english_source: str) -> dict:
    """Back up the database read-only; freeze real queries without copying secrets."""
    snapshot.mkdir(parents=True, exist_ok=False)
    database = library / 'runtime/data/index.sqlite3'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(snapshot / 'index.sqlite3')) as destination:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
    with closing(sqlite3.connect((snapshot / 'index.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Snapshot integrity check failed')
        for source_id in (export_source, pivot, target, english_source):
            if not db.execute('SELECT 1 FROM source_files WHERE source_file_id=?', (source_id,)).fetchone():
                raise ValueError('Requested source is absent from snapshot')
        members = {row[0] for row in db.execute(
            'SELECT source_file_id FROM document_group_members WHERE document_group_id=?', (group,))}
        if not {pivot, target} <= members or pivot == target:
            raise ValueError('Alignment pair must be distinct members of the selected group')
        zh = db.execute('SELECT normalized_text FROM paragraphs WHERE source_file_id=? AND eligible_for_search=1 '
                        'AND length(normalized_text)>200 AND paragraph_index>10 ORDER BY paragraph_index LIMIT 1', (target,)).fetchone()
        en = db.execute('SELECT normalized_text FROM paragraphs WHERE source_file_id=? AND eligible_for_search=1 '
                        'AND length(normalized_text)>200 AND paragraph_index>10 ORDER BY paragraph_index LIMIT 1', (english_source,)).fetchone()
        if zh is None or en is None:
            raise ValueError('Selected sources need eligible body paragraphs longer than 200 characters after paragraph 10')
        zh, en = zh[0][:40], en[0][:80]
        queries = [
            {'id': 'zh_exact', 'query': zh, 'mode': 'exact'},
            {'id': 'script_variant', 'query': '社會', 'mode': 'exact'},
            {'id': 'en_exact', 'query': en, 'mode': 'exact'},
            {'id': 'common_zh', 'query': '社会', 'mode': 'auto'},
            {'id': 'common_en', 'query': 'gender', 'mode': 'auto'},
            {'id': 'normalized', 'query': zh[:10] + '，' + zh[10:], 'mode': 'auto'},
            {'id': 'no_hit', 'query': 'MEFinderBaselineNoHit7f83b92c60a4', 'mode': 'exact'},
            {'id': 'scoped', 'query': '社会', 'mode': 'auto', 'source_type': 'pdf'},
        ]
        for query in queries:
            query.update(limit=10, source_type=query.get('source_type', 'all'))
        manifest = {'fixture_version': 'real-library-1', 'queries': queries,
                    'export_source_id': export_source,
                    'alignment_request': {'document_group_id': group, 'pivot_source_file_id': pivot,
                                          'target_source_file_id': target, 'force': True},
                    'schema': db.execute('PRAGMA user_version').fetchone()[0],
                    'counts': {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                               for table in ('source_files', 'paragraphs', 'pdf_pages')},
                    'workload_paragraphs': {name: db.execute('SELECT count(*) FROM paragraphs WHERE source_file_id=?', (sid,)).fetchone()[0]
                                            for name, sid in (('export', export_source), ('pivot', pivot), ('target', target))}}
    manifest['content_sha256'] = digest(snapshot / 'index.sqlite3')
    manifest['database_bytes'] = (snapshot / 'index.sqlite3').stat().st_size
    (snapshot / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


def main() -> None:
    """Prepare once, then measure fresh processes against an unchanged snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--source-library', type=Path)
    for name in ('export-source', 'group', 'pivot', 'target', 'english-source'):
        parser.add_argument('--' + name)
    parser.add_argument('--models', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--compare', type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--scenario', choices=harness.SCENARIOS, action='append')
    args = parser.parse_args()
    snapshot = args.snapshot.resolve()
    if args.source_library:
        if not all((args.export_source, args.group, args.pivot, args.target, args.english_source)):
            parser.error('Preparation requires export-source/group/pivot/target/english-source')
        prepare_snapshot(args.source_library, snapshot, export_source=args.export_source,
                         group=args.group, pivot=args.pivot, target=args.target, english_source=args.english_source)
        print('Private snapshot prepared; original library unchanged.', flush=True)
        if not args.output:
            return
    if not args.output or args.output.exists() or args.rounds < 1 or args.repeats < 1:
        parser.error('A new output filename and positive rounds/repeats are required')
    fixture = json.loads((snapshot / 'manifest.json').read_text())
    if digest(snapshot / 'index.sqlite3') != fixture['content_sha256']:
        raise ValueError('Frozen snapshot changed; prepare a new baseline')
    from src.me_finder.embedding_models import EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL_ID
    from src.me_finder.preferences import save_preferences
    import psutil
    model = EMBEDDING_MODELS[DEFAULT_EMBEDDING_MODEL_ID]
    scenarios = list(dict.fromkeys(args.scenario or harness.SCENARIOS))
    model_source = args.models / model.fastembed_cache_dirname if args.models else None
    model_files = {}
    if 'alignment' in scenarios:
        if model_source is None or not model_source.is_dir():
            parser.error('Alignment requires an existing --models cache')
        model_files = {str(path.relative_to(model_source)): digest(path)
                       for path in sorted(model_source.rglob('*')) if path.is_file()}
    configuration = {'rounds': args.rounds, 'repeats': args.repeats, 'scenarios': scenarios,
                     'script_folding': True, 'model_id': model.id, 'rss_interval_ms': 50,
                     'request_interval_ms': 100,
                     'manifest_sha256': digest(snapshot / 'manifest.json'),
                     'cache': 'fresh runtime/vector cache; copied stored alignment history; force alignment; warm search',
                     'load': 'one sequential search client; continuous single job; minimum repeats and first job completion'}
    environment = {'platform': platform.platform(), 'machine': platform.machine(),
                   'logical_cpus': os.cpu_count(), 'physical_memory_bytes': psutil.virtual_memory().total,
                   'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                   'packages': {name: importlib.metadata.version(name) for name in
                                ('psutil', 'numpy', 'fastembed', 'onnxruntime', 'opencc-python-reimplemented')}}
    if sys.platform == 'darwin':
        environment['cpu_model'] = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip()
    runs = []
    with tempfile.TemporaryDirectory(prefix='mefinder-real-perf-') as temporary:
        for round_id in range(args.rounds):
            for scenario in scenarios[round_id % len(scenarios):] + scenarios[:round_id % len(scenarios)]:
                root = Path(temporary) / f'{round_id}-{scenario}'
                (root / 'data').mkdir(parents=True)
                shutil.copy2(snapshot / 'index.sqlite3', root / 'data/index.sqlite3')
                save_preferences({'script_folding': True, 'alignment_embedding_model_id': model.id}, root / 'config/preferences.json')
                if scenario == 'alignment':
                    shutil.copytree(model_source, root / 'components/text-alignment/models' / model.fastembed_cache_dirname)
                print(f'round {round_id + 1}/{args.rounds}: {scenario}', flush=True)
                try:
                    runs.append(harness.run_round(root, fixture, scenario, args.repeats, round_id))
                except (OSError, RuntimeError, TimeoutError, ValueError):
                    if (root / 'server.log').exists():
                        shutil.copy2(root / 'server.log', snapshot / 'failed-server.log')
                    raise
                print(json.dumps(harness.summarize([runs[-1]])), flush=True)
                shutil.rmtree(root)
    summary = harness.summarize(runs)
    query_ids = {query['id'] for query in fixture['queries']}
    valid = all(not metrics['identity_mismatches'] for metrics in summary.values()) and all(
        run['warmup_identities'] == runs[0]['warmup_identities'] and
        {sample['query_id'] for sample in run['samples'] if run['scenario'] == 'normal' or sample['overlap']} == query_ids
        for run in runs)
    public_fixture = {key: fixture[key] for key in ('fixture_version', 'content_sha256', 'database_bytes', 'schema', 'counts', 'workload_paragraphs')}
    public_fixture['queries'] = [{**{key: value for key, value in query.items() if key != 'query'},
                                  'query_sha256': hashlib.sha256(query['query'].encode()).hexdigest()} for query in fixture['queries']]
    result = {'protocol_version': 'real-library-1',
              'harness_sha256': hashlib.sha256(Path(__file__).read_bytes() + Path(harness.__file__).read_bytes()).hexdigest(),
              'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
              'tracked_dirty': bool(subprocess.check_output(['git', 'diff', 'HEAD', '--name-only'], cwd=REPO)),
              'source_tree_sha256': hashlib.sha256(b''.join(path.read_bytes() for path in sorted((REPO / 'src').rglob('*.py')))).hexdigest(),
              'measured_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'configuration': configuration,
              'environment': environment, 'model_files': model_files, 'fixture': public_fixture,
              'summary': summary, 'rounds': runs, 'valid': valid}
    if args.compare:
        result['comparison'] = harness.compare_results(json.loads(args.compare.read_text()), result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(f'wrote {args.output}; valid={valid}', flush=True)
    if not valid:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
