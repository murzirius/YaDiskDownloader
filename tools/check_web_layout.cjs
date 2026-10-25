// Visual regression check for the synthetic server in ui_layout_fixture.py.
// Requires the Playwright developer package and a Chromium browser; production has no browser dependency.
// LAYOUT_URL may select a different fixture port. Never run against a real download session.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('fs'),path=require('path');
const out=path.resolve(process.argv[2]||'layout-results');fs.mkdirSync(out,{recursive:true});
const url=process.env.LAYOUT_URL||'http://127.0.0.1:18766';
const audit=()=>{
 const scope=document.querySelector('dialog[open]')||document.body;
 const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[hidden]')&&e.getBoundingClientRect().width>0;
 const label=e=>(e.id||e.textContent.trim().replace(/\s+/g,' ').slice(0,85));
 const issues=[];
 if(document.documentElement.scrollWidth>innerWidth+1)issues.push({kind:'page-overflow',width:document.documentElement.scrollWidth,viewport:innerWidth});
 const buttons=[...scope.querySelectorAll('button')].filter(visible);
 for(const b of buttons){const r=b.getBoundingClientRect();
  if(b.scrollWidth>b.clientWidth+2)issues.push({kind:'button-clipped',label:label(b),extra:b.scrollWidth-b.clientWidth});
  if(r.left<-1||r.right>innerWidth+1)issues.push({kind:'button-outside-viewport',label:label(b),left:r.left,right:r.right});
 }
 for(let i=0;i<buttons.length;i++)for(let j=i+1;j<buttons.length;j++){
  const a=buttons[i],b=buttons[j];if(a.parentElement!==b.parentElement||a.closest('.select-menu'))continue;
  const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();
  const overlapY=Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top),overlapX=Math.min(x.right,y.right)-Math.max(x.left,y.left);
  const horizontal=Math.max(x.left,y.left)-Math.min(x.right,y.right),vertical=Math.max(x.top,y.top)-Math.min(x.bottom,y.bottom);
  if(overlapY>3&&horizontal<5.5||overlapX>3&&vertical<5.5)issues.push({kind:'buttons-touch',a:label(a),b:label(b),gap:Math.round(Math.max(horizontal,vertical)*10)/10});
 }
 return {issues,buttons:buttons.length};
};
(async()=>{
 const browser=await chromium.launch({...(process.env.LAYOUT_BROWSER?{channel:process.env.LAYOUT_BROWSER}:process.platform==='win32'?{channel:'msedge'}:{}),headless:true});
 const results=[],errors=[];
 try{
 for(const width of (!process.env.QUICK_LAYOUT?'320,390,650,768,1000,1100,1440,1920':'390,1100').split(',').map(Number)){
 for(const scale of (!process.env.QUICK_LAYOUT?['1','1.3']:['1.3'])){
  const context=await browser.newContext({viewport:{width,height:Number(process.env.LAYOUT_HEIGHT||900)},deviceScaleFactor:1});
  await context.addInitScript(scale=>{if(!location.protocol.startsWith('http'))return;localStorage.setItem('disk-downloader-help-seen-v1','1');localStorage.setItem('yadisk-simple','false');localStorage.setItem('yadisk-font-scale',scale);},scale);
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);await page.waitForFunction(()=>document.getElementById('open-themes')&&state.connected);
  if(process.env.LAYOUT_THEME)await page.evaluate(name=>{const theme=baseThemes.find(t=>t.name===name);if(!theme)throw Error('Unknown test theme');useTheme(theme,false);},process.env.LAYOUT_THEME);
  if(process.env.LAYOUT_COMPACT)await page.evaluate(()=>{compact.checked=true;compact.onchange();});
  const fixture=await (await context.request.get(url+'/qa/state')).json();
  assert.equal(fixture.synthetic_layout_fixture,true,'A dedicated synthetic fixture is required');
  assert.deepEqual(await page.evaluate(()=>[bytes(null),bytes(0),bytes(1024),bytes(1024**2),bytes(1024**3),bytes(1024**4)]),['неизвестно','0 Б','1 КБ','1 МБ','1 ГБ','1 ТБ']);
  await page.evaluate(async id=>{const j=await api('/api/jobs/'+id);$('destination').value=j.settings.destination;},fixture.ready);
  async function record(name){await page.waitForTimeout(90);const r=await page.evaluate(audit);results.push({width,scale,name,...r});if((r.issues.length||process.env.SCREENSHOTS&&[390,1440].includes(width)&&scale==='1.3')&&['home','job','themes','doctor','tools-verify','wizard-3','journal','changes-report','html-report'].includes(name))await page.screenshot({path:path.join(out,`${width}-${scale}-${name}.png`),fullPage:false});}
  async function close(){await page.evaluate(()=>{document.querySelectorAll('dialog[open]').forEach(d=>d.close());for(const item of enhancedSelects)item.close();});}
  await record('home');
  await page.locator('#home-new').click();await record('wizard-1');
  await page.locator('#whole-disk').check();await page.evaluate(()=>advanceSetup(2));await record('wizard-2');
  await page.evaluate(()=>advanceSetup(3));await page.locator('#settings-panel details').evaluateAll(items=>items.forEach(d=>d.open=true));await record('wizard-3');
  await page.evaluate(id=>openJob(id),fixture.completed);await record('job');
  await page.locator('#job-content details').evaluateAll(items=>items.forEach(d=>d.open=true));await record('job-expanded');
  for(const [name,id] of [['themes','open-themes'],['help','open-help'],['service','open-service'],['diagnostics','open-diagnostics'],['inventory','folder-volumes'],['changes','show-changes'],['files','browse-task-files'],['duplicates','review-duplicates']]){
   await close();await page.locator('#'+id).click();await record(name);
  }
  await close();await page.locator('#open-help').click();await page.locator('#help-next').click();await record('help-2');await page.locator('#help-next').click();await record('help-3');
  await close();await page.getByRole('button',{name:'Проверить приложение',exact:true}).click();await page.waitForTimeout(400);await record('doctor');
  await close();await page.locator('#open-tools').click();
  for(const mode of ['history','queue','verify','backups','cleanup']){await page.locator('[data-tool='+mode+']').click();await record('tools-'+mode);}
  for(const [name,id] of [['traffic','open-traffic'],['install','install-windows']]){await close();await page.locator('#open-tools').click();await page.locator('#'+id).click();await record(name);}
  await close();await page.locator('#open-tools').click();await page.getByRole('searchbox',{name:'Найти настройку'}).fill('потоки');await record('settings-search');await page.locator('.settings-search-results > button').first().click();assert.equal(await page.locator('#settings-panel').isVisible(),true);assert.equal(await page.locator('#verification-workers').locator('..').locator('.select-choice').isVisible(),true);await record('settings-search-destination');await page.evaluate(id=>openJob(id),fixture.completed);
  await close();await page.locator('#open-service').click();await page.locator('#report-issue').click();await record('issue');
  await close();await page.evaluate(()=>{void askConfirmation('Остановить обработку и сохранить прогресс? Уже скачанные файлы останутся в выбранной папке.');});await record('confirmation');await page.locator('.confirmation-dialog').getByRole('button',{name:'Отмена',exact:true}).click();
  await page.evaluate(()=>{void chooseFolderInApp($('destination').value);});await page.locator('.folder-browser').waitFor({state:'visible'});await page.waitForTimeout(200);await record('folder-browser');await page.locator('.folder-browser').getByRole('button',{name:'Отмена',exact:true}).click();
  await close();await page.evaluate(()=>{notice('Приложение на ПК: накопитель временно недоступен. Проверьте подключение и повторите попытку.');$('service-warning').hidden=false;$('service-warning-text').textContent='Приложение на ПК не ответило вовремя. Проверьте связь.';$('network-warning').hidden=false;$('network-warning').textContent='Нет интернета. После восстановления соединения работа продолжится.';$('storage-warning').hidden=false;$('storage-warning-text').textContent='Папка загрузки недоступна. Ожидаем подключения накопителя.';});await record('warnings');
  await page.locator('#job-history').locator('..').locator('.select-choice').click();await record('history-select');await close();
  await page.evaluate(()=>{$('grid-view').click();});await record('file-grid');
  await page.evaluate(()=>{$('home').click();connection(false);});await record('disconnected');
  // Exports normally open as standalone files. A data document preserves that
  // execution model without weakening the application's HTTP security headers.
  for(const [name,route] of [['journal','journal'],['changes-report','changes'],['html-report','report']]){const response=await context.request.get(url+'/qa/'+route);assert.equal(response.status(),200,route);await page.goto('data:text/html;base64,'+Buffer.from(await response.text()).toString('base64'));await record(name);if(name!=='html-report')assert.equal(await page.evaluate(()=>bytes(1024**2)),'1 МБ');}
  await context.close();
 }
 }
 }finally{await browser.close();fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({results,errors},null,2));}
 const issueCount=results.reduce((n,r)=>n+r.issues.length,0);if(issueCount||errors.length)process.exitCode=1;
 console.log(JSON.stringify({cases:results.length,issues:results.reduce((n,r)=>n+r.issues.length,0),pageErrors:errors,examples:results.filter(r=>r.issues.length).map(r=>({width:r.width,name:r.name,issues:r.issues.slice(0,12)}))},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
