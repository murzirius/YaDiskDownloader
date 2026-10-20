'use strict';

const workspace=document.querySelector('main');
const welcome=node('section',undefined,'welcome');
welcome.id='welcome';
welcome.append(node('p','Локальная копия Яндекс.Диска','eyebrow'),node('h1','Что хотите сделать?'),node('p','Файлы сохраняются в выбранную папку на этом устройстве. Вы управляете началом, паузой и продолжением.','welcome-note'));
const welcomeActions=node('div',undefined,'welcome-actions');
for(const [key,title,description] of [
  ['new','Скачать с Диска','Выбрать файлы и создать новую загрузку'],
  ['resume','Продолжить загрузку','Вернуться к сохранённому заданию'],
  ['verify','Проверить папку','Найти отсутствующие или повреждённые файлы без интернета'],
  ['history','История','Найти загрузку, открыть результаты или изменить очередь']
]){
  const button=node('button',undefined,'welcome-card');
  button.id='home-'+key;
  button.append(node('strong',title),node('span',description));
  button.onclick=()=>safely(async()=>{
    if(key==='resume'){
      const id=state.activeJob?.id||state.job;
      if(id){await openJob(id);return;}
      toolMode='history';toolsDialog.showModal();await renderTools();return;
    }
    if(key==='verify'||key==='history'){
      toolMode=key==='verify'?'verify':'history';toolsDialog.showModal();await renderTools();return;
    }
    openWorkspace('new');
  });
  welcomeActions.append(button);
}
welcome.append(welcomeActions);
workspace.before(welcome);
workspace.hidden=true;
const home=node('button','Главная','quiet');home.id='home';
home.onclick=()=>{workspace.hidden=true;welcome.hidden=false;home.setAttribute('aria-current','page');$('home-new').focus();};
$('open-help').before(home);

let wizardStep=1,destinationRevision=0,destinationTimer;
const settingsPanel=$('settings-panel'),steps=node('div',undefined,'setup-steps'),stepBoxes=[];
for(const [index,label] of ['Файлы','Папка на ПК','Качество и запуск'].entries()){
  const button=node('button',(index+1)+'. '+label);
  button.type='button';button.onclick=()=>safely(()=>advanceSetup(index+1));steps.append(button);
  const box=node('section');box.id='setup-step-'+(index+1);box.setAttribute('aria-label',label);stepBoxes.push(box);
}
settingsPanel.querySelector('h1').after(steps);
const reports=document.querySelector('input[name=report]').closest('fieldset');
const estimateButton=$('estimate');
const reportOptions=node('details',undefined,'advanced-settings');reportOptions.append(node('summary','Форматы отчёта'),reports);
const destinationStatus=node('p','Выберите папку — проверим доступ и свободное место.','destination-status');destinationStatus.id='destination-status';destinationStatus.role='status';
stepBoxes[0].append($('operation').closest('label'),$('operation-hint'),node('p','Выберите файлы или папки в списке слева. Можно отметить весь Диск.','hint'),$('whole-disk').closest('label'),$('file-types'));
stepBoxes[1].append($('destination').closest('label'),destinationStatus);
const confirmation=node('div',undefined,'setup-confirmation');confirmation.id='setup-confirmation';
stepBoxes[2].append($('missing-policy-field'),$('new-mode-field'),$('mode-hint'),$('create-zip').closest('label'),confirmation,$('settings-panel').querySelector('.advanced-settings'),reportOptions,$('exclusions').closest('details'),$('profile-list').closest('details'),$('estimate'));
const setupNavigation=node('div',undefined,'actions setup-navigation'),setupBack=node('button','Назад'),setupNext=node('button','Дальше','primary');
setupBack.onclick=()=>safely(()=>advanceSetup(wizardStep-1));setupNext.onclick=()=>safely(()=>advanceSetup(wizardStep+1));setupNavigation.append(setupBack,setupNext);
setupNavigation.append(estimateButton);
settingsPanel.append(...stepBoxes,setupNavigation);
$('estimate').textContent='1. Проверить список и объём';

