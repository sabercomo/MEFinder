"""Execute import controls to cover duplicate submissions and cancellation."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "src/me_finder/static/js/80-import.js").read_text(encoding="utf-8")


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class ImportActiveFrontendTests(unittest.TestCase):
    def run_js(self, signatures: list[str], scenario: str) -> None:
        blocks = []
        for signature in signatures:
            start = SOURCE.index(signature)
            following = re.search(r"\n  (?:async )?function ", SOURCE[start + len(signature):])
            end = start + len(signature) + following.start()
            blocks.append(SOURCE[start:end])
        script = """
const assert = require('node:assert/strict');
var importStore = {queue: []};
var timers = [], toasts = [], polls = [], refreshes = [];
var MEFinderApi;
var global = {MEFinder: {works: {invalidate() {}, load() {}},
  library: {async refreshAfterChange() { refreshes.push('refresh'); }}}};
var currentPage = 'import', calTransientStatus = {};
function renderImportQueue() {}
function showToast(message) { toasts.push(message); }
async function showAppConfirm() { return true; }
function setTimeout(callback) { timers.push(callback); }
function importStepsFor() { return ['读取文件', '类型检测', '本地 OCR', '建立索引']; }
function pdfTypeLabel() { return '扫描 PDF'; }
function invalidateLibraryCatalog() {}
async function ensureSearchDocuments() {}
function updateSearchDocumentLabel() {}
async function uploadImportFile() { return 'upload-one'; }
function pollImportJob(id) { polls.push(id); }
async function flush() { await new Promise(resolve => setImmediate(resolve)); }
""" + "\n".join(blocks) + "\n(async () => {\n" + scenario + "\n})().catch(error => {console.error(error); process.exitCode = 1;});"
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cancel_keeps_row_until_worker_has_stopped(self) -> None:
        self.run_js(["async function removeImport(", "function pollImportJob("], """
var state = 'cancelling';
MEFinderApi = {async fetch(url) {
  if (url === '/api/import-resume-dismiss') return {ok: true, async json() {return {state: 'cancelling'};}};
  return {ok: state !== 'gone', status: state === 'gone' ? 404 : 200,
    async json() {return state === 'gone' ? {error: '导入任务不存在。'} : {status: state, message: '正在停止后台解析…'};}};
}};
var q = {id: 'one', jobId: 'job-one', status: 'processing', type: 'pdf', route: 'local_ocr'};
importStore.queue.push(q);
pollImportJob(q.id); await flush();
assert.equal(await removeImport(q.id, {skipConfirm: true}), true);
assert.equal(importStore.queue.length, 1, 'cancelling worker must remain visible');
assert.equal(q.status, 'processing');
assert.match(q.message, /停止/);
await flush();
assert.equal(timers.length, 1, 'cancellation must continue polling');
state = 'gone'; timers.shift()(); await flush();
assert.equal(importStore.queue.length, 0, 'remove only after the worker disappears');
assert.equal(timers.length, 0);
""")

    def test_duplicate_upload_reuses_job_route_and_one_queue_row(self) -> None:
        self.run_js(["async function uploadImport("], """
MEFinderApi = {async fetch() {return {ok: true, async json() {return {
  job_id: 'job-existing', already_running: true, status: 'processing',
  parse_route: 'local_ocr', provider_id: 'local-ocr', detected_pdf_type: 'broken_text'
};}};}};
var old = {id: 'active-old', jobId: 'job-existing', status: 'processing'};
var q = {id: 'new', type: 'pdf', providerId: 'requested-mineru'};
importStore.queue.push(old, q);
await uploadImport(q.id);
assert.equal(importStore.queue.length, 1);
assert.equal(importStore.queue[0], q);
assert.equal(q.jobId, 'job-existing');
assert.equal(q.route, 'local_ocr');
assert.equal(q.providerId, 'local-ocr');
assert.equal(q.status, 'processing');
assert.match(q.message, /已有/);
assert.deepEqual(polls, ['new']);
""")

    def test_normal_missing_job_is_still_an_error(self) -> None:
        self.run_js(["function pollImportJob("], """
MEFinderApi = {async fetch() {return {ok: false, status: 404, async json() {return {error: '导入任务不存在。'};}};}};
var q = {id: 'one', jobId: 'missing', status: 'processing'};
importStore.queue.push(q);
pollImportJob(q.id); await flush();
assert.equal(importStore.queue.length, 1);
assert.equal(q.status, 'error');
assert.equal(q.message, '导入任务不存在。');
""")

    def test_polling_failure_during_cancellation_remains_retryable(self) -> None:
        self.run_js(["function cancellableImportQueue(", "function pollImportJob("], """
