const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
  constructor(){this.children=[];this.dataset={};this.value='';this.textContent='';}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(key,value){this[key]=value;}
}
const nodes=new Map();
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},createElement(){return new Element();}};
const events=Array.from({length:50000},(_,i)=>({time:'2026-01-01T00:00:00Z',category:'cache',level:'info',message:'file-'+i,path:'disk:/'+i}));
document.getElementById('journal-data').textContent=JSON.stringify({summary:{updated:'2026-01-01',processed:1,files:1,saved_bytes:1,groups:{cache:'Cache'}},events});
document.getElementById('level').value='all';
const script=fs.readFileSync('disk_downloader/templates/journal.html','utf8').split('<script>')[1].split('</script>')[0];
const context=vm.createContext({document,setTimeout,clearTimeout});vm.runInContext(script,context);
assert.equal(nodes.get('rows').children.length,50,'Initial page rendered the complete journal');
(async()=>{
  nodes.get('search').value='not-found';const first=vm.runInContext('filter()',context);
  let yielded=false;setTimeout(()=>{yielded=true;},0);
  nodes.get('search').value='file-49999';await vm.runInContext('filter()',context);await first;
  assert.ok(yielded,'Filtering blocked the event loop');
  assert.equal(nodes.get('rows').children.length,1);
  assert.equal(vm.runInContext('filtered.length',context),1,'A stale search replaced the current result');
  assert.equal(nodes.get('rows')['aria-busy'],'false');
  nodes.get('search').value='';await vm.runInContext('filter()',context);
  assert.equal(nodes.get('rows').children.length,50);
  assert.equal(vm.runInContext('filtered',context),null,'Unfiltered view unnecessarily copied all events');
})().catch(error=>{console.error(error);process.exitCode=1;});
