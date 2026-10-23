const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const source=name=>fs.readFileSync(path.join(root,'disk_downloader/static',name),'utf8');
function block(code,start){
  const begin=code.indexOf(start);assert.notEqual(begin,-1,start);
  const first=code.indexOf('{',begin);let depth=1,index=first+1;
  for(;depth&&index<code.length;index++)depth+=(code[index]==='{')-(code[index]==='}');
  assert.equal(depth,0,start);return code.slice(begin,index);
}
function line(code,start){const value=code.split('\n').find(value=>value.startsWith(start));assert.ok(value,start);return value;}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
class Element{
  constructor(tag='div',text){this.tagName=tag.toUpperCase();this.textContent=text??'';this.value='';this.checked=false;this.disabled=false;this.dataset={};this.children=[];this.listeners=[];this.events=new Map();this.open=false;this.hidden=false;this.onchange=()=>{};}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  setAttribute(){}
  contains(){return false;}
  addEventListener(event,handler){if(event==='click')this.listeners.push(handler);else this.events.set(event,[...(this.events.get(event)||[]),handler]);}
  emit(event){for(const handler of this.events.get(event)||[])handler();}
  showModal(){this.open=true;}
  close(){this.open=false;this.emit('close');}
  scrollIntoView(){}
  async click(){if(this.onclick)await this.onclick();for(const listener of this.listeners)await listener();}
}
function commandHarness({destinationGate,planGate,scheduled=false}={}){
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const requests=[],destinationEntered=deferred(),planEntered=deferred();
  const state={job:'job-A',jobRevision:0,jobStatus:{id:'job-A',root:'folder-A',settings:scheduled?{run_at:'2099-01-01T00:00:00Z'}:{}},connected:true,actionPending:false};
  const context=vm.createContext({state,$,errorSelections:new Set(),outcomes:{},document:{activeElement:null},node:(tag,text)=>new Element(tag,text),safely:operation=>operation(),runtimeLimits:()=>({workers:8}),pollJob:async()=>{},notice:()=>{},checkDestination:async()=>{destinationEntered.resolve();return destinationGate?destinationGate.promise:{free_bytes:100};},api:async(url,body)=>{requests.push({url,body});if(url.endsWith('/plan')){planEntered.resolve();return planGate?planGate.promise:{known_bytes:1};}return {ok:true};}});
  const app=source('app.js');vm.runInContext(block(app,'function selectJob('),context);
  vm.runInContext(block(app,'async function action('),context);
  const upgrade=source('upgrade.js');vm.runInContext(upgrade.slice(upgrade.indexOf('const originalAction=action;'),upgrade.indexOf('const exclusionList=')),context);
  const v150=source('v150.js');vm.runInContext(v150.slice(v150.indexOf('const actionBeforeScheduled150=action;'),v150.indexOf('const renderBeforeScheduled150=')),context);
  const select=id=>{context.selectJob(id);state.jobStatus={id,root:'folder-'+id,settings:{}};};
  return {context,state,$,requests,destinationEntered,planEntered,select};
}
test('AUD-02 changing task during plan never dispatches to another job',async()=>{
  const gate=deferred(),h=commandHarness({planGate:gate});const pending=h.context.action('start');await h.planEntered.promise;
  h.select('job-B');gate.resolve({known_bytes:1});await pending;
  assert.equal(h.requests.filter(item=>item.body).length,0);
});
test('AUD-02 changing task during destination cancels preflight',async()=>{
  const gate=deferred(),h=commandHarness({destinationGate:gate});const pending=h.context.action('start');await h.destinationEntered.promise;
  h.select('job-B');gate.resolve({free_bytes:100});await pending;
  assert.equal(h.requests.length,0);
});
test('AUD-02 returning to the original selection does not revive stale preflight',async()=>{
  const gate=deferred(),h=commandHarness({planGate:gate});const pending=h.context.action('start');await h.planEntered.promise;
  h.select('job-B');h.select('job-A');gate.resolve({known_bytes:1});await pending;
  assert.equal(h.requests.filter(item=>item.body).length,0);
});
test('AUD-02 unchanged selection sends exactly the requested start',async()=>{
  const h=commandHarness();await h.context.action('start');
  const commands=h.requests.filter(item=>item.body);assert.equal(commands.length,1);assert.equal(commands[0].url,'/api/jobs/job-A/action');assert.equal(commands[0].body.action,'start');
});
test('AUD-02 explicit stale callback identity is rejected for every task verb',async()=>{
  for(const verb of ['start','resume','pause','stop','retry','retry_failed','pack']){
    const h=commandHarness();h.select('job-B');await h.context.action(verb,'job-A');assert.equal(h.requests.length,0,verb);
  }
});
test('AUD-02 scheduled start queues the captured task once',async()=>{
  const h=commandHarness({scheduled:true});await h.context.action('start');
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/api/queue');assert.equal(h.requests[0].body.job,'job-A');
});
test('AUD-02 failed preflight and duplicate clicks never produce a start',async()=>{
  const gate=deferred(),h=commandHarness({planGate:gate});const first=h.context.action('start');await h.planEntered.promise;
  await h.context.action('start');gate.resolve({known_bytes:101});await assert.rejects(first,/недостаточно/);
  assert.equal(h.requests.length,1);assert.equal(h.requests.filter(item=>item.body).length,0);
});
test('AUD-02 old error-row retry cannot target the newly selected job',async()=>{
  const h=commandHarness();
  h.context.api=async(url,body)=>{h.requests.push({url,body});return {items:[{path:'disk:/one.bin',status:'error',error:'synthetic',original_status:'failed',preview_status:'failed'}],has_more:false};};
  const app=source('app.js');vm.runInContext(block(app,'async function loadErrors('),h.context);vm.runInContext(block(app,'async function retrySelected('),h.context);
  h.state.errorOffset=0;await h.context.loadErrors();
  const row=h.$('errors-list').children[0],retry=row.children.find(child=>child.textContent==='Повторить этот файл');assert.ok(retry);
  h.select('job-B');await retry.click();assert.equal(h.requests.filter(item=>item.body).length,0);
});

