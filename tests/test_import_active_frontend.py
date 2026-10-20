"""Execute the complete import task owner, including pending requests and timers."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


JOBS = Path(__file__).resolve().parents[1] / 'src/me_finder/static/js/78-import-jobs.js'


@unittest.skipUnless(shutil.which('node'), 'Node.js is required')
class ImportActiveFrontendTests(unittest.TestCase):
    def run_js(self, scenario: str) -> None:
        script = r"""
const assert = require('node:assert/strict'), fs = require('node:fs');
global.window = global;
const timers = new Map(), toasts = [], confirmations = [], completed = [], settled = [];
let timerId = 0, renders = 0, provider = {id:'vision',name:'视觉 API'};
global.setTimeout = callback => {timers.set(++timerId,callback); return timerId;};
global.clearTimeout = id => timers.delete(id);
global.importStepsFor = () => ['读取文件','类型检测','本地 OCR','MinerU 解析','建立索引'];
global.pdfTypeLabel = () => '扫描 PDF';
const response = (data, status=200) => ({ok:status<400,status,json:async()=>data});
const flush = () => new Promise(resolve=>setImmediate(resolve));
async function tick() {
 const [id,callback] = timers.entries().next().value;
 timers.delete(id);callback();await flush();
}
eval.call(global,fs.readFileSync(process.argv[1],'utf8'));
const jobs = MEFinder.importJobs;
jobs.configure({changed:()=>{renders++;},toast:message=>toasts.push(message),
 askConfirm:async message=>{confirmations.push(message);return true;},retryProvider:()=>provider,
 openSettings:()=>{},completed:()=>completed.push(true),settled:id=>settled.push(id),parsing:()=>{}});
const item = () => jobs.snapshot()[0];
""" + "\n(async () => {\n" + scenario + "\n})().catch(error=>{console.error(error);process.exitCode=1;});"
        result = subprocess.run(
            [shutil.which('node'), '-e', script, str(JOBS)], capture_output=True,
            text=True, encoding='utf-8', timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repeated_watch_starts_only_one_request(self) -> None:
        self.run_js("""
let requests=0;
global.MEFinderApi={fetch(){requests++;return new Promise(()=>{});}};
jobs.add({id:'one',jobId:'job-one',status:'processing'});
jobs.pollJob('one');jobs.pollJob('one');
assert.equal(requests,1,'one task has one polling chain');
""")

    def test_old_poll_cannot_overwrite_replacement_job(self) -> None:
        self.run_js("""
let release;
global.MEFinderApi={async fetch(url){
 if(url==='/api/import-retry-mineru') return response({job_id:'new'});
 if(url.endsWith('old')) return new Promise(resolve=>{release=resolve;});
 return new Promise(()=>{});
}};
jobs.add({id:'one',jobId:'old',status:'error',type:'pdf'});
jobs.pollJob('one');
await jobs.retryWithMinerU('one');
release(response({status:'failed',message:'old failure'}));await flush();
assert.equal(item().jobId,'new');assert.equal(item().status,'processing');
assert.match(item().message,/切换到 MinerU/);
assert.equal(completed.length,0);assert.equal(timers.size,0);
""")

    def test_cancel_keeps_row_until_worker_has_stopped(self) -> None:
        self.run_js("""
let state='cancelling';
global.MEFinderApi={async fetch(url){
 if(url==='/api/import-resume-dismiss') return response({state:'cancelling'});
 return state==='gone'?response({error:'导入任务不存在。'},404):response({status:state});
}};
jobs.add({id:'one',jobId:'job-one',status:'processing',type:'pdf',route:'local_ocr'});
jobs.pollJob('one');await flush();
assert.equal(await jobs.remove('one',{skipConfirm:true}),true);await flush();
assert.equal(jobs.snapshot().length,1);assert.equal(item().status,'processing');
assert.match(item().message,/停止/);assert.equal(timers.size,1);
state='gone';await tick();
assert.equal(jobs.snapshot().length,0);assert.equal(timers.size,0);
""")

    def test_duplicate_upload_reuses_job_route_and_one_queue_row(self) -> None:
        self.run_js("""
