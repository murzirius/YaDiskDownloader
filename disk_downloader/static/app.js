const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const selected = new Set();
const errorSelections = new Set();
const state = {closing:false,actionPending:false,activeJob:null,limitsJob:null,restoreLimits:true,limitsTouched:false,connected:false,authRevision:-1,authBusy:false,instance:null,path:'disk:/',offset:0,items:[],total:null,hasMore:false,view:'table',history:[],job:null,jobStatus:null,errorOffset:0,browseVersion:0,thumbVersion:0,urls:[],polling:false};
const phases = {new:'Новое задание',scanning:'Составляем список файлов',preparing:'Проверяем папку и готовим результат',ready:'Список готов — можно скачивать',downloading:'Скачивание',paused:'На паузе',stopping:'Сохраняем прогресс и останавливаем…',stopped:'Остановлено — можно продолжить',interrupted:'Работа прервалась — можно продолжить',packing:'Создаём ZIP',completed:'Завершено',completed_with_errors:'Завершено — есть замечания',failed:'Работа остановилась из-за ошибки'};
const modes = {previews:'Только превью',both:'Оригиналы и превью',fallback:'Оригинал, превью при неудаче'};
const typeLabels = {photos:'фото',videos:'видео',music:'музыка',files:'другие файлы'};
const outcomes = {downloaded_verified:'скачан и проверен',existing_verified:'уже имеется, проверен',unavailable:'недоступно',failed:'не удалось',not_requested:'не запрашивалось',not_needed:'не требуется'};
function showPanel(panel){$('settings-panel').hidden=panel!=='settings';$('job-panel').hidden=panel!=='job';$('settings-tab').setAttribute('aria-selected',String(panel==='settings'));$('job-tab').setAttribute('aria-selected',String(panel==='job'));}
function initializePanel(){
  $('estimate').textContent='1. Проверить список и объём';
  $('operation').onchange=()=>{
    const missing=$('operation').value==='missing';
    $('destination-label').textContent=missing?'Папка, куда скачивали раньше':'Куда сохранить на ПК';
    $('missing-policy-field').hidden=!missing;
    $('new-mode-field').hidden=missing;
    $('mode-hint').hidden=missing;
    $('operation-hint').textContent=missing?'Выберите прежнюю папку с результатами. Проверим, что уже сохранено, и скачаем недостающее в ту же папку.':'Выберите файлы или весь Диск и папку на ПК. Сначала покажем список и объём, затем вы сможете начать скачивание.';
  };
  $('operation').onchange();
}
function node(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function initializeHelp(){
  const dialog=$('help-dialog');
  if(!dialog)return;
  let step=0;
  const seenKey='disk-downloader-help-seen-v1';
  function render(){
    for(const section of dialog.querySelectorAll('.help-step'))section.hidden=Number(section.dataset.step)!==step;
    $('help-progress').textContent=`Шаг ${step+1} из 3`;
    $('help-back').disabled=step===0;
    $('help-next').textContent=step===2?'Начать работу':'Далее';
  }
  function open(){step=0;render();if(!dialog.open)dialog.showModal();}
  dialog.addEventListener('close',()=>{try{localStorage.setItem(seenKey,'1');}catch{}});
  $('open-help').onclick=open;
  $('close-help').onclick=()=>dialog.close();
  $('help-back').onclick=()=>{step--;render();};
  $('help-next').onclick=()=>{if(step===2)dialog.close();else{step++;render();}};
  $('oauth-client-id').oninput=()=>{
    const value=$('oauth-client-id').value.trim();
    const link=$('oauth-authorize');
    const valid=/^[a-f0-9]{32}$/i.test(value);
    link.setAttribute('aria-disabled',String(!valid));
    if(valid)link.href='https://oauth.yandex.ru/authorize?'+new URLSearchParams({response_type:'token',client_id:value});else link.removeAttribute('href');
  };
  let seen=false;
  try{seen=localStorage.getItem(seenKey)==='1';}catch{}
  if(!seen)open();
}
function bytes(n){if(n===null||n===undefined)return 'неизвестно';const units=['Б','КиБ','МиБ','ГиБ','ТиБ'];let i=0;while(n>=1024&&i<units.length-1){n/=1024;i++;}return `${n.toLocaleString('ru-RU',{maximumFractionDigits:i?1:0})} ${units[i]}`;}
function remainingTime(seconds){if(seconds===null||seconds===undefined)return 'Уточняется…';if(seconds===0)return '0 мин';if(seconds<60)return `≈ ${Math.ceil(seconds)} с`;const minutes=Math.ceil(seconds/60);if(minutes<60)return `≈ ${minutes} мин`;const hours=Math.floor(minutes/60),rest=minutes%60;if(hours<24)return `≈ ${hours} ч${rest?' '+rest+' мин':''}`;return `≈ ${Math.floor(hours/24)} д${hours%24?' '+hours%24+' ч':''}`;}
const dismissedNotices = new Set();
function notice(message){const boxes=[$('notice'),...document.querySelectorAll('.dialog-notice')];if(!message){for(const box of boxes)box.hidden=true;dismissedNotices.clear();return;}if(dismissedNotices.has(message))return;const dialog=document.querySelector('dialog[open]');if(dialog&&!dialog.querySelector('.dialog-notice')){const local=node('div',undefined,'notice dialog-notice');local.role='status';dialog.prepend(local);boxes.push(local);}for(const box of boxes){box.replaceChildren(node('span',message));const close=node('button','×','notice-close');close.setAttribute('aria-label','Закрыть уведомление');close.onclick=()=>{dismissedNotices.add(message);for(const holder of boxes)holder.hidden=true;};box.append(close);box.hidden=!!dialog&&box===$('notice');}}
function serviceWarning(message,session=false){if(!message&&(state.sessionExpired||$('service-warning').hidden))return;if(session)state.sessionExpired=true;$('service-warning').hidden=!message;$('service-warning-text').textContent=message||'';$('reload-page').hidden=!session;$('recheck-service').hidden=session;}
function requestPurpose(path,data){if(path==='/api/folder')return 'Выбор папки Windows';if(path==='/api/connect')return 'Проверка ключа доступа в Яндекс.Диске';if(path.startsWith('/api/browse'))return 'Получение списка файлов Яндекс.Диска';if(path.endsWith('/action'))return ({pause:'Постановка задания на паузу',stop:'Остановка задания',resume:'Продолжение загрузки',continue_scan:'Продолжение поиска файлов',start:'Запуск скачивания',retry:'Повторная загрузка',pack:'Создание ZIP'})[data?.action]||'Управление заданием';if(path==='/api/jobs')return 'Подготовка нового задания';if(path.startsWith('/api/jobs/')||path==='/api/status')return 'Обновление состояния задания';return 'Запрос к приложению на этом ПК';}
function acceptAuth(result){
  if(result.recovery&&typeof renderRecovery==='function')renderRecovery(result.recovery);
  if(result.instance_id&&state.instance&&state.instance!==result.instance_id){serviceWarning('Программа была перезапущена. Обновите страницу, затем подключите ключ доступа и продолжите задание. История и скачанные файлы сохранены.',true);return;}
  if(result.instance_id)state.instance=result.instance_id;
  if(typeof result.auth_revision!=='number'||result.auth_revision<state.authRevision)return;
  state.authRevision=result.auth_revision;connection(result.connected);
  if(result.auth_reason==='expired')$('auth-hint').firstChild.textContent='Яндекс.Диск отклонил прежний ключ. Получите новый ключ с правом чтения Диска, подключитесь и нажмите «Продолжить» у задания. ';
}
async function api(path,data){
  const controller=new AbortController(),purpose=requestPurpose(path,data);
  const timeout=path==='/api/connect'?25000:path.endsWith('/action')?15000:data===undefined?15000:60000;
  const timer=setTimeout(()=>controller.abort(),timeout);
  const options={headers:{'X-CSRF-Token':csrf},credentials:'same-origin',cache:'no-store',signal:controller.signal};
  if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(data);}
  try{
    let response;
    try{response=await fetch(path,options);}catch(error){
      const timed=error.name==='AbortError';
      const message=timed?`${purpose}: приложение на этом ПК (${location.host}) не прислало ответ за ${timeout/1000} с. Команда могла выполниться — нажмите «Проверить связь», чтобы увидеть состояние. Если окно программы закрыто, запустите её снова.`:`${purpose}: страница не может связаться с программой на этом ПК (${location.host}). Запустите YaDiskDownloader и нажмите «Проверить связь». Закрытие вкладки само по себе не останавливает скачивание.`;
      serviceWarning(message);throw new Error(message);
    }
    let result;
    try{result=await response.json();}catch{throw new Error(`${purpose}: приложение вернуло ответ, который не удалось прочитать (HTTP ${response.status}). Обновите страницу. Если повторяется — отправьте диагностику через «Сообщить об ошибке».`);}
    if(result.code==='session_expired')serviceWarning(result.error,true);
    else if(response.ok&&(path==='/api/status'||/^\/api\/jobs\/[^/]+$/.test(path)))serviceWarning('');
    acceptAuth(result);
    if(!response.ok)throw new Error((result.operation||purpose)+': '+(result.error||`Ошибка HTTP ${response.status}`)+(response.status>=500&&result.request_id?' Номер ошибки: '+result.request_id+'.':''));
    return result;
  }finally{if(timer!==null)clearTimeout(timer);}
}
async function safely(fn){try{notice('');await fn();}catch(error){notice(error.message);}}
function singleFlight(operation){let current=null;return(...args)=>{if(!current)current=Promise.resolve().then(()=>operation(...args)).finally(()=>{current=null;});return current;};}
function connection(connected){state.connected=connected;const key=JSON.stringify([connected,!!state.activeJob,state.hasMore,state.offset,!!state.recovering]);if(state.connectionKey===key)return;state.connectionKey=key;$('connection').textContent=connected?'Диск подключён':'Не подключён';$('connection').classList.toggle('online',connected);$('auth-form').hidden=connected;$('disconnect').hidden=!connected;$('estimate').disabled=!connected||!!state.activeJob||!!state.recovering;$('refresh').disabled=!connected;$('next-page').disabled=!connected||!state.hasMore;$('prev-page').disabled=!connected||state.offset===0;}
function breadcrumb(){const holder=$('breadcrumbs');holder.replaceChildren();const parts=state.path==='disk:/'?[]:state.path.slice(6).split('/');let path='disk:/';const root=node('button','Диск');root.onclick=()=>safely(()=>browse('disk:/',0,true));holder.append(root);for(const part of parts){holder.append(node('span','/','muted'));path=path.replace(/\/$/,'')+'/'+part;const target=path;const button=node('button',part);button.onclick=()=>safely(()=>browse(target,0,true));holder.append(button);}$('up').disabled=parts.length===0||!state.connected;$('back').disabled=!state.history.length||!state.connected;}
async function browse(path=state.path,offset=0,remember=false){
  if(!state.connected)return;
  const version=++state.browseVersion;
  $('file-list').replaceChildren(node('div','Загрузка списка…','empty'));
  try{
    const result=await api('/api/browse?'+new URLSearchParams({path,offset,sort:$('sort').value}));
    if(version!==state.browseVersion)return;
    if(remember&&path!==state.path)state.history.push({path:state.path,offset:state.offset});
    Object.assign(state,{path:result.path,offset:result.offset,items:result.items,total:result.total,hasMore:result.has_more});
    breadcrumb();renderFiles();$('page-info').textContent=`${state.items.length?state.offset+1:0}–${state.offset+state.items.length}${state.total===null?'':' из '+state.total} · выбор не ограничен страницей`;
    $('prev-page').disabled=state.offset===0;$('next-page').disabled=!state.hasMore;
  }catch(error){
    if(version===state.browseVersion){const message=node('div',error.message,'empty'),retry=node('button','Повторить получение списка');retry.onclick=()=>safely(()=>browse(path,offset,remember));message.append(retry);$('file-list').replaceChildren(message);}
    throw error;
  }
}
function updateSelection(){const whole=$('whole-disk').checked;$('selection-count').textContent=whole?'Выбран весь Диск':`Выбрано: ${selected.size}`;const items=visibleItems();$('select-page').disabled=whole||!items.length;$('select-page').checked=items.length>0&&items.every(item=>selected.has(item.path));$('select-page').indeterminate=items.some(item=>selected.has(item.path))&&!$('select-page').checked;const holder=$('selected-paths');holder.replaceChildren();if(whole){holder.textContent='disk:/ · весь Диск';return;}if(!selected.size){holder.textContent='Выбор пуст';return;}for(const path of selected){const row=node('div',path);holder.append(row);}}
function visibleItems(){const query=$('filter').value.toLocaleLowerCase('ru-RU');return state.items.filter(item=>(item.name||'').toLocaleLowerCase('ru-RU').includes(query));}
function checkbox(item){const box=node('input');box.type='checkbox';box.checked=selected.has(item.path);box.disabled=$('whole-disk').checked;box.setAttribute('aria-label','Выбрать '+item.name);box.onchange=()=>{if(box.checked)selected.add(item.path);else selected.delete(item.path);renderFiles();};return box;}
function icon(item){const div=node('div',item.type==='dir'?'ПАПКА':(item.name.split('.').pop()||'ФАЙЛ').slice(0,6).toUpperCase(),'file-icon'+(item.type==='dir'?' folder':''));if(item.has_preview&&item.type==='file')div.dataset.previewPath=item.path;return div;}
function filename(item){const button=node('button',item.name,'name-button');button.title=item.path;button.onclick=()=>{if(item.type==='dir')safely(()=>browse(item.path,0,true));else{if(selected.has(item.path))selected.delete(item.path);else selected.add(item.path);renderFiles();}};return button;}
function renderFiles(){state.thumbVersion++;state.thumbnailObserver?.disconnect();for(const controller of state.thumbnailControllers||[])controller.abort();state.thumbnailControllers=new Set();for(const url of state.urls)URL.revokeObjectURL(url);state.urls=[];const holder=$('file-list');holder.replaceChildren();const items=visibleItems();if(!items.length){holder.append(node('div',state.items.length?'По фильтру ничего не найдено':'Папка пуста','empty'));updateSelection();return;}if(state.view==='table'){const table=node('table',undefined,'files-table');const thead=node('thead');const header=node('tr');for(const text of ['','Имя','Размер','Изменён'])header.append(node('th',text));thead.append(header);table.append(thead);const body=node('tbody');for(const item of items){const row=node('tr');row.classList.toggle('selected',selected.has(item.path));const checkCell=node('td');checkCell.append(checkbox(item));const nameCell=node('td');const wrapper=node('div',undefined,'file-name');wrapper.append(icon(item),filename(item),excludeButton(item));nameCell.append(wrapper);row.append(checkCell,nameCell,node('td',item.type==='dir'?'—':bytes(item.size),'muted'),node('td',item.modified?new Date(item.modified).toLocaleDateString('ru-RU'):'—','muted'));body.append(row);}table.append(body);holder.append(table);}else{const grid=node('div',undefined,'files-grid');for(const item of items){const tile=node('div',undefined,'file-tile');tile.classList.toggle('selected',selected.has(item.path));const top=node('div',undefined,'tile-top');top.append(checkbox(item),node('span',item.type==='dir'?'Папка':bytes(item.size),'muted'),excludeButton(item));tile.append(top,icon(item),filename(item));grid.append(tile);}holder.append(grid);}updateSelection();loadThumbnails();}
function loadThumbnails(){const version=state.thumbVersion;const queue=[];let running=0;const observer=new IntersectionObserver(entries=>{for(const entry of entries){if(entry.isIntersecting){observer.unobserve(entry.target);queue.push(entry.target);}}pump();},{root:$('file-list'),rootMargin:'100px'});state.thumbnailObserver=observer;for(const el of $('file-list').querySelectorAll('[data-preview-path]'))observer.observe(el);async function pump(){if(version!==state.thumbVersion){observer.disconnect();return;}while(running<2&&queue.length){const el=queue.shift();running++;const controller=new AbortController();state.thumbnailControllers.add(controller);(async()=>{try{const response=await fetch('/api/thumbnail?'+new URLSearchParams({path:el.dataset.previewPath,size:state.view==='grid'?'M':'S'}),{headers:{'X-CSRF-Token':csrf},credentials:'same-origin',cache:'no-store',signal:controller.signal});if(!response.ok)return;const blob=await response.blob();if(version!==state.thumbVersion||!state.connected)return;const url=URL.createObjectURL(blob);state.urls.push(url);const img=node('img');img.src=url;img.alt='';el.replaceChildren(img);}catch{}finally{state.thumbnailControllers.delete(controller);running--;pump();}})();}}}
function advancedSettings(){return {create_zip:$('create-zip').checked,adaptive_workers:false,resume_parts:$('resume-parts').checked,auto_pause:$('auto-pause').checked,max_disk_percent:Number($('max-disk-percent').value),speed_limit_mib:Number($('speed-limit').value),exclusions:$('exclusions').value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean)};}
function settings(){return {operation:$('operation').value,missing_policy:$('missing-policy').value,paths:[...selected],whole_disk:$('whole-disk').checked,destination:$('destination').value,mode:$('mode').value,file_types:$('file-types')?[...document.querySelectorAll('input[name=file-type]:checked')].map(el=>el.value):['photos','videos','music','files'],preview_size:$('preview-size').value,workers:Number($('workers').value),attempts:Number($('retries').value),timeout:Number($('timeout').value),total_timeout:Number($('total-timeout').value),reports:[...document.querySelectorAll('input[name=report]:checked')].map(el=>el.value),...advancedSettings()};}
function selectJob(id){state.job=id;state.jobStatus=null;state.restoreLimits=true;state.limitsTouched=false;state.limitsJob=null;state.resumePolicyJob=null;state.errorOffset=0;state.errorRenderKey=null;state.renderKey=null;errorSelections.clear();}
async function openJob(id){selectJob(id);if(typeof openWorkspace==='function')openWorkspace('job');showPanel('job');await pollJob();}
async function refreshJobs(){state.renderKey=null;const result=await api('/api/status');renderRunning(result.active_job);const history=$('job-history');history.replaceChildren();if(!result.jobs.length){history.append(new Option('Нет заданий',''));$('job-content').hidden=true;return;}for(const job of result.jobs)history.append(new Option(`${job.title||String(job.root||'').split(/[\\/]/).pop()||job.id.slice(0,8)} · ${phases[job.phase]||job.phase}`,job.id));if(state.job&&!result.jobs.some(job=>job.id===state.job)){try{const previous=await api('/api/jobs/'+state.job);history.append(new Option((previous.title||previous.id.slice(0,8))+' · '+(phases[previous.phase]||previous.phase),previous.id));}catch{selectJob(null);}}if(!state.job)selectJob(result.active_job?.id||result.jobs[0].id);history.value=state.job;showPanel('job');await pollJob();}
function counter(holder,label,value,wide=false){const div=node('div',undefined,wide?'storage-counter':undefined);div.append(node('span',label),node('strong',String(value)));holder.append(div);}
function renderJobCounters(job){
  if(job.statistics_pending){$('counters').replaceChildren(node('p',job.statistics_error||'Считаем сохранённые файлы в фоне…','hint'));$('extra-counters').replaceChildren();return;}
  const main=$('counters');
  const extra=$('extra-counters');
  main.replaceChildren();
  extra.replaceChildren();
  for(const [label,value,wide] of [
    [!job.scan_complete?'Найдено файлов':'Обработано',!job.scan_complete?String(job.files):`${job.processed} / ${job.files}`],
    ['Скорость',bytes(job.speed)+'/с'],
    ['На устройстве',job.folder_bytes_error?'Папка недоступна':bytes(job.folder_bytes??job.local_bytes)+(job.folder_bytes_scanning?' · уточняется':''),true],
    ['Свободно на диске',bytes(job.free_bytes),true],
    ['Полные оригиналы',job.completeness.originals],
    ['Только превью',job.completeness.preview_only],
    ['Без результата',job.completeness.no_result],
    ['Ошибки',job.errors+job.scan_errors]
  ])counter(main,label,value,wide);
  if(job.previous_results)counter(main,'Уже есть результат',job.previous_results,true);
  if(['downloading','packing','paused'].includes(job.phase))counter(main,job.eta_phase==='packing'?'До конца ZIP':'До конца загрузки',job.phase==='paused'?'Пауза':job.eta_seconds==null?(job.eta_message||'Уточняется…'):remainingTime(job.eta_seconds),true);
  for(const [label,value] of [
    ['Потоки',`${job.workers_status.current} / ${job.workers_status.maximum}`],['Недокачанные части',bytes(job.partial_bytes)],
    ['Превью всего',job.previews],['Замены оригинала',job.replacements],['Папки',job.folders],['Уже имеются',job.existing],['Исключены по типу',job.counts.filtered||0],
    ['Нет превью',job.missing_previews],['Отменено',job.cancelled],
    ['Передано',bytes(job.downloaded)],['Сохранено',bytes(job.saved_bytes)]
  ])counter(extra,label,value);
}
function renderEstimate(job){
  if(job.statistics_pending){$('estimate-info').hidden=true;return;}
  const estimate=job.estimate;
  const holder=$('estimate-info');
  const details=$('estimate-details');
  const conditional=estimate.conditional_preview_sizes||0;
  const unknown=estimate.unknown_sizes||0;
  const hasUnknown=unknown>0||conditional>0;
  holder.replaceChildren(node('strong',job.scan_complete?'Оценка объёма':'Найдено при обходе'));
  const sizes=node('div',undefined,'estimate-values');
  counter(sizes,'Данные',!estimate.known_bytes&&hasUnknown?'Размер неизвестен':bytes(estimate.known_bytes)+(hasUnknown?' + неизвестно':''));
  if(job.settings.create_zip!==false)counter(sizes,'ZIP ≈',!estimate.zip_basis_bytes&&hasUnknown?'Пока неизвестен':bytes(estimate.zip_basis_bytes)+(hasUnknown?' + неизвестно':'')+' + служебные данные');
  else counter(sizes,'ZIP','Выключен');
  holder.append(sizes);
  if(hasUnknown)holder.append(node('div',`Неизвестных размеров: ${unknown}${conditional?' + до '+conditional+' превью при ошибке':''}`,'estimate-note'));
  holder.append(node('div','Предварительно · без сжатия','estimate-note'));
  holder.hidden=!['new','scanning','ready'].includes(job.phase)&&!(job.phase==='paused'&&!job.scan_complete);
  details.replaceChildren();
  details.append(node('p','Найденные оригиналы: '+bytes(estimate.original_bytes)+'. Неизвестных размеров: '+estimate.original_unknown+'.'));
  details.append(node('p',(job.settings.create_zip!==false?'Запас для ZIP и временных файлов: ':'Запас для временных файлов: ')+bytes(estimate.extra_temporary_bytes)+(hasUnknown?' + неизвестный объём':'')+' сверх результата.'));
  details.append(node('p','Размеры превью заранее неизвестны. Приоритет оригинала оценивается по оригиналам; итог зависит от успешности загрузки.'));
  details.append(node('p','Показатель «На устройстве» учитывает всё содержимое папки, включая прежние загрузки, отчёты, ZIP и недокачанные части. Обновляется примерно каждые 10 секунд; ссылки на внешние папки не обходятся.'));
}
async function pollJob(){
  if(!state.job||state.polling||state.closing)return;
  state.polling=true;
  try{
    const id=state.job;
    const job=await api('/api/jobs/'+id);
    if(id!==state.job)return;
    state.jobStatus=job;
    if(state.resumePolicyJob!==job.id){state.resumePolicyJob=job.id;$('resume-policy').value=job.settings.missing_policy||'originals';}
    if(state.restoreLimits&&state.limitsJob!==job.id){
      state.limitsJob=job.id;
      $('workers').value=job.settings.workers;
      $('retries').value=job.settings.attempts??job.settings.retries+1;
      $('timeout').value=job.settings.timeout;
      $('total-timeout').value=job.settings.total_timeout??job.settings.timeout;
      for(const [id,key] of [['create-zip','create_zip'],['resume-parts','resume_parts'],['auto-pause','auto_pause']])$(id).checked=job.settings[key]!==false;
      $('max-disk-percent').value=job.settings.max_disk_percent??95;
      $('speed-limit').value=job.settings.speed_limit_mib??0;
      $('resume-policy').value=job.settings.missing_policy||'originals';
      $('exclusions').value=(job.settings.exclusions??[]).join('\n');
      for(const input of document.querySelectorAll('input[name=file-type]'))input.checked=(job.settings.file_types||Object.keys(typeLabels)).includes(input.value);
    }

    const renderKey=JSON.stringify([job,state.connected,state.actionPending,state.stopPending,state.closing,state.resumePolicyJob,state.limitsJob,errorSelections.size,state.errorOffset,$('errors-detail').open]);
    if(state.renderKey===renderKey){if($('errors-detail').open)await loadErrors();return;}
    const option=$('job-history').selectedOptions[0];
    if(option&&option.value===job.id)option.textContent=(job.title||String(job.root||'').split(/[\\/]/).pop()||job.id.slice(0,8))+' · '+(phases[job.phase]||job.phase);
    $('job-content').hidden=false;
    $('phase').textContent=phases[job.phase]||job.phase;
    const packing=job.phase==='packing'||(job.phase==='paused'&&job.zip_total&&job.zip_done<job.zip_total);
    const percent=packing?(job.zip_total?Math.min(100,job.zip_done*100/job.zip_total):0):job.percent;
    if(job.statistics_pending||job.phase==='scanning'||!job.scan_complete&&!packing){
      $('progress').removeAttribute('value');
      $('percentage').textContent=job.statistics_pending?'Получаем статистику…':'Ищем файлы…';
    }else{
      $('progress').value=percent??0;
      $('percentage').textContent=(percent??0).toLocaleString('ru-RU',{maximumFractionDigits:1})+'%'+(packing?' ZIP':' обработано');
    }
    renderJobCounters(job);
    const active=$('active-files');
    active.replaceChildren();
    for(const file of job.active){
      const div=node('div',undefined,'active-item');
      div.append(node('span',file.action+' · '+file.path));
      if(file.size!==null&&file.size!==undefined){
        const progress=node('progress');
        progress.max=file.size||1;
        progress.value=file.done;
        div.append(progress,node('span',file.unit==='objects'?file.done.toLocaleString('ru-RU')+' / '+file.size.toLocaleString('ru-RU')+' файлов':bytes(file.done)+' / '+bytes(file.size),'muted'));
      }else div.append(node('div',file.unit==='objects'?file.done.toLocaleString('ru-RU')+' объектов':bytes(file.done),'muted'));
      active.append(div);
    }
    renderEstimate(job);
    $('job-settings').textContent=`${modes[job.settings.mode]} · ${(job.settings.file_types||Object.keys(typeLabels)).map(value=>typeLabels[value]).join(', ')} · ${job.settings.preview_size} · ${job.settings.workers} потока · ${job.settings.reports.join(', ')} · ${job.settings.whole_disk?'весь Диск':job.settings.paths.length+' исходных путей'}`;
    const live=job.live;
    const scanOnly=!job.scan_complete||job.settings.scan_only_pending;
    $('resume-job').textContent=scanOnly?'Продолжить обход':'Продолжить загрузку';
    $('stop-job').textContent=job.phase==='stopping'?'Останавливаем…':scanOnly?'Остановить обход':job.phase==='packing'?'Остановить ZIP':'Остановить загрузку';
    $('job-hint').textContent=state.closing?'Завершаем приложение…':job.phase==='stopping'?'Отменяем запросы и сохраняем прогресс…':job.phase==='ready'?(live?'Завершаем подготовку следующего этапа.':'Список готов. Нажмите «Начать скачивание».'):job.phase==='scanning'?'Составляем список файлов. Можно остановить и продолжить позже.':job.phase==='preparing'?'Проверяем локальные файлы и подготавливаем результаты.':job.phase==='paused'?'Работа на паузе. Для продолжения нажмите кнопку выше.':['stopped','interrupted'].includes(job.phase)?(scanOnly?'Обход остановлен. «Продолжить обход» вернёт вас к оценке.':(state.connected?'Прогресс сохранён. В блоке «Как продолжить загрузку» выберите, достаточно ли превью, и нажмите «Продолжить загрузку».':'Прогресс сохранён. Нажмите «Подключить Диск, чтобы продолжить», вставьте ключ доступа, затем продолжите загрузку.')):'Закрытие вкладки не останавливает работу. Для выхода используйте «Выход». ';
    if(live)renderRunning({id:job.id,phase:job.phase,scan_complete:job.scan_complete});
    const hasIssues=job.errors||job.scan_errors;
    const hasRetry=hasIssues||job.replacements||job.counts.partial||job.completeness.preview_only;
    $('start-job').textContent='2. Начать скачивание';
    $('start-job').hidden=job.phase!=='ready'||live;
    $('start-job').disabled=state.actionPending||live||!state.connected||job.statistics_pending;
    $('pause-job').disabled=state.actionPending||job.phase==='stopping';
    $('pause-job').hidden=!job.can_pause;
    $('resume-job').hidden=!job.can_resume||!['paused','stopped','interrupted','failed'].includes(job.phase);
    $('resume-job').disabled=state.actionPending||(!state.connected&&!packing);
    $('connect-for-job').hidden=state.connected||!job.can_resume||!['ready','paused','stopped','interrupted','failed'].includes(job.phase);
    $('resume-options').hidden=!job.can_resume||!['ready','paused','stopped','interrupted','failed'].includes(job.phase);
    $('resume-policy').disabled=live;
    $('resume-options-hint').textContent=live?'Чтобы изменить учёт превью, остановите задание. Затем выберите вариант и продолжите — прогресс сохранится.':'Проверим папку перед продолжением. Сначала скачаем файлы без копий. Готовые оригиналы повторно не скачиваем.';
    $('stop-job').hidden=!live;
    $('stop-job').disabled=state.stopPending||job.phase==='stopping';
    $('retry-job').hidden=!hasRetry||live||job.phase==='ready';
    $('retry-job').textContent=hasIssues?'Повторить файлы с ошибками':'Попробовать скачать оригиналы';
    $('retry-job').disabled=live||!state.connected;
    $('pack-job').hidden=live||job.phase==='ready';
    $('pack-job').disabled=!job.scan_complete&&!job.processed;
    $('pack-job').textContent=job.zip_path?'Пересобрать ZIP':'Собрать ZIP';
    if(typeof renderDismissibleNetwork==='function')renderDismissibleNetwork(job);
    else{$('network-warning').hidden=!job.network_waiting;$('network-warning').textContent=job.network_warning||'';}
    const completenessMessage=!job.statistics_pending&&['completed','completed_with_errors'].includes(job.phase)?(job.scan_errors?'Обход завершился с ошибками: часть папок могла остаться непроверенной. ':!job.completeness.no_result&&!job.completeness.preview_only?'Все выбранные файлы сохранены в оригинале. ':'')+(job.completeness.preview_only?'Только превью: '+job.completeness.preview_only+'. Это уменьшенные копии. ':'')+(job.completeness.no_result?'Без результата: '+job.completeness.no_result+'. ':''):'';
    const resultMessage=job.pause_reason||job.error||job.statistics_error||completenessMessage||(job.phase==='completed_with_errors'?'Результат неполный.':job.phase==='stopped'?'Остановлено. Незавершённых файлов: '+job.cancelled+'.':'');
    if(typeof renderDismissibleResult==='function')renderDismissibleResult(job,resultMessage);else $('result-info').textContent=resultMessage;
    if(job.zip_path){
      $('download-zip').hidden=false;
      $('zip-saved').hidden=false;
      $('zip-saved').textContent='ZIP сохранён: '+job.zip_path+' · '+bytes(job.zip_bytes);
      $('download-zip').textContent='Открыть папку с ZIP';
    }else{$('download-zip').hidden=true;$('zip-saved').hidden=true;}
    $('output-path').textContent=job.root+(job.zip_path?'\nZIP: '+job.zip_path:'');
    $('open-journal').hidden=!job.settings.journal_local;
    $('journal-info').textContent=job.journal_error||(job.settings.journal_local?'Журнал: '+job.settings.journal_local+' · каждые 5 минут и при завершении':'');
    $('errors-title').textContent=`Ошибки (${job.errors+job.scan_errors})`;
    $('errors-detail').hidden=!hasIssues;
    $('retry-selected').hidden=!errorSelections.size;
    $('retry-selected').textContent='Повторить выбранные ('+errorSelections.size+')';
    $('retry-selected').disabled=live||!state.connected;
    state.renderKey=renderKey;
    if($('errors-detail').open)await loadErrors();
    if(typeof renderExperience==='function')renderExperience(job);
  }finally{
    state.polling=false;
  }
}
async function loadErrors(){if(!state.job)return;const jobId=state.job,offset=state.errorOffset;const result=await api(`/api/jobs/${jobId}/errors?offset=${offset}`);if(jobId!==state.job||offset!==state.errorOffset)return;const holder=$('errors-list'),signature=JSON.stringify([jobId,offset,result,state.connected,!!state.jobStatus?.live,[...errorSelections]]);if(state.errorRenderKey===signature)return;state.errorRenderKey=signature;const focused=holder.contains(document.activeElement)?document.activeElement.dataset.focusKey:null;holder.replaceChildren();if(!result.items.length)holder.append(node('p','Нет записей на этой странице','muted'));for(const item of result.items){const div=node('div',undefined,'error-item'),label=node('label',undefined,'error-selection'),check=node('input');check.type='checkbox';check.dataset.focusKey='select|'+item.path;check.checked=errorSelections.has(item.path);check.disabled=item.status==='scan_error';check.setAttribute('aria-label','Выбрать '+item.path);check.onchange=()=>{if(check.checked)errorSelections.add(item.path);else errorSelections.delete(item.path);$('retry-selected').hidden=!errorSelections.size;$('retry-selected').textContent='Повторить выбранные ('+errorSelections.size+')';};label.append(check,node('strong',item.path));div.append(label,node('p',item.error),node('span',`Оригинал: ${outcomes[item.original_status]||item.original_status}. Превью: ${outcomes[item.preview_status]||item.preview_status}.${item.fallback?' Переход к превью после неудачи оригинала.':''}`));if(item.status!=='scan_error'){const retry=node('button','Повторить этот файл');retry.dataset.focusKey='retry|'+item.path;retry.disabled=state.jobStatus?.live||!state.connected;retry.onclick=()=>safely(()=>retrySelected([item.path]));div.append(retry);}holder.append(div);}$('errors-prev').disabled=state.errorOffset===0;$('errors-next').disabled=!result.has_more;if(focused)[...holder.querySelectorAll('[data-focus-key]')].find(el=>el.dataset.focusKey===focused)?.focus({preventScroll:true});}
function runtimeLimits(){const result=baseRuntimeLimits();result.missing_policy=$('resume-policy').value;return result;}
function baseRuntimeLimits(){const previous=state.jobStatus?.settings;if(!state.limitsTouched&&state.limitsJob!==state.job&&previous)return Object.fromEntries(['workers','attempts','timeout','total_timeout','create_zip','adaptive_workers','resume_parts','auto_pause','max_disk_percent','speed_limit_mib'].filter(key=>previous[key]!==undefined).map(key=>[key,previous[key]]));return {workers:Number($('workers').value),attempts:Number($('retries').value),timeout:Number($('timeout').value),total_timeout:Number($('total-timeout').value),...advancedSettings()};}
async function action(verb){
  if(!state.job||(verb==='stop'?state.stopPending:state.actionPending))return;
  if(verb==='stop')state.stopPending=true;
  state.actionCount=(state.actionCount||0)+1;state.actionPending=true;
  for(const id of ['start-job','pause-job','resume-job'])$(id).disabled=true;
  if(verb==='stop'){$('phase').textContent='Останавливаем…';$('job-hint').textContent='Отменяем запросы и сохраняем прогресс…';}
  try{
    const scanOnly=state.jobStatus&&(!state.jobStatus.scan_complete||state.jobStatus.settings.scan_only_pending);
    const command=verb==='resume'&&scanOnly?'continue_scan':verb;
    await api('/api/jobs/'+state.job+'/action',{action:command,limits:['start','resume','retry'].includes(verb)?{...runtimeLimits(),...(verb==='retry'&&!state.jobStatus?.errors&&!state.jobStatus?.scan_errors?{missing_policy:'originals'}:{})}:undefined});
  }finally{if(verb==='stop')state.stopPending=false;state.actionCount--;state.actionPending=state.actionCount>0;await pollJob();}
}
function renderRunning(job){
  state.activeJob=job||null;$('estimate').disabled=!state.connected||!!job||state.closing;$('running-bar').hidden=!job||state.closing;
  if(!job)return;
  $('running-label').textContent=phases[job.phase]||'Задание выполняется';
  const stopping=job.phase==='stopping';
  $('stop-active').textContent=stopping?'Останавливаем…':!job.scan_complete?'Остановить обход':'Остановить';
  $('stop-active').disabled=state.stopPending||stopping||['completed','completed_with_errors'].includes(job.phase);
}
async function refreshActivity(){if(state.closing||state.activityPending)return;state.activityPending=true;try{const result=await api('/api/status');renderRunning(result.active_job);}finally{state.activityPending=false;}}
async function exitApplication(){
  if(state.closing)return;
  if(typeof askConfirmation==='function'&&!await askConfirmation('Завершить приложение? Текущая работа остановится, прогресс сохранится.'))return;
  await api('/api/exit',{});state.closing=true;renderRunning(null);
  $('quit-app').disabled=true;$('quit-app').textContent='Завершаем…';
  document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());
  notice('Приложение завершается. Прогресс сохранён; эту вкладку можно закрыть.');
}
$('show-active').onclick=()=>safely(async()=>{if(!state.activeJob)return;await openJob(state.activeJob.id);});
$('stop-active').onclick=()=>safely(async()=>{if(!state.activeJob||state.stopPending)return;const id=state.activeJob.id;$('stop-active').disabled=true;state.stopPending=true;try{await api('/api/jobs/'+id+'/action',{action:'stop'});}finally{state.stopPending=false;await refreshActivity();await pollJob();}});
$('quit-app').onclick=()=>safely(exitApplication);
setInterval(()=>{if(!state.closing&&!document.hidden)refreshActivity().catch(()=>{});},3000);

