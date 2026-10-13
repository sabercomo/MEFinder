"""Execute shared catalog requests while a document change invalidates them."""

import shutil
import subprocess
import unittest
from pathlib import Path


JS = Path(__file__).resolve().parents[1] / "src/me_finder/static/js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is required for frontend behavior tests")
class LibraryRefreshFrontendTests(unittest.TestCase):
    def test_invalidated_request_cannot_restore_old_catalog_or_clear_new_request(self):
        script = r"""
const fs=require('fs'),assert=require('assert/strict');
global.window=global;
global.addEventListener=()=>{};
global.document={addEventListener:()=>{}};
global.MEFinderActions={register:()=>{},registerInline:()=>{}};
global.searchStore={};global.libraryStore={};
const requests=[];
global.MEFinderApi={fetch:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))};
eval.call(global,fs.readFileSync(process.argv[1]+'/20-search.js','utf8'));
const response=data=>({ok:true,json:async()=>data});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 for(const oldFails of [false,true]) {
  invalidateLibraryCatalog();requests.length=0;
  const old=fetchLibraryCatalog();
  invalidateLibraryCatalog();
  const current=fetchLibraryCatalog();
  assert.equal(fetchLibraryCatalog(),current,'consumers share the current request');
  if(oldFails) requests[0].reject(Error('obsolete request failed'));
  else requests[0].resolve(response({items:['old']}));
  await flush();
  assert.equal(searchStore.libraryCatalog,null,'obsolete response must not repopulate the catalog');
  assert.equal(searchStore.libraryCatalogPromise,current,'obsolete response must not clear the new request');
  const fresh={items:['new']};
  requests[1].resolve(response(fresh));
  assert.equal(await current,fresh);
  assert.equal(await old,fresh,'waiting consumers must also receive the new catalog');
  assert.equal(searchStore.libraryCatalog,fresh);
  assert.equal(requests.length,2,'invalidation creates only one replacement request');
 }
 invalidateLibraryCatalog();requests.length=0;
 const obsolete=fetchLibraryCatalog();
 invalidateLibraryCatalog();
 const replacement=fetchLibraryCatalog();
 requests[0].resolve(response({items:['obsolete']}));
 await flush();
 const bothRejected=Promise.all([
  assert.rejects(obsolete,/replacement failed/),assert.rejects(replacement,/replacement failed/)
 ]);
 requests[1].reject(Error('replacement failed'));
 await bothRejected;
 assert.equal(requests.length,2,'a failed replacement must not trigger an automatic retry');
 invalidateLibraryCatalog();
 const failed=fetchLibraryCatalog();
 const rejection=assert.rejects(failed,/current request failed/);
 requests.at(-1).reject(Error('current request failed'));
 await rejection;
 assert.equal(searchStore.libraryCatalogPromise,null,'current errors remain retryable');
 const retry=fetchLibraryCatalog(),fresh={items:['retry']};
 requests.at(-1).resolve(response(fresh));
 assert.equal(await retry,fresh);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            [NODE, "-e", script, str(JS)], capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
