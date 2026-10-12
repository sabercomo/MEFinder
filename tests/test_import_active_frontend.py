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
var timers = [], toasts = [], polls = [];
var MEFinderApi;
var global = {MEFinder: {works: {invalidate() {}, load() {}}}};
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
MEFinderApi = {async fetch() {throw new Error('网络暂时不可用');}};
var q = {id: 'one', jobId: 'job-one', status: 'processing', cancelRequested: true};
importStore.queue.push(q);
pollImportJob(q.id); await flush();
assert.equal(q.status, 'error');
assert.equal(q.message, '网络暂时不可用');
assert.deepEqual(cancellableImportQueue(), [q]);
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