const requests=[];
global.MEFinderApi={async fetch(url){requests.push(url);
 if(url==='/api/import-upload/start') return response({upload_id:'upload-one',chunk_size:2});
 if(url==='/api/import-upload/chunk') return response({received_size:2});
 if(url==='/api/import-upload/finish') return response({job_id:'job-existing',already_running:true,
 status:'processing',parse_route:'local_ocr',provider_id:'local-ocr',detected_pdf_type:'broken_text'});
 return new Promise(()=>{});
}};
const file=new Blob(['ab']);Object.defineProperty(file,'name',{value:'book.pdf'});
jobs.add({id:'active-old',jobId:'job-existing',status:'processing'});
jobs.pollJob('active-old');
jobs.add({id:'new',type:'pdf',providerId:'requested-mineru',file});
await jobs.upload('new');
assert.equal(jobs.snapshot().length,1);assert.equal(item().id,'new');
assert.equal(item().jobId,'job-existing');assert.equal(item().route,'local_ocr');
assert.equal(item().providerId,'local-ocr');assert.equal(item().status,'processing');
assert.match(item().message,/已有/);
assert.equal(requests.filter(url=>url==='/api/import-upload/chunk').length,1);
""")

    def test_normal_missing_job_is_still_an_error(self) -> None:
        self.run_js("""
global.MEFinderApi={async fetch(){return response({error:'导入任务不存在。'},404);}};
jobs.add({id:'one',jobId:'missing',status:'processing'});
jobs.pollJob('one');await flush();
assert.equal(jobs.snapshot().length,1);assert.equal(item().status,'error');
assert.equal(item().message,'导入任务不存在。');assert.equal(timers.size,0);
""")

    def test_non_network_polling_error_is_reported(self) -> None:
        self.run_js("""
global.MEFinderApi={async fetch(){throw Error('网络暂时不可用');}};
jobs.add({id:'one',jobId:'job-one',status:'processing',cancelRequested:true});
jobs.pollJob('one');await flush();
assert.equal(item().status,'error');assert.equal(item().message,'网络暂时不可用');
assert.equal(item().cancelRequested,true);assert.equal(timers.size,0);
""")

    def test_polling_failure_during_cancellation_remains_retryable(self) -> None:
        self.run_js("""
let disconnected=true;
global.MEFinderApi={async fetch(){
 if(disconnected) throw new TypeError('Failed to fetch');
 return response({error:'导入任务不存在。'},404);
}};
jobs.add({id:'one',jobId:'job-one',status:'processing',cancelRequested:true});
jobs.pollJob('one');await flush();
assert.equal(item().status,'processing');assert.equal(item().cancelRequested,true);
assert.match(item().message,/停止/);assert.match(item().message,/重试/);
assert.equal(timers.size,1);
disconnected=false;await tick();
assert.equal(jobs.snapshot().length,0);assert.equal(timers.size,0);
""")

    def test_disconnected_polling_preserves_progress_and_recovers_to_completion(self) -> None:
        self.run_js("""
let calls=0;
global.MEFinderApi={async fetch(url){
 assert.equal(url,'/api/import-status?job_id=job-one');
 if(++calls<=2) throw new TypeError('Failed to fetch');
 return response({status:'completed',message:'导入完成',source_file_id:'source-one'});
}};
jobs.add({id:'one',jobId:'job-one',status:'processing',step:2,route:'native',sourceFileId:'source-one'});
jobs.pollJob('one');await flush();
for(let index=0;index<2;index++){
 assert.equal(item().status,'processing');assert.equal(item().step,2);
 assert.equal(item().route,'native');assert.equal(settled.length,0);
 assert.match(item().message,/本地/);assert.match(item().message,/重试/);
 assert.equal(timers.size,1);await tick();
}
assert.equal(calls,3);assert.equal(item().status,'done');assert.equal(item().message,'导入完成');
assert.deepEqual(settled,['source-one']);assert.deepEqual(completed,[true]);assert.equal(timers.size,0);
""")

    def test_interrupted_response_body_retries_and_reports_actual_import_failure(self) -> None:
        self.run_js("""
