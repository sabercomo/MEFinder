"""Execute the actual library script; mock only DOM and transport."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node unavailable')
class MarkdownPageExportUiTests(unittest.TestCase):
    def test_dialog_selection_cancel_failure_retry_and_duplicate_submit(self):
        source = Path(__file__).resolve().parents[1] / 'src/me_finder/static/js/30-library.js'
        script = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const nodes = {}, calls = [], toasts = [];
let picker = '/exports', resolveFetch;
function classList(){const set=new Set();return {toggle(name,on){on?set.add(name):set.delete(name);},contains(name){return set.has(name);}};}
for (const id of ['md-page-mode-printed','md-page-mode-physical','md-page-mode-printed-item','md-page-mode-physical-item','md-page-input','md-page-error','md-page-note','md-page-fields','markdown-page-dialog']) {
  nodes[id] = {value:'',textContent:'',disabled:false,checked:false,classList:classList(),focus(){},querySelector(){return null;},
    showModal(){this.open=true;},close(){this.open=false;}};
}
const context = {module:{exports:{}}, libraryStore:{sources:[{source_file_id:'pdf',source_type:'pdf'}, {source_file_id:'epub',source_type:'word'}]},
  document:{activeElement:{isConnected:true,focus(){}},getElementById(id){return nodes[id];}},
  sourceFormatLabel(s){return s.source_type === 'pdf' ? 'PDF' : 'EPUB';},
  chooseDesktopExportDirectory:async()=>picker,
  fetch:async(url,opts)=>{calls.push([url,JSON.parse(opts.body)]);return new Promise(r=>{resolveFetch=r;});},
  showToast(...args){toasts.push(args);}};
vm.createContext(context); vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const event = {preventDefault(){}};
(async()=>{
  context.MEFinder.library.pageExport.open('epub');
  assert.equal(nodes['md-page-mode-physical'].disabled,true);
  assert.equal(nodes['md-page-mode-physical-item'].classList.contains('is-disabled'),true);
  assert.ok(nodes['md-page-note'].textContent.includes('脚注'));
  context.MEFinder.library.pageExport.close();
  context.MEFinder.library.pageExport.open('pdf');
  assert.equal(nodes['md-page-mode-physical'].disabled,false);
  assert.equal(nodes['md-page-mode-physical-item'].classList.contains('is-disabled'),false);
  assert.equal(nodes['md-page-mode-printed'].checked,true);
  await context.MEFinder.library.pageExport.submit(event); assert.equal(calls.length,0);
  nodes['md-page-input'].value='12-18,25';
  picker=null; await context.MEFinder.library.pageExport.submit(event);
  assert.equal(calls.length,0); assert.equal(nodes['md-page-fields'].disabled,false);
  picker='/exports'; const first=context.MEFinder.library.pageExport.submit(event);
  await Promise.resolve(); await context.MEFinder.library.pageExport.submit(event);
  assert.equal(calls.length,1); assert.equal(nodes['md-page-fields'].disabled,true);
  context.MEFinder.library.pageExport.cancel(event); assert.equal(nodes['markdown-page-dialog'].open,true);
  assert.deepEqual(calls[0],['/api/document/export-markdown',{source_id:'pdf',output_dir:'/exports',page_selection:{mode:'printed',pages:'12-18,25'}}]);
  resolveFetch({ok:false,json:async()=>({error:'页码不存在'})}); await first;
  assert.equal(nodes['md-page-fields'].disabled,false); assert.equal(nodes['md-page-error'].textContent,'页码不存在');
  const retry=context.MEFinder.library.pageExport.submit(event); await Promise.resolve();
  resolveFetch({ok:true,json:async()=>({path:'/exports/book.md',page_count:8,warnings:['未配对脚注保留']})}); await retry;
  assert.equal(nodes['markdown-page-dialog'].open,false); assert.equal(toasts.length,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', script, str(source)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