function openWorkspace(mode){
  welcome.hidden=true;workspace.hidden=false;home.removeAttribute('aria-current');
  if(mode==='job'){showPanel('job');return;}
  state.restoreLimits=false;
  if(mode==='new')applyDownloadSettings({operation:'new',missing_policy:'originals',paths:[],whole_disk:false,destination:$('destination').value,mode:'previews',preview_size:'XXXL',workers:8,attempts:1,timeout:20,total_timeout:0,max_disk_percent:95,speed_limit_mib:0,create_zip:true,auto_pause:true,resume_parts:true,file_types:Object.keys(typeLabels),reports:['csv','json'],exclusions:[]});
  $('operation').value=mode==='missing'?'missing':'new';$('operation').onchange();
  wizardStep=mode==='missing'?2:1;showPanel('settings');renderSetup();
}
function renderSetup(){
  stepBoxes.forEach((box,index)=>{box.hidden=index!==wizardStep-1;});
  [...steps.children].forEach((button,index)=>button.setAttribute('aria-current',index===wizardStep-1?'step':'false'));
  setupBack.disabled=wizardStep===1;setupNext.hidden=wizardStep===3;
  $('estimate').hidden=wizardStep!==3;
  settingsPanel.querySelector('h1').textContent=$('operation').value==='missing'?'Дозагрузить папку':'Новая загрузка';
  const value=settings(),types=value.file_types.map(type=>typeLabels[type]).join(', ');
  confirmation.textContent=(value.whole_disk?'Весь Диск':selected.size?'Выбрано объектов: '+selected.size:'Выбор из прежней загрузки')+'\n'+types+'\nПапка: '+value.destination+'\n'+(value.operation==='missing'?(value.missing_policy==='any_result'?'Превью достаточно. Готовые копии пропустим.':'Готовые оригиналы пропустим. Для превью попробуем получить оригиналы.'):modes[value.mode])+ '\n'+(value.create_zip?'После загрузки создадим ZIP.':'ZIP выключен — файлы останутся в папке.');
}
async function inspectDestination(){
  const revision=++destinationRevision,path=$('destination').value;
  destinationStatus.textContent='Проверяем папку на ПК…';
  try{
    const result=await api('/api/destination',{path});
    if(revision!==destinationRevision)return null;
    destinationStatus.textContent=(result.exists?'Папка найдена.':'Папку создадим при запуске.')+' Запись доступна. Свободно '+bytes(result.free_bytes)+'. Диск заполнен на '+result.used_percent+'%.'+(result.previous_import?' Найдена предыдущая загрузка. Для неё можно выбрать «Дозагрузить недостающее».':'');
    return result;
  }catch(error){if(revision===destinationRevision)destinationStatus.textContent=error.message;throw error;}
}
async function advanceSetup(target){
  target=Math.max(1,Math.min(3,target));
  if(target>1&&!$('whole-disk').checked&&!selected.size&&$('operation').value!=='missing')throw Error('Выберите файлы или папки слева либо отметьте «Скачать весь Диск».');
  setupNext.disabled=true;
  try{if(target===3){const destination=await inspectDestination();if(!destination)return;}
    wizardStep=target;renderSetup();if(target===2)inspectDestination().catch(()=>{});
  }finally{setupNext.disabled=false;}
}
$('destination').addEventListener('input',()=>{destinationRevision++;clearTimeout(destinationTimer);destinationStatus.textContent='После ввода проверим папку на ПК.';destinationTimer=setTimeout(()=>inspectDestination().catch(()=>{}),500);});
$('choose-folder').addEventListener('click',()=>{destinationRevision++;});
document.addEventListener('change',event=>{if(settingsPanel.contains(event.target))renderSetup();});
renderSetup();

function applyDownloadSettings(value){
  for(const [id,key] of [['operation','operation'],['missing-policy','missing_policy'],['destination','destination'],['mode','mode'],['preview-size','preview_size'],['workers','workers'],['retries','attempts'],['timeout','timeout'],['total-timeout','total_timeout'],['max-disk-percent','max_disk_percent'],['speed-limit','speed_limit_mib']]){
    if(value[key]!==undefined)$(id).value=value[key];
  }
  for(const [id,key] of [['whole-disk','whole_disk'],['create-zip','create_zip'],['auto-pause','auto_pause'],['resume-parts','resume_parts']])if(value[key]!==undefined)$(id).checked=!!value[key];
  for(const input of document.querySelectorAll('input[name=report]'))input.checked=(value.reports||['json']).includes(input.value);
  for(const input of document.querySelectorAll('input[name=file-type]'))input.checked=(value.file_types||Object.keys(typeLabels)).includes(input.value);
  $('exclusions').value=(value.exclusions||[]).join('\n');selected.clear();for(const path of value.paths||[])selected.add(path);updateSelection();renderFiles();state.limitsTouched=true;$('operation').onchange();renderSetup();
}