let calls=0;
global.MEFinderApi={async fetch(){return {status:200,async json(){
 if(++calls===1) throw new TypeError('Failed to fetch');
 return {status:'failed',message:'索引写入失败',can_resume:true,failure_stage:'index'};
}};}};
jobs.add({id:'one',jobId:'job-one',status:'processing',step:2});
jobs.pollJob('one');await flush();
assert.equal(item().status,'processing');assert.equal(timers.size,1);await tick();
assert.equal(item().status,'error');assert.equal(item().message,'索引写入失败');
assert.equal(item().canResume,true);assert.equal(item().failureStage,'index');
assert.equal(completed.length,0);assert.equal(timers.size,0);
""")

    def test_invalid_status_json_is_reported_without_retry(self) -> None:
        self.run_js("""
global.MEFinderApi={async fetch(){return {status:200,async json(){throw new SyntaxError('Unexpected token');}};}};
jobs.add({id:'one',jobId:'job-one',status:'processing'});
jobs.pollJob('one');await flush();
assert.equal(item().status,'error');assert.equal(item().message,'Unexpected token');assert.equal(timers.size,0);
""")

    def test_removed_import_does_not_poll_again_after_disconnection(self) -> None:
        self.run_js("""
let calls=0;
global.MEFinderApi={async fetch(url){
 if(url==='/api/import-resume-dismiss') return response({state:'dismissed'});
 calls++;throw new TypeError('Failed to fetch');
}};
jobs.add({id:'one',jobId:'job-one',status:'processing'});
jobs.pollJob('one');await flush();assert.equal(timers.size,1);
const staleTimer=timers.values().next().value;
await jobs.remove('one',{skipConfirm:true});assert.equal(timers.size,0);
staleTimer();await flush();assert.equal(calls,1);
""")

    def test_cancel_from_polling_error_restarts_polling_and_exposes_cleanup_failure(self) -> None:
        self.run_js("""
global.MEFinderApi={async fetch(url){
 if(url==='/api/import-resume-dismiss') return response({state:'cancelling'});
 return response({status:'failed',message:'取消清理失败'});
}};
jobs.add({id:'one',jobId:'job-one',status:'error',type:'pdf'});
assert.equal(await jobs.remove('one'),true);await flush();
assert.equal(jobs.snapshot().length,1);assert.equal(item().status,'error');
assert.equal(item().cancelRequested,false);assert.equal(item().message,'取消清理失败');
assert.equal(timers.size,0);
""")

    def test_queue_snapshots_and_added_items_do_not_allow_external_writes(self) -> None:
        self.run_js("""
const file=new Blob(['book']);
const original={id:'one',jobId:'J',status:'processing',file};
jobs.add(original);original.status='done';
const snapshot=jobs.snapshot();snapshot[0].status='failed';snapshot.pop();
assert.equal(item().status,'processing');assert.equal(item().file,file);
assert.equal(jobs.snapshot().length,1);
""")

    def test_restore_resume_and_finish_update_the_same_task(self) -> None:
        self.run_js("""