var disconnected = true;
MEFinderApi = {async fetch() {
  if (disconnected) throw new TypeError('Failed to fetch');
  return {status: 404, async json() {return {error: '导入任务不存在。'};}};
}};
var q = {id: 'one', jobId: 'job-one', status: 'processing', cancelRequested: true};
importStore.queue.push(q);
pollImportJob(q.id); await flush();
assert.equal(q.status, 'processing');
assert.match(q.message, /停止/);
assert.match(q.message, /重试/);
assert.equal(q.cancelRequested, true);
assert.deepEqual(cancellableImportQueue(), []);
assert.equal(timers.length, 1);
disconnected = false; timers.shift()(); await flush();
assert.equal(importStore.queue.length, 0);
assert.equal(timers.length, 0);
""")

    def test_disconnected_polling_preserves_progress_and_recovers_to_completion(self) -> None:
        self.run_js(["function pollImportJob("], """
var calls = 0;
MEFinderApi = {async fetch(url) {
  assert.equal(url, '/api/import-status?job_id=job-one');
  calls++;
  if (calls <= 2) throw new TypeError('Failed to fetch');
  return {status: 200, async json() {return {
    status: 'completed', message: '导入完成', source_file_id: 'source-one'
  };}};
}};
var q = {id: 'one', jobId: 'job-one', status: 'processing', step: 2, route: 'native',
  sourceFileId: 'source-one'};
importStore.queue.push(q);
calTransientStatus['source-one'] = 'importing';
pollImportJob(q.id); await flush();
for (var index = 0; index < 2; index++) {
  assert.equal(q.status, 'processing', 'query failure must not mark the import failed');
  assert.equal(q.step, 2);
  assert.equal(q.route, 'native');
  assert.equal(calTransientStatus['source-one'], 'importing');
  assert.match(q.message, /本地/);
  assert.match(q.message, /重试/);
  assert.equal(timers.length, 1, 'keep exactly one pending status query');
  timers.shift()(); await flush();
}
assert.equal(calls, 3);
assert.equal(q.status, 'done');
assert.equal(q.message, '导入完成');
assert.equal(calTransientStatus['source-one'], undefined);
assert.deepEqual(refreshes, ['refresh']);
assert.equal(timers.length, 0);
""")

    def test_interrupted_response_body_retries_and_reports_actual_import_failure(self) -> None:
        self.run_js(["function pollImportJob("], """
var calls = 0;
MEFinderApi = {async fetch() {return {status: 200, async json() {
  if (++calls === 1) throw new TypeError('Failed to fetch');
  return {status: 'failed', message: '索引写入失败', can_resume: true, failure_stage: 'index'};
}};}};
var q = {id: 'one', jobId: 'job-one', status: 'processing', step: 2};
importStore.queue.push(q);
pollImportJob(q.id); await flush();
assert.equal(q.status, 'processing');
assert.equal(timers.length, 1);
timers.shift()(); await flush();
assert.equal(q.status, 'error');
assert.equal(q.message, '索引写入失败');
assert.equal(q.canResume, true);
assert.equal(q.failureStage, 'index');
assert.deepEqual(refreshes, []);
assert.equal(timers.length, 0, 'actual import failure must stop polling');
""")

    def test_invalid_status_json_is_reported_without_retry(self) -> None:
        self.run_js(["function pollImportJob("], """
MEFinderApi = {async fetch() {return {status: 200, async json() {
  throw new SyntaxError('Unexpected token');
}};}};
var q = {id: 'one', jobId: 'job-one', status: 'processing'};
importStore.queue.push(q);
pollImportJob(q.id); await flush();
assert.equal(q.status, 'error');
assert.equal(q.message, 'Unexpected token');
assert.equal(timers.length, 0);
""")

    def test_removed_import_does_not_poll_again_after_disconnection(self) -> None:
        self.run_js(["function pollImportJob("], """
var calls = 0;
MEFinderApi = {async fetch() {calls++; throw new TypeError('Failed to fetch');}};
var q = {id: 'one', jobId: 'job-one', status: 'processing'};
importStore.queue.push(q);
pollImportJob(q.id); await flush();
assert.equal(timers.length, 1);
importStore.queue = [];
timers.shift()(); await flush();
assert.equal(calls, 1);
assert.equal(timers.length, 0);
""")

    def test_cancel_from_polling_error_restarts_polling_and_exposes_cleanup_failure(self) -> None:
        self.run_js(["async function removeImport(", "function pollImportJob("], """
MEFinderApi = {async fetch(url) {
  if (url === '/api/import-resume-dismiss') return {ok: true, async json() {return {state: 'cancelling'};}};
  return {ok: true, status: 200, async json() {return {status: 'failed', message: '取消清理失败'};}};
}};
var q = {id: 'one', jobId: 'job-one', status: 'error', type: 'pdf'};
importStore.queue.push(q);
assert.equal(await removeImport(q.id), true);
await flush();
assert.equal(importStore.queue.length, 1);
assert.equal(q.status, 'error');
assert.equal(q.cancelRequested, false);
assert.equal(q.message, '取消清理失败');
assert.equal(timers.length, 0);
""")