const verificationSummary=node('div',undefined,'verification-summary');verificationSummary.id='preflight-summary';verificationSummary.role='status';verificationSummary.hidden=true;$('active-files').before(verificationSummary);
const resultActions=node('div',undefined,'actions');resultActions.id='result-actions';$('result-info').after(resultActions);
let resultActionsKey='';
function renderExperience(job){
  refreshJournal.disabled=job.live;
  if(['completed','completed_with_errors'].includes(job.phase)&&!job.error){
    const saved=job.completeness.originals+job.completeness.preview_only;
    const text='Сохранено '+saved+' из '+job.completeness.selected+'. Оригиналы: '+job.completeness.originals+' · Только превью: '+job.completeness.preview_only+' · Без результата: '+job.completeness.no_result+'.';
    if($('job-hint').textContent!==text)$('job-hint').textContent=text;
  }
  const check=job.verification;
  verificationSummary.hidden=!check||!['preparing','paused'].includes(job.phase);
  if(!verificationSummary.hidden){
    const text='Проверено на устройстве: '+check.checked+' / '+check.total+' · Осталось: '+Math.max(0,check.total-check.checked)+'\nОригиналы: '+check.originals+' · Превью: '+check.previews+' · Без копии: '+check.missing+'\nДо конца проверки: '+(job.phase==='paused'?'На паузе':check.eta_seconds==null?'Уточняем…':remainingTime(check.eta_seconds));
    if(verificationSummary.textContent!==text)verificationSummary.textContent=text;
  }
  const key=JSON.stringify([job.id,job.phase,job.error,job.live,job.network_waiting,job.errors,job.scan_errors,job.completeness?.preview_only,job.completeness?.no_result,job.counts?.partial,job.missing_previews,state.connected]);
  if(key===resultActionsKey)return;resultActionsKey=key;resultActions.replaceChildren();
  $('retry-job').hidden=true;
  if(job.live)return;
  if(job.errors||job.scan_errors||job.counts?.partial||job.missing_previews){
    const retry=toolButton(job.scan_errors?'Повторить обход и дозагрузку':job.errors?'Повторить ошибки':'Повторить отсутствующие копии',async()=>action(job.scan_errors?'retry':'retry_failed'));
    retry.disabled=!state.connected;resultActions.append(retry);
  }
  if(job.completeness?.preview_only||job.completeness?.no_result){
    resultActions.append(toolButton(job.completeness?.preview_only?'Получить оригиналы вместо превью':'Скачать файлы без копии',async()=>{
      applyDownloadSettings({...job.settings,operation:'missing',destination:job.root,missing_policy:'originals'});
      openWorkspace('missing');showPanel('settings');
      notice('Прежняя папка выбрана. Нажмите «Проверить список и объём», затем начните скачивание. Сначала загрузятся файлы без копий, затем оригиналы вместо превью.');
    }));
  }
  if(job.error?.includes('опись'))resultActions.append(toolButton('Сохранить опись ещё раз',async()=>{await api('/api/jobs/'+job.id+'/action',{action:'save_manifest'});await pollJob();}));
  if(job.errors||job.scan_errors||job.error){
    resultActions.append(toolButton('Показать проблемы',async()=>{$('errors-detail').open=true;$('errors-detail').scrollIntoView({behavior:'smooth',block:'nearest'});await loadErrors();}),toolButton('Собрать диагностику',async()=>{$('open-service').click();}));
  }
}
const refreshSize=toolButton('Обновить объём папки',async()=>{if(!state.job)return;await api('/api/jobs/'+state.job+'/refresh-usage',{});notice('Размер папки пересчитывается в фоне. Управление загрузкой доступно.');});$('job-details').append(refreshSize);
const refreshJournal=toolButton('Обновить HTML-журнал',async()=>{if(!state.job)return;await api('/api/jobs/'+state.job+'/action',{action:'refresh_journal'});notice('Обновляем HTML-журнал из сохранённых событий.');await pollJob();});$('job-details').append(refreshJournal);

const recoveryBanner=node('div',undefined,'recovery-banner');recoveryBanner.id='recovery-banner';recoveryBanner.role='status';recoveryBanner.hidden=true;$('auth-hint').after(recoveryBanner);
function renderRecovery(recovery){
  state.recovering=!!(recovery.busy||recovery.error);recoveryBanner.hidden=!state.recovering;
  if(state.recovering){const text=recovery.error||'Восстанавливаем результаты после сбоя на ПК: '+Math.min(recovery.processed,recovery.total)+' / '+recovery.total+'. Проверяем сохранённые файлы. Дождитесь окончания; историю уже можно смотреть.';if(recoveryBanner.dataset.message!==text){recoveryBanner.dataset.message=text;recoveryBanner.replaceChildren(node('span',text));if(recovery.error)recoveryBanner.append(toolButton('Повторить восстановление',async()=>{await api('/api/recovery',{});await refreshActivity();}));}}
  $('connect').disabled=state.recovering||state.authBusy;connection(state.connected);
}

const originalNotice=notice;
notice=function(message){
  originalNotice(message);
  if(!message)return;
  for(const box of document.querySelectorAll('.notice:not([hidden])')){
    if(/ключ|подключите.*Диск/i.test(message)){const action=toolButton('Подключить Диск',async()=>{$('auth-form').hidden=false;$('token').focus();$('auth-form').scrollIntoView({block:'center'});});box.append(action);}
  }
};