async function retrySelected(paths){await api('/api/jobs/'+state.job+'/action',{action:'retry_selected',paths,limits:runtimeLimits()});errorSelections.clear();await pollJob();}
$('auth-form').onsubmit=event=>{event.preventDefault();if(state.authBusy)return;safely(async()=>{state.authBusy=true;$('connect').disabled=true;$('connect').textContent='Проверяем ключ…';try{await api('/api/connect',{token:$('token').value.trim()});$('token').value='';const results=await Promise.allSettled([refreshJobs(),browse('disk:/')]);const failed=results.filter(item=>item.status==='rejected').map(item=>item.reason.message);notice('Диск подключён.'+(failed.length?' '+failed.join(' '):state.job?' Откройте задание и нажмите «Продолжить», чтобы вернуться к загрузке.':''));}finally{state.authBusy=false;$('connect').disabled=false;$('connect').textContent='Подключить';}});};
$('disconnect').onclick=()=>safely(async()=>{await api('/api/disconnect',{});state.browseVersion++;state.thumbVersion++;for(const url of state.urls)URL.revokeObjectURL(url);state.urls=[];state.items=[];$('file-list').replaceChildren(node('div','Диск отключён. Для продолжения вставьте ключ доступа вверху страницы. Скачанные файлы и история сохранены.','empty'));await pollJob();});
$('refresh').onclick=()=>safely(()=>browse(state.path,state.offset));
$('sort').onchange=()=>safely(()=>browse());
$('filter').oninput=renderFiles;
$('back').onclick=()=>safely(async()=>{const previous=state.history.pop();if(previous)await browse(previous.path,previous.offset);});
$('up').onclick=()=>safely(()=>browse(state.path.slice(6).includes('/')?state.path.slice(0,state.path.lastIndexOf('/')):'disk:/',0,true));
$('prev-page').onclick=()=>safely(()=>browse(state.path,Math.max(0,state.offset-100)));
$('next-page').onclick=()=>safely(()=>browse(state.path,state.offset+100));
$('table-view').onclick=()=>{state.view='table';$('table-view').setAttribute('aria-pressed','true');$('grid-view').setAttribute('aria-pressed','false');renderFiles();};
$('grid-view').onclick=()=>{state.view='grid';$('table-view').setAttribute('aria-pressed','false');$('grid-view').setAttribute('aria-pressed','true');renderFiles();};
$('thumb-size').oninput=()=>document.documentElement.style.setProperty('--thumb',$('thumb-size').value+'px');
$('select-page').onchange=()=>{for(const item of visibleItems()){if($('select-page').checked)selected.add(item.path);else selected.delete(item.path);}renderFiles();};
$('clear-selection').onclick=()=>{selected.clear();renderFiles();};
$('whole-disk').onchange=renderFiles;
$('choose-folder').onclick=()=>safely(async()=>{$('choose-folder').disabled=true;try{const path=await chooseFolder($('destination').value);if(path)$('destination').value=path;}finally{$('choose-folder').disabled=false;}});
$('estimate').onclick=()=>safely(async()=>{$('estimate').disabled=true;try{const result=await api('/api/jobs',settings());state.job=result.id;state.restoreLimits=true;state.limitsTouched=false;state.errorOffset=0;errorSelections.clear();await refreshJobs();}finally{$('estimate').disabled=!state.connected||!!state.activeJob;}});
$('refresh-jobs').onclick=()=>safely(refreshJobs);
$('job-history').onchange=()=>safely(()=>openJob($('job-history').value));
for(const id of ['workers','retries','timeout','total-timeout'])$(id).oninput=()=>{state.limitsTouched=true;};
for(const [id,verb] of [['start-job','start'],['pause-job','pause'],['resume-job','resume'],['stop-job','stop'],['retry-job','retry'],['pack-job','pack']])$(id).onclick=()=>safely(()=>action(verb));
$('errors-detail').ontoggle=()=>{if($('errors-detail').open)safely(loadErrors);};
$('errors-prev').onclick=()=>{state.errorOffset=Math.max(0,state.errorOffset-100);safely(loadErrors);};
$('errors-next').onclick=()=>{state.errorOffset+=100;safely(loadErrors);};
$('download-zip').onclick=()=>safely(()=>api('/api/jobs/'+state.job+'/open-output',{}));
$('open-journal').onclick=()=>safely(()=>api('/api/jobs/'+state.job+'/open-journal',{}));
$('retry-selected').onclick=()=>safely(()=>retrySelected([...errorSelections]));
$('settings-tab').onclick=()=>showPanel('settings');
$('job-tab').onclick=()=>showPanel('job');
initializePanel();initializeHelp();connection(false);breadcrumb();updateSelection();safely(async()=>{try{await refreshJobs();}finally{if(state.connected)await browse();}});
setInterval(()=>{if(!state.closing&&!document.hidden&&state.job&&!state.polling&&Date.now()>=(state.nextPoll||0))pollJob().then(()=>{state.pollFailures=0;state.nextPoll=0;}).catch(error=>{state.pollFailures=Math.min(4,(state.pollFailures||0)+1);state.nextPoll=Date.now()+Math.min(15000,1000*2**state.pollFailures);if(!state.closing)serviceWarning(error.message);});},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!state.closing){state.nextPoll=0;state.renderKey=null;pollJob().catch(error=>serviceWarning(error.message));refreshActivity().catch(()=>{});if(typeof syncSelects==='function')syncSelects();}});

