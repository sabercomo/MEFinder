"""在一致性副本上通过真实 HTTP/托管组件验证 Bertalign；从不写源库。

先用应用设置在 --runtime-root 安装 Bertalign。可在 macOS sandbox-exec 禁止
外网的环境运行本脚本，验证真实离线计算。所有结果写入 --output。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from src.me_finder.app_context import AppContext
from src.me_finder.alignment_regions import alignment_body_bounds
from src.me_finder.bertalign_runtime import bertalign_model_installed
from src.me_finder.web import ManagedThreadingHTTPServer, make_handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--pivot', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not bertalign_model_installed(args.runtime_root):
        parser.error('请先在独立验收运行目录安装 Bertalign 组件及模型')
    db = args.runtime_root / 'data' / ('bertalign-verification-' + uuid.uuid4().hex + '.sqlite3')
    db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(args.source_db.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        with sqlite3.connect(db) as destination:
            source.backup(destination)
    finally:
        source.close()
    handler = make_handler(db, app_context=AppContext.create(args.runtime_root, index_path=db))
    server = ManagedThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'

    def request(route, data=None):
        payload = None if data is None else json.dumps(data).encode('utf-8')
        req = urllib.request.Request(base + route, payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.load(response)

    try:
        _, original_preferences = request('/api/preferences')
        request('/api/preferences', {'alignment_backend': 'bertalign'})
        pair = {'document_group_id': args.group, 'pivot_source_file_id': args.pivot,
                'target_source_file_id': args.target}
        _, body = request('/api/text-alignments/body-range', pair)
        ranges, identities = {}, {}
        with sqlite3.connect(db) as connection:
            for side in body['sides']:
                texts = [row[0] for row in connection.execute(
                    'SELECT text_raw FROM text_segments WHERE segment_set_id=? ORDER BY order_index',
                    (side['segment_set_id'],))]
                ranges[side['side']] = list(alignment_body_bounds(texts))
                identities[side['side']] = side['segment_set_id']
        payload = {**pair, 'force': True, 'reviewed_body_ranges': ranges,
                   'expected_segment_set_ids': identities}
        started = time.monotonic()
        code, job = request('/api/text-alignments/start', payload)
        assert code == 202, job
        while True:
            code, result = request('/api/text-alignments/status?job_id=' + job['job_id'])
            if code != 202:
                break
            print(f'计算中：{time.monotonic() - started:.0f} 秒', flush=True)
            time.sleep(5)
        assert code == 200 and result.get('ok'), result
        _, reused = request('/api/text-alignments/generate', {**payload, 'force': False})
        assert reused['result']['reused'] is True, reused
        _, overview = request('/api/translation-works/overview?include_statistics=0&source_id=' + args.pivot)
        _, targets = request('/api/text-alignments/targets?source_id=' + args.pivot)
        report = {'seconds': time.monotonic() - started, 'database_copy': str(db),
                  'request': payload, 'result': result, 'reused_result': reused,
                  'overview': overview, 'targets': targets}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        if 'original_preferences' in locals():
            request('/api/preferences', {'alignment_backend': original_preferences['alignment_backend']})
        handler.begin_shutdown()
        server.shutdown()
        thread.join()
        server.server_close()
        handler.wait_for_durable_operations()
        handler.close_runtime()
    print(args.output)


if __name__ == '__main__':
    main()