function profileHarness(){
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const reports=['csv','json','html','txt'].map(value=>Object.assign(new Element('input'),{value}));
  const types=['photos','videos','music','files'].map(value=>Object.assign(new Element('input'),{value}));
  const context=vm.createContext({$,profiles:[],selected:new Set(),state:{},typeLabels:{photos:'photo',videos:'video',music:'music',files:'file'},document:{querySelectorAll:selector=>{const group=selector.includes('name=report')?reports:selector.includes('file-type')?types:[];return selector.includes(':checked')?group.filter(item=>item.checked):group;}},safely:operation=>operation(),updateSelection:()=>{},renderFiles:()=>{},renderSetup:()=>{},renderExclusionChoices:()=>{},notice:()=>{},verifySelect:{value:'fast'},duplicateCheck:{checked:false},dateFrom:{value:''},dateTo:{value:''},dateField:{value:'modified'},unknownDates:{checked:true},priorityPaths:{value:''},scheduledAt:new Element('input'),quietEnabled:{checked:false},quietStart:{value:'23'},quietEnd:{value:'8'},quietSpeed:{value:'0'},quietMute:{checked:true},preventSleep:{checked:false}});
  const app=source('app.js'),experience=source('experience.js'),upgrade=source('upgrade.js'),v150=source('v150.js');
  for(const name of ['function advancedSettings(','function settings('])vm.runInContext(block(app,name),context);
  vm.runInContext(block(experience,'function applyDownloadSettings('),context);
  for(const file of [upgrade,v150])for(const value of file.split('\n'))if(/^(const originalAdvancedSettings=|advancedSettings=function\(|const originalApplySettings=|applyDownloadSettings=function\(|const oldAdvanced150=|const oldApply150=|const settingsBeforeSchedule=|const applyBeforeSchedule=)/.test(value))vm.runInContext(value,context);
  for(const row of v150.split('\n'))if(row.startsWith('scheduledAt.step=')||row.startsWith("scheduledAt.addEventListener('input'"))vm.runInContext(row,context);
  vm.runInContext(line(source('features.js'),"$('apply-profile').onclick="),context);
  for(const file of [experience,upgrade])for(const value of file.split('\n'))if(value.startsWith("$('apply-profile').addEventListener('click'"))vm.runInContext(value,context);
  return {context,$,profile:value=>{context.profiles=[{name:'synthetic',settings:value}];$('profile-list').value='synthetic';return $('apply-profile').click();}};
}
const completeProfile={operation:'missing',missing_policy:'both',paths:['disk:/Priority','disk:/Another'],whole_disk:false,destination:'/synthetic/downloads',mode:'both',file_types:['photos','videos'],preview_size:'XL',workers:5,attempts:3,timeout:41,total_timeout:91,reports:['json','html'],auto_start:false,create_zip:false,adaptive_workers:false,resume_parts:false,auto_pause:false,max_disk_percent:91,speed_limit_mib:3.5,exclusions:['*.tmp'],verification_mode:'full',deduplicate:true,date_from:'2020-01-01',date_to:'2020-12-31',date_field:'created',include_unknown_dates:false,priority_paths:['disk:/Priority'],run_at:'2030-01-01T12:34:00.000Z',quiet_hours:{enabled:true,start:21,end:7,speed_limit_mib:1.5,mute:false},prevent_sleep:true};
function assertProfile(context,expected){
  const serialized=JSON.parse(JSON.stringify(context.settings()));
  for(const [key,value] of Object.entries(expected))assert.deepEqual(serialized[key],value,key);
}
test('AUD-03 profile round-trip restores every user-facing setting',async()=>{
  const h=profileHarness();await h.profile(completeProfile);assertProfile(h.context,completeProfile);
});
test('AUD-03 applying opposite profiles clears all previous late settings',async()=>{
  const h=profileHarness();await h.profile(completeProfile);assertProfile(h.context,completeProfile);
  const reset={...completeProfile,operation:'new',missing_policy:'any_result',paths:[],whole_disk:true,mode:'previews',file_types:['music','files'],preview_size:'S',workers:2,attempts:1,timeout:20,total_timeout:0,reports:['csv'],auto_start:true,create_zip:true,resume_parts:true,auto_pause:true,max_disk_percent:95,speed_limit_mib:0,exclusions:[],verification_mode:'fast',deduplicate:false,date_from:'',date_to:'',date_field:'modified',include_unknown_dates:true,priority_paths:[],run_at:'',quiet_hours:{enabled:false,start:23,end:8,speed_limit_mib:0,mute:true},prevent_sleep:false};
  await h.profile(reset);assertProfile(h.context,reset);
});
test('AUD-03 applying a damaged profile leaves the form unchanged',async()=>{
  const h=profileHarness();h.context.applyDownloadSettings(completeProfile);
  h.context.profiles=[{name:'synthetic',settings:null,error:'broken'}];h.$('profile-list').value='synthetic';
  await assert.rejects(h.$('apply-profile').click(),/broken/);assertProfile(h.context,completeProfile);
});

test('R-01 imported schedule preserves seconds and milliseconds',async()=>{
  const h=profileHarness(),expected={...completeProfile,run_at:'2030-01-01T12:34:56.789Z'};
  await h.profile(expected);assertProfile(h.context,expected);
});

test('R-01 imported schedule preserves second DST-fold instant',async()=>{
  const original=process.env.TZ;process.env.TZ='America/New_York';
  try{
    const h=profileHarness(),expected={...completeProfile,run_at:'2026-11-01T06:30:00.000Z'};
    await h.profile(expected);assertProfile(h.context,expected);
  }finally{if(original===undefined)delete process.env.TZ;else process.env.TZ=original;}
});


test('R-01 editing the local schedule uses the edited instant',async()=>{
  const h=profileHarness();await h.profile({...completeProfile,run_at:'2030-01-01T12:34:56.789Z'});
  h.context.scheduledAt.value='2030-01-02T16:10:09.123';h.context.scheduledAt.emit('input');
  assert.equal(h.context.settings().run_at,new Date('2030-01-02T16:10:09.123').toISOString());
});

test('R-01 explicit input clears a preserved DST-fold offset',async()=>{
  const original=process.env.TZ;process.env.TZ='America/New_York';
  try{
    const h=profileHarness();await h.profile({...completeProfile,run_at:'2026-11-01T06:30:56.789Z'});
    h.context.scheduledAt.emit('input');
    assert.equal(h.context.settings().run_at,'2026-11-01T05:30:56.789Z');
    h.context.scheduledAt.value='';h.context.scheduledAt.emit('input');assert.equal(h.context.settings().run_at,'');
  }finally{if(original===undefined)delete process.env.TZ;else process.env.TZ=original;}
});

function diagnosticsHarness({saveGate}={}){
  const featureSource=source('features.js'),elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const state={job:'job-A',jobRevision:0},requests=[],previewGate=deferred(),entered=deferred(),saving=deferred();
  const context=vm.createContext({state,$,safely:fn=>fn(),loadUpdates:async()=>{},api:async(url,body)=>{requests.push({url,body});if(url.endsWith('diagnostics-preview')){entered.resolve();return previewGate.promise;}saving.resolve();return saveGate?saveGate.promise:{local:'synthetic-diagnostics.zip'};}});
  for(const row of featureSource.split('\n'))if(row.startsWith('let diagnostics')||row.startsWith('const diagnosticsCurrent=')||row.startsWith('const diagnosticsSettled='))vm.runInContext(row,context);
  for(const start of ["$('open-service').onclick=","$('close-service').onclick=","$('preview-diagnostics').onclick=","$('save-diagnostics').onclick="])vm.runInContext(line(featureSource,start),context);
  for(const row of featureSource.split('\n'))if(row.startsWith("$('service-dialog').addEventListener('close'"))vm.runInContext(row,context);
  const payload={files:{'environment.json':{},'settings.json':{},'summary.json':{},'errors.json':[],'events.json':[]}};
  return {state,$,requests,previewGate,entered,saving,payload,open:()=>$('open-service').onclick(),close:()=>$('close-service').onclick(),preview:()=>$('preview-diagnostics').onclick(),save:()=>$('save-diagnostics').onclick(),job:()=>vm.runInContext('diagnosticsJob',context),select:id=>{state.job=id;state.jobRevision++;}};
}

test('AUD-16 stale preview is ignored after closing and selecting another job',async()=>{
  const h=diagnosticsHarness();await h.open();const pending=h.preview();await h.entered.promise;
  h.close();h.select('job-B');await h.open();h.previewGate.resolve(h.payload);await pending;
  assert.equal(h.job(),null);assert.equal(h.$('diagnostics-preview').hidden,true);
});
test('AUD-16 reopening same dialog never revives its old pending preview',async()=>{
  const h=diagnosticsHarness();await h.open();const pending=h.preview();await h.entered.promise;
  h.$('service-dialog').close();await h.open();h.previewGate.resolve(h.payload);await pending;
  assert.equal(h.job(),null);assert.equal(h.$('diagnostics-preview').hidden,true);
});
test('AUD-16 unchanged preview saves exactly the job whose contents are shown',async()=>{
  const h=diagnosticsHarness();await h.open();const pending=h.preview();await h.entered.promise;h.previewGate.resolve(h.payload);await pending;await h.save();
  assert.equal(h.job(),'job-A');assert.equal(h.requests.at(-1).url,'/api/jobs/job-A/diagnostics');
});
test('AUD-16 stale save completion does not overwrite reopened dialog status',async()=>{
  const saveGate=deferred(),h=diagnosticsHarness({saveGate});await h.open();const preview=h.preview();await h.entered.promise;h.previewGate.resolve(h.payload);await preview;
  const pending=h.save();await h.saving.promise;h.close();h.select('job-B');await h.open();const status=h.$('diagnostics-status').textContent;
  saveGate.resolve({local:'synthetic-A.zip'});await pending;
  assert.equal(h.$('diagnostics-status').textContent,status);
});


test('AUD-16 a stale preview failure is ignored after the dialog changes',async()=>{
  const h=diagnosticsHarness();await h.open();const pending=h.preview();await h.entered.promise;
  h.close();h.select('job-B');await h.open();h.previewGate.reject(new Error('old task request failed'));await pending;
  assert.equal(h.job(),null);assert.equal(h.$('diagnostics-preview').hidden,true);
});

test('AUD-16 a current diagnostics failure remains visible to the caller',async()=>{
  const h=diagnosticsHarness();await h.open();const pending=h.preview();await h.entered.promise;
  h.previewGate.reject(new Error('current task request failed'));await assert.rejects(pending,/current task request failed/);
  assert.equal(h.$('preview-diagnostics').disabled,false);
});

test('AUD-16 delayed close event cannot invalidate a reopened dialog',async()=>{
  const h=diagnosticsHarness();await h.open();h.close();await h.open();const pending=h.preview();await h.entered.promise;
  h.$('service-dialog').emit('close');h.previewGate.resolve(h.payload);await pending;
  assert.equal(h.job(),'job-A');assert.equal(h.$('diagnostics-preview').hidden,false);
});

test('AUD-16 same-dialog task change leaves preview controls usable',async()=>{
  const h=diagnosticsHarness();await h.open();const pending=h.preview();await h.entered.promise;
  h.select('job-B');h.previewGate.resolve(h.payload);await pending;
  assert.equal(h.job(),null);assert.equal(h.$('diagnostics-preview').hidden,true);
  assert.equal(h.$('preview-diagnostics').disabled,false);
  assert.equal(h.$('save-diagnostics').disabled,true);
  assert.match(h.$('diagnostics-status').textContent,/job-B/);
});

test('AUD-16 same-dialog task change after save resets stale contents',async()=>{
  const saveGate=deferred(),h=diagnosticsHarness({saveGate});await h.open();const preview=h.preview();await h.entered.promise;h.previewGate.resolve(h.payload);await preview;
  const pending=h.save();await h.saving.promise;h.select('job-B');saveGate.resolve({local:'synthetic-A.zip'});await pending;
  assert.equal(h.job(),null);assert.equal(h.$('diagnostics-preview').hidden,true);
  assert.equal(h.$('preview-diagnostics').disabled,false);assert.equal(h.$('save-diagnostics').disabled,true);
});