function excludePaths(paths){const input=$('exclusions');input.value=[...new Set([...input.value.split(/\r?\n/).filter(Boolean),...paths])].join('\n');for(const path of paths)selected.delete(path);renderFiles();notice('Добавлено в исключения: '+paths.length);}
function excludeButton(item){const button=node('button','⊖','exclude-button');button.title='Исключить '+item.name;button.setAttribute('aria-label',button.title);button.onclick=()=>excludePaths([item.path]);return button;}

$('key-help').onclick=()=>$('open-help').click();
$('connect-for-job').onclick=()=>{$('auth-form').hidden=false;$('token').focus();$('auth-form').scrollIntoView({behavior:'smooth',block:'center'});notice('Вставьте ключ доступа вверху страницы и нажмите «Подключить». После перезапуска программы ключ нужно ввести снова; обновление страницы его не удаляет.');};
$('resume-policy').onchange=()=>{};
$('recheck-service').onclick=()=>safely(async()=>{$('recheck-service').disabled=true;try{await refreshJobs();notice('Связь с программой восстановлена. Состояние задания обновлено.');}finally{$('recheck-service').disabled=false;}});
$('reload-page').onclick=()=>location.reload();
$('open-job-folder').onclick=()=>safely(()=>api('/api/jobs/'+state.job+'/open-folder',{}));

async function chooseFolder(initial=''){if(typeof chooseFolderInApp==='function')return chooseFolderInApp(initial);return chooseNativeFolder(initial);}
async function chooseNativeFolder(initial=''){
  if(state.folderSelection)return state.folderSelection;
  state.folderSelection=(async()=>{
    let result=await api('/api/folder',{initial});
    const id=result.request_id;
    while(result.busy&&!state.closing){
      await new Promise(resolve=>setTimeout(resolve,400));
      result=await api('/api/folder');
      if(result.request_id!==id)throw Error('Выбор папки изменился в другом окне. Нажмите кнопку выбора ещё раз.');
    }
    if(result.error)throw Error(result.error);
    return result.path||'';
  })();
  try{return await state.folderSelection;}finally{state.folderSelection=null;}
}
