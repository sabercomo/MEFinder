"""Run the optional component controls, including errors and selection, in Node."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node is unavailable')
class BertalignSettingsUiTests(unittest.TestCase):
    def test_install_cancel_and_backend_selection(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={},calls=[],events=[];
let backend='default', component={supported:true,installed:false,operation:null}, broken=false;
const node=id=>nodes[id]||(nodes[id]={});
const ctx={document:{getElementById:node},setTimeout(){return 1;},clearTimeout(){},
  localStorage:{setItem(k,v){events.push([k,v]);}},window:{dispatchEvent(e){events.push(e.type);}},Event:class {constructor(type){this.type=type;}},
  async fetch(url,options){
    if(broken) throw Error('network failed');
    const payload=options.body?JSON.parse(options.body):null;calls.push([url,payload]);
    if(url==='/api/preferences') {if(payload)backend=payload.alignment_backend;return {ok:true,json:async()=>({alignment_backend:backend})};}
    if(payload){component={...component,operation:payload.action==='install'?'install':null};}
    return {ok:true,json:async()=>({...component})};
  }};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
(async()=>{
 await ctx.MEFinderBertalignSettings.load();assert.equal(node('alignment-backend').value,'default');
 assert.equal(node('bertalign-action').disabled,false);
 node('bertalign-action').onclick();for(let i=0;i<15;i++)await Promise.resolve();
 assert.equal(node('bertalign-cancel').hidden,false);assert.equal(calls.at(-1)[1].backend,'bertalign');
 node('bertalign-cancel').onclick();for(let i=0;i<15;i++)await Promise.resolve();
 assert.equal(calls.at(-1)[1].action,'cancel');
 await ctx.MEFinderBertalignSettings.select('bertalign');assert.equal(backend,'bertalign');assert(events.includes('library_changed'));
 broken=true;await ctx.MEFinderBertalignSettings.load();assert.equal(node('bertalign-action').textContent,'重新读取');
 broken=false;component={supported:true,installed:true,has_models:true,operation:null};
 await node('bertalign-action').onclick();assert.equal(node('bertalign-state').textContent,'已安装');assert.equal(node('bertalign-uninstall').hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        path = Path(__file__).resolve().parents[1] / 'src/me_finder/static/js/61-bertalign.js'
        result = subprocess.run([shutil.which('node'), '-e', script, str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
