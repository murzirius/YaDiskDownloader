const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const selected = new Set();
const state = {limitsJob:null,restoreLimits:false,limitsTouched:false,connected:false,path:'disk:/',offset:0,items:[],total:null,hasMore:false,view:'table',history:[],job:null,jobStatus:null,errorOffset:0,browseVersion:0,thumbVersion:0,urls:[],polling:false};
const phases = {new:'Новое задание',scanning:'Обход Диска',ready:'Оценка готова',downloading:'Скачивание',paused:'Пауза',stopping:'Остановка…',stopped:'Остановлено',interrupted:'Требуется продолжение',packing:'Создание ZIP',completed:'Завершено',completed_with_errors:'Завершено с неполным результатом',failed:'Операция завершилась ошибкой'};
const modes = {previews:'Только превью',both:'Оригиналы и превью',fallback:'Оригинал, превью при неудаче'};
const typeLabels = {photos:'фото',videos:'видео',music:'музыка',files:'другие файлы'};
const outcomes = {downloaded_verified:'скачан и проверен',existing_verified:'уже имеется, проверен',unavailable:'недоступно',failed:'не удалось',not_requested:'не запрашивалось',not_needed:'не требуется'};
function showPanel(panel){$('settings-panel').hidden=panel!=='settings';$('job-panel').hidden=panel!=='job';$('settings-tab').setAttribute('aria-selected',String(panel==='settings'));$('job-tab').setAttribute('aria-selected',String(panel==='job'));}
function initializePanel(){
  const settingsPanel=$('settings-panel');
  settingsPanel.querySelector('h1').textContent='Скачивание';
  settingsPanel.querySelector('input[name=report]').closest('fieldset').querySelector('legend').textContent='Отчёты';
  const destinationLabel=$('destination').closest('label');
  const labelText=[...destinationLabel.childNodes].find(child=>child.nodeType===Node.TEXT_NODE);
  if(labelText)labelText.textContent='Сохранить в';
  $('estimate').textContent='Оценить выбранное';
  for(const hint of settingsPanel.querySelectorAll(':scope > .hint'))hint.remove();
  if(!settingsPanel.querySelector('.advanced-settings')){
    const previewSelect=$('preview-size');
    const sizeLabel=previewSelect.closest('label');
    const details=node('details',undefined,'advanced-settings');
    details.append(node('summary','Дополнительные настройки'));
    details.append(sizeLabel,settingsPanel.querySelector('.numeric-settings'));
    details.append(node('p','Превью без обрезки. Файлы, отчёты и ZIP — в папке импорта.','hint'));
    const text=[...sizeLabel.childNodes].find(child=>child.nodeType===Node.TEXT_NODE);
    if(text)text.textContent='Размер превью';
    const labels={XXXL:'Максимальный · 1280 px',XXL:'1024 px',XL:'800 px',L:'500 px',M:'300 px',S:'150 px'};
    for(const option of previewSelect.options)option.textContent=labels[option.value];
    $('estimate').before(details);
  }
  if(!$('operation')){
    const label=node('label','Как загрузить','field');
    const select=node('select');
    select.id='operation';
    select.append(new Option('Новая загрузка','new'),new Option('Дозагрузить недостающее','missing'));
    label.append(select);
    settingsPanel.querySelector('h1').after(label);
  }
  if(!$('destination-label')){
    const span=node('span','Сохранить в');
    span.id='destination-label';
    if(labelText)labelText.replaceWith(span);else destinationLabel.prepend(span);
  }
  $('operation').onchange=()=>{$('destination-label').textContent=$('operation').value==='missing'?'Папка предыдущей загрузки (Disk-…)':'Сохранить в';};
  $('retries').closest('label').firstChild.textContent='Попытки всего';
  $('retries').min=1;
  $('retries').max=9;
  $('timeout').closest('label').firstChild.textContent='Лимит попытки, с';
  if($('download-zip').tagName==='A'){
    const button=node('button','Открыть папку с ZIP','download');
    button.id='download-zip';
    button.hidden=true;
    $('download-zip').replaceWith(button);
  }
  if(!$('zip-saved')){
    const info=node('div',undefined,'hint');
    info.id='zip-saved';
    info.hidden=true;
    $('download-zip').before(info);
  }
  if(!$('job-details')){
    const details=node('details',undefined,'job-details');
    details.id='job-details';
    const extra=node('div',undefined,'counters');
    extra.id='extra-counters';
    const estimate=node('div',undefined,'hint');
    estimate.id='estimate-details';
    details.append(node('summary','Подробности задания'),extra,estimate,$('job-settings'),$('output-path'));
    $('errors-detail').before(details);
  }
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
function notice(message){$('notice').textContent=message||'';$('notice').hidden=!message;}
async function api(path,data){const options={headers:{'X-CSRF-Token':csrf},credentials:'same-origin',cache:'no-store'};if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(data);}const response=await fetch(path,options);const result=await response.json();if(!response.ok)throw new Error(result.error||`HTTP ${response.status}`);return result;}
async function safely(fn){try{notice('');await fn();}catch(error){notice(error.message);}}
function connection(connected){state.connected=connected;$('connection').textContent=connected?'Диск подключён':'Не подключён';$('connection').classList.toggle('online',connected);$('auth-form').hidden=connected;$('disconnect').hidden=!connected;$('estimate').disabled=!connected;$('refresh').disabled=!connected;$('next-page').disabled=!connected||!state.hasMore;$('prev-page').disabled=!connected||state.offset===0;}
function breadcrumb(){const holder=$('breadcrumbs');holder.replaceChildren();const parts=state.path==='disk:/'?[]:state.path.slice(6).split('/');let path='disk:/';const root=node('button','Диск');root.onclick=()=>safely(()=>browse('disk:/',0,true));holder.append(root);for(const part of parts){holder.append(node('span','/','muted'));path=path.replace(/\/$/,'')+'/'+part;const target=path;const button=node('button',part);button.onclick=()=>safely(()=>browse(target,0,true));holder.append(button);}$('up').disabled=parts.length===0||!state.connected;$('back').disabled=!state.history.length||!state.connected;}
async function browse(path=state.path,offset=0,remember=false){if(!state.connected)return;const version=++state.browseVersion;$('file-list').replaceChildren(node('div','Загрузка списка…','empty'));if(remember&&path!==state.path)state.history.push({path:state.path,offset:state.offset});const result=await api('/api/browse?'+new URLSearchParams({path,offset,sort:$('sort').value}));if(version!==state.browseVersion)return;Object.assign(state,{path:result.path,offset:result.offset,items:result.items,total:result.total,hasMore:result.has_more});breadcrumb();renderFiles();$('page-info').textContent=`${state.items.length?state.offset+1:0}–${state.offset+state.items.length}${state.total===null?'':' из '+state.total} · выбор не ограничен страницей`;$('prev-page').disabled=state.offset===0;$('next-page').disabled=!state.hasMore;}
function updateSelection(){const whole=$('whole-disk').checked;$('selection-count').textContent=whole?'Выбран весь Диск':`Выбрано: ${selected.size}`;const items=visibleItems();$('select-page').disabled=whole||!items.length;$('select-page').checked=items.length>0&&items.every(item=>selected.has(item.path));$('select-page').indeterminate=items.some(item=>selected.has(item.path))&&!$('select-page').checked;const holder=$('selected-paths');holder.replaceChildren();if(whole){holder.textContent='disk:/ · весь Диск';return;}if(!selected.size){holder.textContent='Выбор пуст';return;}for(const path of selected){const row=node('div',path);holder.append(row);}}
function visibleItems(){const query=$('filter').value.toLocaleLowerCase('ru-RU');return state.items.filter(item=>(item.name||'').toLocaleLowerCase('ru-RU').includes(query));}
function checkbox(item){const box=node('input');box.type='checkbox';box.checked=selected.has(item.path);box.disabled=$('whole-disk').checked;box.setAttribute('aria-label','Выбрать '+item.name);box.onchange=()=>{if(box.checked)selected.add(item.path);else selected.delete(item.path);renderFiles();};return box;}
function icon(item){const div=node('div',item.type==='dir'?'ПАПКА':(item.name.split('.').pop()||'ФАЙЛ').slice(0,6).toUpperCase(),'file-icon'+(item.type==='dir'?' folder':''));if(item.has_preview&&item.type==='file')div.dataset.previewPath=item.path;return div;}
function filename(item){const button=node('button',item.name,'name-button');button.title=item.path;button.onclick=()=>{if(item.type==='dir')safely(()=>browse(item.path,0,true));else{if(selected.has(item.path))selected.delete(item.path);else selected.add(item.path);renderFiles();}};return button;}
function renderFiles(){state.thumbVersion++;for(const url of state.urls)URL.revokeObjectURL(url);state.urls=[];const holder=$('file-list');holder.replaceChildren();const items=visibleItems();if(!items.length){holder.append(node('div',state.items.length?'По фильтру ничего не найдено':'Папка пуста','empty'));updateSelection();return;}if(state.view==='table'){const table=node('table',undefined,'files-table');const thead=node('thead');const header=node('tr');for(const text of ['','Имя','Размер','Изменён'])header.append(node('th',text));thead.append(header);table.append(thead);const body=node('tbody');for(const item of items){const row=node('tr');row.classList.toggle('selected',selected.has(item.path));const checkCell=node('td');checkCell.append(checkbox(item));const nameCell=node('td');const wrapper=node('div',undefined,'file-name');wrapper.append(icon(item),filename(item));nameCell.append(wrapper);row.append(checkCell,nameCell,node('td',item.type==='dir'?'—':bytes(item.size),'muted'),node('td',item.modified?new Date(item.modified).toLocaleDateString('ru-RU'):'—','muted'));body.append(row);}table.append(body);holder.append(table);}else{const grid=node('div',undefined,'files-grid');for(const item of items){const tile=node('div',undefined,'file-tile');tile.classList.toggle('selected',selected.has(item.path));const top=node('div',undefined,'tile-top');top.append(checkbox(item),node('span',item.type==='dir'?'Папка':bytes(item.size),'muted'));tile.append(top,icon(item),filename(item));grid.append(tile);}holder.append(grid);}updateSelection();loadThumbnails();}
function loadThumbnails(){const version=state.thumbVersion;const queue=[];let running=0;const observer=new IntersectionObserver(entries=>{for(const entry of entries){if(entry.isIntersecting){observer.unobserve(entry.target);queue.push(entry.target);}}pump();},{root:$('file-list'),rootMargin:'100px'});for(const el of $('file-list').querySelectorAll('[data-preview-path]'))observer.observe(el);async function pump(){if(version!==state.thumbVersion){observer.disconnect();return;}while(running<2&&queue.length){const el=queue.shift();running++;(async()=>{try{const response=await fetch('/api/thumbnail?'+new URLSearchParams({path:el.dataset.previewPath,size:state.view==='grid'?'M':'S'}),{headers:{'X-CSRF-Token':csrf},credentials:'same-origin',cache:'no-store'});if(!response.ok)return;const blob=await response.blob();if(version!==state.thumbVersion||!state.connected)return;const url=URL.createObjectURL(blob);state.urls.push(url);const img=node('img');img.src=url;img.alt='';el.replaceChildren(img);}catch{}finally{running--;pump();}})();}}}
function settings(){return {operation:$('operation').value,paths:[...selected],whole_disk:$('whole-disk').checked,destination:$('destination').value,mode:$('mode').value,file_types:$('file-types')?[...document.querySelectorAll('input[name=file-type]:checked')].map(el=>el.value):['photos','videos','music','files'],preview_size:$('preview-size').value,workers:Number($('workers').value),attempts:Number($('retries').value),timeout:Number($('timeout').value),reports:[...document.querySelectorAll('input[name=report]:checked')].map(el=>el.value)};}
async function refreshJobs(){const result=await api('/api/status');connection(result.connected);const history=$('job-history');history.replaceChildren();if(!result.jobs.length){history.append(new Option('Нет заданий',''));$('job-content').hidden=true;return;}for(const job of result.jobs)history.append(new Option(`${job.id.slice(0,8)} · ${phases[job.phase]||job.phase}`,job.id));if(!state.job||!result.jobs.some(job=>job.id===state.job))state.job=result.jobs[0].id;history.value=state.job;showPanel('job');await pollJob();}
function counter(holder,label,value,wide=false){const div=node('div',undefined,wide?'storage-counter':undefined);div.append(node('span',label),node('strong',String(value)));holder.append(div);}
function renderJobCounters(job){
  const main=$('counters');
  const extra=$('extra-counters');
  main.replaceChildren();
  extra.replaceChildren();
  for(const [label,value,wide] of [
    ['Файлы',`${job.processed} / ${job.files}`],
    ['Скорость',bytes(job.speed)+'/с'],
    ['Результаты на ПК',bytes(job.local_bytes),true],
    ['Свободно на диске',bytes(job.free_bytes),true],
    ['Оригиналы',job.originals],
    ['Превью',job.previews],
    ['Замены оригинала',job.replacements],
    ['Ошибки',job.errors+job.scan_errors+(job.counts.no_preview||0)]
  ])counter(main,label,value,wide);
  if(['downloading','packing','paused','completed','completed_with_errors'].includes(job.phase))counter(main,job.eta_phase==='packing'?'До конца ZIP':'До конца загрузки',job.phase==='paused'?'Пауза':remainingTime(job.eta_seconds),true);
  for(const [label,value] of [
    ['Папки',job.folders],['Уже имеются',job.existing],['Исключены по типу',job.counts.filtered||0],
    ['Нет превью',job.missing_previews],['Отменено',job.cancelled],
    ['Передано',bytes(job.downloaded)],['Сохранено',bytes(job.saved_bytes)]
  ])counter(extra,label,value);
}
function renderEstimate(job){
  const estimate=job.estimate;
  const holder=$('estimate-info');
  const details=$('estimate-details');
  const conditional=estimate.conditional_preview_sizes||0;
  const unknown=estimate.unknown_sizes||0;
  const hasUnknown=unknown>0||conditional>0;
  holder.replaceChildren(node('strong',job.scan_complete?'Оценка объёма':'Найдено при обходе'));
  const sizes=node('div',undefined,'estimate-values');
  counter(sizes,'Данные',!estimate.known_bytes&&hasUnknown?'Размер неизвестен':bytes(estimate.known_bytes)+(hasUnknown?' + неизвестно':''));
  counter(sizes,'ZIP ≈',!estimate.zip_basis_bytes&&hasUnknown?'Пока неизвестен':bytes(estimate.zip_basis_bytes)+(hasUnknown?' + неизвестно':'')+' + служебные данные');
  holder.append(sizes);
  if(hasUnknown)holder.append(node('div',`Неизвестных размеров: ${unknown}${conditional?' + до '+conditional+' превью при ошибке':''}`,'estimate-note'));
  holder.append(node('div','Предварительно · без сжатия','estimate-note'));
  holder.hidden=!['new','scanning','ready'].includes(job.phase)&&!(job.phase==='paused'&&!job.scan_complete);
  details.replaceChildren();
  details.append(node('p','Найденные оригиналы: '+bytes(estimate.original_bytes)+'. Неизвестных размеров: '+estimate.original_unknown+'.'));
  details.append(node('p','Запас для ZIP и временных файлов: '+bytes(estimate.extra_temporary_bytes)+(hasUnknown?' + неизвестный объём':'')+' сверх результата.'));
  details.append(node('p','Размеры превью заранее неизвестны. Приоритет оригинала оценивается по оригиналам; итог зависит от успешности загрузки.'));
  details.append(node('p','Результаты на ПК: актуальные файлы этого задания, его отчёты, ZIP и текущие временные файлы. Старые версии исходных файлов и постороннее содержимое папки не входят в счётчик.'));
}
async function pollJob(){
  if(!state.job||state.polling)return;
  state.polling=true;
  try{
    const id=state.job;
    const job=await api('/api/jobs/'+id);
    if(id!==state.job)return;
    state.jobStatus=job;
    if(state.restoreLimits&&state.limitsJob!==job.id){
      state.limitsJob=job.id;
      $('workers').value=job.settings.workers;
      $('retries').value=job.settings.attempts??job.settings.retries+1;
      $('timeout').value=job.settings.timeout;
      for(const input of document.querySelectorAll('input[name=file-type]'))input.checked=(job.settings.file_types||Object.keys(typeLabels)).includes(input.value);
    }
    connection(job.connected);
    const option=$('job-history').selectedOptions[0];
    if(option)option.textContent=job.id.slice(0,8)+' · '+(phases[job.phase]||job.phase);
    $('job-content').hidden=false;
    $('phase').textContent=phases[job.phase]||job.phase;
    const packing=job.phase==='packing'||(job.phase==='paused'&&job.zip_total&&job.zip_done<job.zip_total);
    const percent=packing?(job.zip_total?Math.min(100,job.zip_done*100/job.zip_total):0):job.percent;
    if(job.phase==='scanning'||!job.scan_complete&&!packing){
      $('progress').removeAttribute('value');
      $('percentage').textContent='Обход без общего %';
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
        div.append(progress,node('span',bytes(file.done)+' / '+bytes(file.size),'muted'));
      }else div.append(node('div',bytes(file.done),'muted'));
      active.append(div);
    }
    renderEstimate(job);
    $('job-settings').textContent=`${modes[job.settings.mode]} · ${(job.settings.file_types||Object.keys(typeLabels)).map(value=>typeLabels[value]).join(', ')} · ${job.settings.preview_size} · ${job.settings.workers} потока · ${job.settings.reports.join(', ')} · ${job.settings.whole_disk?'весь Диск':job.settings.paths.length+' исходных путей'}`;
    const live=job.live;
    const hasIssues=job.errors||job.scan_errors||job.missing_previews||job.replacements;
    $('start-job').hidden=job.phase!=='ready';
    $('start-job').disabled=live||!state.connected;
    $('pause-job').hidden=!live||!['scanning','downloading','packing'].includes(job.phase);
    $('resume-job').hidden=!['paused','stopped','interrupted','failed'].includes(job.phase);
    $('resume-job').disabled=job.phase!=='paused'&&(!state.connected||live);
    $('stop-job').hidden=!live;
    $('stop-job').disabled=job.phase==='stopping';
    $('retry-job').hidden=!hasIssues;
    $('retry-job').disabled=live||!state.connected;
    $('pack-job').hidden=live||job.phase==='ready';
    $('pack-job').disabled=!job.scan_complete&&!job.processed;
    $('pack-job').textContent=job.zip_path?'Пересобрать ZIP':'Собрать ZIP';
    $('result-info').textContent=job.error||(job.phase==='completed_with_errors'?'Результат неполный.':job.phase==='stopped'?'Остановлено. Незавершённых файлов: '+job.cancelled+'.':'');
    if(job.zip_path){
      $('download-zip').hidden=false;
      $('zip-saved').hidden=false;
      $('zip-saved').textContent='ZIP сохранён: '+job.zip_path+' · '+bytes(job.zip_bytes);
      $('download-zip').textContent='Открыть папку с ZIP';
    }else{$('download-zip').hidden=true;$('zip-saved').hidden=true;}
    $('output-path').textContent=job.root+(job.zip_path?'\nZIP: '+job.zip_path:'');
    $('open-journal').hidden=!job.settings.journal_local;
    $('journal-info').textContent=job.journal_error||(job.settings.journal_local?'Журнал: '+job.settings.journal_local+' · каждые 5 минут и при завершении':'');
    $('errors-title').textContent=`Ошибки и замены (${job.errors+job.scan_errors+(job.counts.no_preview||0)+job.replacements})`;
    $('errors-detail').hidden=!hasIssues;
    if($('errors-detail').open)await loadErrors();
  }finally{
    state.polling=false;
  }
}
async function loadErrors(){if(!state.job)return;const result=await api(`/api/jobs/${state.job}/errors?offset=${state.errorOffset}`);const holder=$('errors-list');holder.replaceChildren();if(!result.items.length)holder.append(node('p','Нет записей на этой странице','muted'));for(const item of result.items){const div=node('div',undefined,'error-item');div.append(node('strong',item.path),node('p',item.error),node('span',`Оригинал: ${outcomes[item.original_status]||item.original_status}. Превью: ${outcomes[item.preview_status]||item.preview_status}.${item.fallback?' Переход к превью после неудачи оригинала.':''}`));holder.append(div);}$('errors-prev').disabled=state.errorOffset===0;$('errors-next').disabled=!result.has_more;}
async function action(action){let limits;if(['start','resume','retry'].includes(action)){const previous=state.jobStatus?.settings;limits=!state.limitsTouched&&state.limitsJob!==state.job&&previous?{workers:previous.workers,attempts:previous.attempts,timeout:previous.timeout}:{workers:Number($('workers').value),attempts:Number($('retries').value),timeout:Number($('timeout').value)};}await api('/api/jobs/'+state.job+'/action',{action,limits});await pollJob();}
$('auth-form').onsubmit=event=>{event.preventDefault();safely(async()=>{const input=$('token');const token=input.value;input.value='';await api('/api/connect',{token});connection(true);await browse('disk:/');await refreshJobs();});};
$('disconnect').onclick=()=>safely(async()=>{await api('/api/disconnect',{});connection(false);state.browseVersion++;state.thumbVersion++;for(const url of state.urls)URL.revokeObjectURL(url);state.urls=[];state.items=[];$('file-list').replaceChildren(node('div','Диск отключён. Токен удалён из состояния приложения.','empty'));await pollJob();});
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
$('choose-folder').onclick=()=>safely(async()=>{$('choose-folder').disabled=true;try{const result=await api('/api/folder',{});if(result.path)$('destination').value=result.path;}finally{$('choose-folder').disabled=false;}});
$('estimate').onclick=()=>safely(async()=>{$('estimate').disabled=true;try{const result=await api('/api/jobs',settings());state.job=result.id;state.restoreLimits=true;state.limitsTouched=false;state.errorOffset=0;await refreshJobs();}finally{$('estimate').disabled=!state.connected;}});
$('refresh-jobs').onclick=()=>safely(refreshJobs);
$('job-history').onchange=()=>{state.job=$('job-history').value;state.restoreLimits=true;state.limitsTouched=false;state.errorOffset=0;safely(pollJob);};
for(const id of ['workers','retries','timeout'])$(id).oninput=()=>{state.limitsTouched=true;};
for(const [id,verb] of [['start-job','start'],['pause-job','pause'],['resume-job','resume'],['stop-job','stop'],['retry-job','retry'],['pack-job','pack']])$(id).onclick=()=>safely(()=>action(verb));
$('errors-detail').ontoggle=()=>{if($('errors-detail').open)safely(loadErrors);};
$('errors-prev').onclick=()=>{state.errorOffset=Math.max(0,state.errorOffset-100);safely(loadErrors);};
$('errors-next').onclick=()=>{state.errorOffset+=100;safely(loadErrors);};
$('download-zip').onclick=()=>safely(()=>api('/api/jobs/'+state.job+'/open-output',{}));
$('open-journal').onclick=()=>safely(()=>api('/api/jobs/'+state.job+'/open-journal',{}));
$('settings-tab').onclick=()=>showPanel('settings');
$('job-tab').onclick=()=>showPanel('job');
initializePanel();initializeHelp();connection(false);breadcrumb();updateSelection();safely(async()=>{await refreshJobs();if(state.connected)await browse();});
setInterval(()=>pollJob().catch(error=>notice(error.message)),1000);