let resumed=false;
global.MEFinderApi={async fetch(url){
 if(url==='/api/import-resumable') return response({jobs:[{job_id:'J',file_type:'pdf',status:'paused',
 can_resume:true,failure_stage:'index',parse_route:'vision',provider_id:'vision'}]});
 if(url==='/api/import-resume'){resumed=true;return response({job_id:'J'});}
 assert.ok(resumed);return response({status:'completed',source_file_id:'S'});
}};
await jobs.loadResumableImports();await jobs.loadResumableImports();
assert.equal(jobs.snapshot().length,1);assert.equal(item().status,'paused');
await jobs.resume(item().id);await flush();
assert.equal(confirmations.length,0,'index-only retry must not show a paid parsing prompt');
assert.equal(item().status,'done');assert.equal(item().step,5);
assert.equal(completed.length,1);assert.deepEqual(settled,['S']);assert.equal(timers.size,0);
""")

    def test_active_recovery_is_deduplicated_and_paused_state_stops_polling(self) -> None:
        self.run_js("""
let requests=0;
global.MEFinderApi={async fetch(url){
 if(url==='/api/import-active') return response({jobs:[{job_id:'J',file_type:'epub',status:'processing'}]});
 requests++;return response({status:'paused',can_resume:true,message:'已暂停'});
}};
await jobs.loadActiveImports();await flush();await jobs.loadActiveImports();
assert.equal(jobs.snapshot().length,1);assert.equal(item().type,'epub');
assert.equal(item().status,'paused');assert.equal(item().canResume,true);
assert.equal(requests,1);assert.equal(timers.size,0);
""")

    def test_provider_retries_keep_routes_flags_and_confirmations(self) -> None:
        self.run_js("""
const requests=[];
global.MEFinderApi={async fetch(url,options){
 if(url.startsWith('/api/import-status')) return new Promise(()=>{});
 requests.push([url,JSON.parse(options.body)]);
 return response({job_id:'new-'+requests.length,provider_id:'vision',provider_name:'视觉 API'});
}};
jobs.add({id:'one',jobId:'old',type:'pdf',status:'error',visionFailed:true,
 needsProviderConfig:true,canRetryVision:true,canRetryLocalMineru:true,mineruFailed:true});
await jobs.retryWithVision('one');
assert.equal(item().route,'vision');assert.equal(item().needsProviderConfig,false);
assert.equal(item().visionFailed,false);assert.equal(item().canRetryVision,false);
await jobs.retryWithMinerU('one');
assert.equal(item().route,'mineru');assert.equal(item().providerId,null);
await jobs.retryWithLocalMinerU('one');
assert.equal(item().providerId,'mineru-local');assert.equal(item().canRetryLocalMineru,false);
assert.equal(item().mineruFailed,false);assert.equal(item().status,'processing');
assert.equal(confirmations.length,3);
assert.deepEqual(requests.map(row=>row[0]),['/api/import-retry','/api/import-retry-mineru','/api/import-retry-mineru-local']);
assert.equal(requests[0][1].provider_id,'vision');
""")

    def test_deleted_task_ignores_late_completion_and_clears_timer(self) -> None:
        self.run_js("""
let release;
global.MEFinderApi={async fetch(url){
 if(url==='/api/import-resume-dismiss') return response({state:'dismissed'});
 return new Promise(resolve=>{release=resolve;});
}};
jobs.add({id:'one',jobId:'J',status:'processing'});jobs.pollJob('one');
await jobs.remove('one',{skipConfirm:true});
release(response({status:'completed',source_file_id:'S'}));await flush();
assert.equal(jobs.snapshot().length,0);assert.equal(completed.length,0);assert.equal(timers.size,0);
""")

    def test_reparse_joins_existing_task_without_an_extra_poll(self) -> None:
        self.run_js("""
let polls=0;
global.MEFinderApi={async fetch(url){
 if(url==='/api/mineru-reparse') return response({ok:true,job_id:'J',already_running:true});
 polls++;return new Promise(()=>{});
}};
jobs.add({id:'existing',jobId:'J',status:'processing'});jobs.pollJob('existing');
await jobs.reparse('S',{title:'book'});
assert.equal(jobs.snapshot().length,1);assert.equal(item().id,'existing');
assert.equal(polls,1);assert.match(item().message,/进行中/);
""")
