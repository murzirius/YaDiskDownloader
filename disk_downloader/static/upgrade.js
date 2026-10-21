'use strict';

// Theme files contain named colours only; executable CSS and URLs are rejected.
const themeKeys=['bg','panel','input','line','text','muted','accent'];
const baseThemes=[
  {
    "name": "Тёмная",
    "colors": {
      "bg": "#15171b",
      "panel": "#1c1f24",
      "input": "#22262d",
      "line": "#434b59",
      "text": "#e2e5ea",
      "muted": "#a6afbe",
      "accent": "#a8c3e1"
    }
  },
  {
    "name": "Светлая",
    "colors": {
      "bg": "#f2f5fa",
      "panel": "#ffffff",
      "input": "#edf1f7",
      "line": "#bdc8d7",
      "text": "#182537",
      "muted": "#4e6078",
      "accent": "#245c95"
    }
  },
  {
    "name": "Really Black",
    "colors": {
      "bg": "#000000",
      "panel": "#0c0c0c",
      "input": "#171717",
      "line": "#484848",
      "text": "#f5f5f5",
      "muted": "#b8b8b8",
      "accent": "#93c5fd"
    }
  },
  {
    "name": "Dracula",
    "colors": {
      "bg": "#282a36",
      "panel": "#30323f",
      "input": "#363848",
      "line": "#62677f",
      "text": "#f8f8f2",
      "muted": "#b7bed6",
      "accent": "#bd93f9"
    }
  },
  {
    "name": "Nord",
    "colors": {
      "bg": "#2e3440",
      "panel": "#3b4252",
      "input": "#434c5e",
      "line": "#69778d",
      "text": "#eceff4",
      "muted": "#d8dee9",
      "accent": "#88c0d0"
    }
  },
  {
    "name": "Catppuccin Mocha",
    "colors": {
      "bg": "#1e1e2e",
      "panel": "#242436",
      "input": "#313244",
      "line": "#585b70",
      "text": "#cdd6f4",
      "muted": "#bac2de",
      "accent": "#cba6f7"
    }
  },
  {
    "name": "Catppuccin Latte",
    "colors": {
      "bg": "#eff1f5",
      "panel": "#ffffff",
      "input": "#e6e9ef",
      "line": "#9ca0b0",
      "text": "#4c4f69",
      "muted": "#5c5f77",
      "accent": "#8839ef"
    }
  },
  {
    "name": "Tokyo Night",
    "colors": {
      "bg": "#1a1b26",
      "panel": "#202230",
      "input": "#292e42",
      "line": "#565f89",
      "text": "#c0caf5",
      "muted": "#a9b1d6",
      "accent": "#7aa2f7"
    }
  },
  {
    "name": "Everforest",
    "colors": {
      "bg": "#2d353b",
      "panel": "#343f44",
      "input": "#3d484d",
      "line": "#687a73",
      "text": "#d3c6aa",
      "muted": "#b2b9a5",
      "accent": "#a7c080"
    }
  },
  {
    "name": "Solarized Dark",
    "colors": {
      "bg": "#002b36",
      "panel": "#073642",
      "input": "#0a3c48",
      "line": "#586e75",
      "text": "#eee8d5",
      "muted": "#93a1a1",
      "accent": "#2aa198"
    }
  },
  {
    "name": "Solarized Light",
    "colors": {
      "bg": "#fdf6e3",
      "panel": "#fffbed",
      "input": "#eee8d5",
      "line": "#93a1a1",
      "text": "#073642",
      "muted": "#586e75",
      "accent": "#006f86"
    }
  },
  {
    "name": "Gruvbox",
    "colors": {
      "bg": "#282828",
      "panel": "#32302f",
      "input": "#3c3836",
      "line": "#7c6f64",
      "text": "#ebdbb2",
      "muted": "#bdae93",
      "accent": "#fabd2f"
    }
  },
  {
    "name": "High Contrast",
    "colors": {
      "bg": "#000000",
      "panel": "#080808",
      "input": "#151515",
      "line": "#ffffff",
      "text": "#ffffff",
      "muted": "#e2e2e2",
      "accent": "#ffea00"
    }
  }
];
let installedThemes=[],currentTheme=baseThemes[0];
function validateTheme(value){
  if(!value||Array.isArray(value)||typeof value!=='object'||Object.keys(value).some(k=>!['name','colors'].includes(k))||typeof value.name!=='string'||!value.name.trim()||value.name.length>40||/[\x00-\x1f]/.test(value.name))throw Error('В файле темы нужны название name и семь цветов в colors.');
  const colors=value.colors;
  if(!colors||Array.isArray(colors)||typeof colors!=='object'||Object.keys(colors).length!==themeKeys.length||themeKeys.some(key=>!Object.hasOwn(colors,key)||!/^#[0-9a-f]{6}$/i.test(colors[key])))throw Error('Цвета темы должны быть в формате #RRGGBB: '+themeKeys.join(', ')+'.');
  const luminance=color=>{const values=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return values[0]*.2126+values[1]*.7152+values[2]*.0722;};
  const contrast=(a,b)=>{const x=luminance(a),y=luminance(b);return(Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
  if(['bg','panel','input'].some(key=>contrast(colors.text,colors[key])<4.5)||contrast(colors.muted,colors.panel)<3)throw Error('Текст темы плохо читается. Увеличьте разницу между цветами текста и фона.');
  return {name:value.name.trim(),colors:Object.fromEntries(themeKeys.map(key=>[key,colors[key]]))};
}
function useTheme(theme,save=true){
  theme=validateTheme(theme);currentTheme=theme;
  for(const key of themeKeys)document.documentElement.style.setProperty('--'+key,theme.colors[key]);
  document.documentElement.style.setProperty('--control-bg',theme.colors.input);
  document.documentElement.style.setProperty('--control-accent',theme.colors.accent);
  document.documentElement.style.colorScheme=parseInt(theme.colors.bg.slice(1,3),16)>140?'light':'dark';
  if(save)try{localStorage.setItem('yadisk-theme',JSON.stringify(theme));}catch{notice('Тема применена. Браузер запретил сохранение настройки.');}
}
try{
  const saved=JSON.parse(localStorage.getItem('yadisk-installed-themes')||'[]');
  if(Array.isArray(saved)&&saved.length<=20)installedThemes=saved.map(validateTheme);
  const chosen=localStorage.getItem('yadisk-theme');if(chosen)useTheme(JSON.parse(chosen),false);
}catch{installedThemes=[];useTheme(baseThemes[0],false);}
const themeDialog=node('dialog',undefined,'tools-dialog');themeDialog.id='theme-dialog';
const themeTitle=node('h2','Оформление'),themeClose=node('button','Закрыть'),themeChoices=node('div',undefined,'theme-choices');
themeClose.onclick=()=>themeDialog.close();
const importTheme=node('input');importTheme.type='file';importTheme.accept='.json,application/json';importTheme.setAttribute('aria-label','Установить тему из JSON');
function renderThemes(){themeChoices.replaceChildren();for(const theme of [...baseThemes,...installedThemes]){const button=node('button',theme.name,'theme-choice');button.style.borderColor=theme.colors.accent;button.style.background=theme.colors.panel;button.style.color=theme.colors.text;button.onclick=()=>safely(()=>useTheme(theme));themeChoices.append(button);}}
function saveJSON(value,name){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const link=node('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
importTheme.onchange=()=>safely(async()=>{
  const file=importTheme.files[0];if(!file)return;
  try{
    if(file.size>16384)throw Error('Файл темы слишком большой. Допустимо до 16 КБ.');
    const theme=validateTheme(JSON.parse(await file.text()));
    const next=installedThemes.filter(t=>t.name!==theme.name);if(next.length>=20)throw Error('Установлено 20 тем. Сначала удалите установленные темы.');
    next.push(theme);localStorage.setItem('yadisk-installed-themes',JSON.stringify(next));installedThemes=next;useTheme(theme);renderThemes();notice('Тема «'+theme.name+'» установлена.');
  }catch(error){throw Error('Не удалось установить тему: '+error.message);}finally{importTheme.value='';}
});
themeDialog.append(themeTitle,themeChoices,node('p','Выберите готовую тему или установите свою. Скачайте образец, измените название и цвета и выберите полученный JSON-файл. Скрипты и произвольные стили не принимаются.','hint'),importTheme,
  toolButton('Скачать образец темы',async()=>saveJSON(currentTheme,'yadisk-theme.json')),
  toolButton('Удалить установленные темы',async()=>{installedThemes=[];try{localStorage.removeItem('yadisk-installed-themes');}catch{}useTheme(baseThemes[0]);renderThemes();}),themeClose);
document.body.append(themeDialog);renderThemes();
const themeButton=toolButton('Оформление',async()=>themeDialog.showModal());themeButton.id='open-themes';$('open-help').before(themeButton);

for(const id of ['missing-policy','resume-policy'])$(id).append(new Option('Сохранить и оригиналы, и превью','both'));
$('missing-policy-field').querySelector('.hint').textContent='Сначала скачиваем файлы без копий. Если нужен только оригинал, прежнее превью удалим после успешного скачивания и проверки оригинала. В режиме двух копий превью остаются.';
const verifyLabel=node('label','Проверка скачанных файлов','field'),verifySelect=node('select');verifySelect.id='verification-mode';
verifySelect.append(new Option('Быстрая — повторно читать только изменённые файлы','fast'),new Option('Полная — перечитать содержимое каждого файла','full'));
verifyLabel.append(verifySelect);stepBoxes[2].insertBefore(verifyLabel,confirmation);
const verificationHint=node('p','Быстрая проверка использует прежнюю проверку целостности, только если файловая система подтверждает отсутствие изменений. Если подтвердить это нельзя, файл проверяется полностью. Журналы и отчёты не проверяются как скачанный контент.','hint');verifyLabel.after(verificationHint);
const duplicateField=node('label',undefined,'check'),duplicateCheck=node('input');duplicateCheck.type='checkbox';duplicateCheck.id='deduplicate';
duplicateField.append(duplicateCheck,document.createTextNode('Не скачивать точные дубликаты повторно'));
verificationHint.after(duplicateField,node('p','Защита выключена по умолчанию. Совпадение имени, размера или даты не означает одинаковое содержимое: нужна проверка SHA-256. Найденные лишние копии попадут в список «Дубликаты». Проверка ничего из этого списка не удаляет: перенос в корзину запускается отдельно, после выбора и подтверждения. Для превью без контрольной суммы сначала потребуется загрузка.','hint'));
const originalAdvancedSettings=advancedSettings;
advancedSettings=function(){return {...originalAdvancedSettings(),verification_mode:verifySelect.value,deduplicate:duplicateCheck.checked};};
const originalApplySettings=applyDownloadSettings;
applyDownloadSettings=function(value){verifySelect.value=value.verification_mode||'fast';duplicateCheck.checked=!!value.deduplicate;return originalApplySettings(value);};
const originalRuntimeLimits=runtimeLimits;
runtimeLimits=function(){return {...originalRuntimeLimits(),verification_mode:verifySelect.value,deduplicate:duplicateCheck.checked};};

async function checkDestination(path,revisionValid=()=>true){
  let check=await api('/api/destination-check',{path});const identity=check.id;
  const deadline=Date.now()+20000;
  while(check.busy){
    if(!revisionValid())return null;
    if(Date.now()>deadline)throw Error('Накопитель долго отвечает. Проверьте подключение сетевой папки или диска. Управление заданием остаётся доступным; можно вернуться к проверке позже.');
    await new Promise(resolve=>setTimeout(resolve,350));check=await api('/api/destination-check');
    if(check.id!==identity)throw Error('Проверяется другая папка. Повторите выбор папки.');
  }
  if(check.error)throw Error(check.error);
  return revisionValid()?check.result:null;
}
inspectDestination=async function(){
  const revision=++destinationRevision,path=$('destination').value;
  destinationStatus.textContent='Проверяем доступ к папке и свободное место…';
  try{
    const result=await checkDestination(path,()=>revision===destinationRevision);if(!result)return null;
    if($('operation').value==='missing'&&!result.exists)throw Error('Для дозагрузки нужна существующая папка с прежними файлами. Выберите её на предыдущем шаге.');
    destinationStatus.textContent='Запись доступна. Свободно '+bytes(result.free_bytes)+'. Заполнено '+result.used_percent+'%.'+(result.previous_import?' Найдена история загрузки.':$('operation').value==='missing'?' История не найдена. Оригиналы будем сверять с контрольными суммами Яндекс.Диска; неопознанные файлы останутся на месте.':'');
    const chosen=settings().whole_disk?['disk:/']:[...selected].sort();
    if($('operation').value==='missing'&&chosen.length&&result.previous_paths?.length&&JSON.stringify(chosen)!==JSON.stringify([...result.previous_paths].sort()))destinationStatus.textContent+=' Выбор исходных папок отличается от прежней загрузки. Проверьте выбор перед запуском.';
    return result;
  }catch(error){if(revision===destinationRevision)destinationStatus.textContent=error.message;throw error;}
};
const originalAction=action;
let checkingStart=false;
action=async function(verb){
  if(verb==='start'){
    if(checkingStart)return;checkingStart=true;const jobId=state.job;
    try{
      const result=await checkDestination(state.jobStatus.root);
      if(state.job!==jobId)return;
      const plan=await api('/api/jobs/'+jobId+'/plan');
      if(plan.known_bytes>result.free_bytes)throw Error('На выбранном накопителе недостаточно свободного места для известных оригиналов. Освободите место или измените выбор.');
    }finally{checkingStart=false;}
  }
  return originalAction(verb);
};

const exclusionList=node('div',undefined,'exclusion-chips');$('exclusions').after(exclusionList);
let exclusionKey='';
function renderExclusionChoices(){
  const key=$('exclusions').value;if(key===exclusionKey)return;exclusionKey=key;exclusionList.replaceChildren();
  for(const path of key.split(/\r?\n/).map(v=>v.trim()).filter(Boolean)){
    const button=node('button',path+' ×');button.type='button';button.title='Снова разрешить скачивание: '+path;
    button.onclick=()=>{$('exclusions').value=$('exclusions').value.split(/\r?\n/).filter(v=>v.trim()!==path).join('\n');renderExclusionChoices();renderSetup();renderFiles();};exclusionList.append(button);
  }
}
const originalExcludePaths=excludePaths;
excludePaths=function(paths){originalExcludePaths(paths);renderExclusionChoices();};
excludeButton=function(item){
  const excluded=$('exclusions').value.split(/\r?\n/).some(path=>path.trim()===item.path);
  const button=node('button',excluded?'Вернуть':'Исключить','exclude-button');
  button.title=(excluded?'Снова разрешить скачивание: ':'Не скачивать: ')+item.name;
  button.setAttribute('aria-label',button.title);
  button.onclick=()=>{
    if(!excluded){excludePaths([item.path]);return;}
    $('exclusions').value=$('exclusions').value.split(/\r?\n/).filter(path=>path.trim()!==item.path).join('\n');
    renderExclusionChoices();renderSetup();renderFiles();
  };
  return button;
};
$('exclusions').addEventListener('input',renderExclusionChoices);
$('exclusions').addEventListener('change',renderFiles);
document.addEventListener('change',renderExclusionChoices);

const taskStage=node('section',undefined,'task-stage');taskStage.id='task-stage';taskStage.role='status';$('phase').parentElement.before(taskStage);
const planBox=node('section',undefined,'download-plan');planBox.id='download-plan';planBox.hidden=true;$('estimate-info').before(planBox);
let planKey='',stageKey='',lastMovement=Date.now(),movementKey='',lastJobId='';
async function showPlan(job){
  const key=JSON.stringify([job.id,job.phase,job.settings.missing_policy]);if(planKey===key)return;planKey=key;
  if(!['ready','completed','completed_with_errors'].includes(job.phase)){planBox.hidden=true;return;}
  const plan=await api('/api/jobs/'+job.id+'/plan');if(state.job!==job.id||state.jobStatus?.phase!==job.phase)return;
  if(job.phase!=='ready'){
    planBox.hidden=false;planBox.replaceChildren(node('h2','Результат этой загрузки'),node('p','Новых оригиналов: '+plan.new_originals+' · Новых превью: '+plan.new_previews+' · Уже были сохранены: '+plan.skipped+'.'),node('p','Всего оригиналов: '+plan.originals+' · Только превью: '+plan.preview_only+' · Без копии: '+plan.missing+'.'));return;
  }
  planBox.hidden=false;planBox.replaceChildren(node('h2','Что будет скачано'),node('p','В списке: '+plan.files+'. Уже есть оригиналы: '+plan.originals+'. Только превью: '+plan.preview_only+'. Без копии: '+plan.missing+'.'),node('strong','Нужно обработать: '+plan.to_download+' файлов'),node('p','Известный объём оригиналов: '+bytes(plan.known_bytes)+'. '+plan.note,'hint'));
  if(plan.warning)planBox.append(node('p',plan.warning,'hint'));
}

const fileDialog=node('dialog',undefined,'tools-dialog');fileDialog.id='job-files-dialog';
const fileSearch=node('input');fileSearch.type='search';fileSearch.placeholder='Имя или путь файла';fileSearch.setAttribute('aria-label','Поиск в задании');
const fileFilter=node('select');fileFilter.setAttribute('aria-label','Состояние файла');
for(const [value,title] of [['all','Все файлы'],['original','Есть оригинал'],['preview','Есть превью'],['missing','Нет сохранённой копии'],['problems','Есть ошибка'],['skipped','Пропущенные и исключённые']])fileFilter.append(new Option(title,value));
const fileRows=node('div'),filePage=node('p',undefined,'hint'),fileBack=node('button','Назад'),fileNext=node('button','Дальше'),fileClose=node('button','Закрыть');
let fileJob=null,fileOffset=0,fileRevision=0,fileTimer;
fileClose.onclick=()=>fileDialog.close();
async function loadTaskFiles(){
  const revision=++fileRevision,id=fileJob,offset=fileOffset;fileRows.textContent='Получаем файлы…';
  const result=await api('/api/jobs/'+id+'/files?'+new URLSearchParams({q:fileSearch.value,state:fileFilter.value,offset}));
  if(revision!==fileRevision||fileJob!==id||!fileDialog.open)return;
  fileRows.replaceChildren();
  for(const file of result.items){
    const row=node('div',undefined,'tool-row');row.append(node('strong',file.path),node('p',file.original?'Оригинал сохранён'+(file.preview?' · Превью сохранено':''):file.preview?'Сохранено только превью':'Сохранённой копии пока нет'));
    if(file.reason)row.append(node('p',file.reason,'hint'));
    if(file.error)row.append(node('p',file.error));
    const actions=node('div',undefined,'actions');
    if(file.original||file.preview)actions.append(toolButton('Показать в папке',async()=>api('/api/jobs/'+id+'/open-file-folder',{path:file.path})));
    const retry=toolButton('Повторить файл',async()=>{await api('/api/jobs/'+id+'/action',{action:'retry_selected',paths:[file.path],limits:state.job===id?runtimeLimits():undefined});fileDialog.close();await openJob(id);});retry.disabled=file.status==='excluded'||!state.connected||state.jobStatus?.live||state.jobStatus?.phase==='ready';actions.append(retry);row.append(actions);fileRows.append(row);
  }
  if(!result.items.length)fileRows.append(node('p','По этим условиям файлов нет.'));
  fileBack.disabled=offset===0;fileNext.disabled=!result.has_more;filePage.textContent=result.items.length?'Файлы '+(offset+1)+'–'+(offset+result.items.length):'Нет результатов';
}
fileSearch.oninput=()=>{clearTimeout(fileTimer);fileOffset=0;fileTimer=setTimeout(()=>safely(loadTaskFiles),300);};
fileFilter.onchange=()=>{fileOffset=0;safely(loadTaskFiles);};fileBack.onclick=()=>{fileOffset=Math.max(0,fileOffset-100);safely(loadTaskFiles);};fileNext.onclick=()=>{fileOffset+=100;safely(loadTaskFiles);};
fileDialog.append(node('h2','Файлы задания'),fileSearch,fileFilter,fileRows,filePage,fileBack,fileNext,fileClose);document.body.append(fileDialog);
const browseTaskFiles=toolButton('Найти файл в задании',async()=>{if(!state.job)return;fileJob=state.job;fileOffset=0;fileDialog.showModal();await loadTaskFiles();});browseTaskFiles.id='browse-task-files';$('job-details').before(browseTaskFiles);

const duplicateDialog=node('dialog',undefined,'tools-dialog');duplicateDialog.id='duplicate-review-dialog';
const duplicateRows=node('div'),duplicateStatus=node('p',undefined,'hint'),duplicatePage=node('p',undefined,'hint');
const duplicateBack=node('button','Назад'),duplicateNext=node('button','Дальше'),duplicateRefresh=node('button','Обновить список'),duplicateRemove=node('button','Переместить выбранные в корзину'),duplicateCancel=node('button','Остановить очистку'),duplicateClose=node('button','Закрыть');
const duplicateConfirm=node('div',undefined,'duplicate-confirm');duplicateConfirm.hidden=true;
const duplicateConfirmText=node('p'),duplicateYes=node('button','Да, переместить в корзину'),duplicateNo=node('button','Отмена');duplicateConfirm.append(duplicateConfirmText,duplicateYes,duplicateNo);
let duplicateJob=null,duplicateOffset=0,duplicateRevision=0,duplicatePolling=null,duplicateBusy=false,duplicateCanCleanup=true;
const duplicateSelected=new Map();
function duplicateSelection(){duplicateRemove.disabled=duplicateBusy||!duplicateCanCleanup||!duplicateSelected.size;duplicateRemove.textContent='Переместить выбранные в корзину ('+duplicateSelected.size+')';}
async function loadDuplicates(){
  const revision=++duplicateRevision,id=duplicateJob,offset=duplicateOffset;
  const result=await api('/api/jobs/'+id+'/duplicates?offset='+offset);
  if(revision!==duplicateRevision||duplicateJob!==id||!duplicateDialog.open)return;
  clearTimeout(duplicatePolling);const cleanup=result.cleanup;duplicateBusy=cleanup.busy;duplicateCanCleanup=result.can_cleanup;
  duplicateStatus.textContent=cleanup.busy?(cleanup.job===id?'Очистка: '+cleanup.processed+' / '+cleanup.total+'. '+cleanup.current:'В другом задании выполняется очистка.'):
    cleanup.job===id&&cleanup.total?((cleanup.cancelled?'Очистка остановлена. ':'')+'В корзине: '+cleanup.removed+'. Оставлено: '+cleanup.skipped+'. '+(cleanup.error||'')+(cleanup.zip_outdated?' После очистки соберите ZIP заново.':'')):'Список найденных копий. Сам просмотр и проверка ничего не удаляют.';
  if(!cleanup.busy&&!result.can_cleanup)duplicateStatus.textContent+=' Для очистки остановите текущую загрузку или проверку и дождитесь её завершения.';
  duplicateRows.replaceChildren();
  for(const item of result.items){
    const row=node('div',undefined,'tool-row'),label=node('label',undefined,'check'),check=node('input');check.type='checkbox';check.checked=duplicateSelected.has(item.local);check.disabled=duplicateBusy;
    check.setAttribute('aria-label','Выбрать копию '+item.local);
    check.onchange=()=>{duplicateConfirm.hidden=true;if(check.checked)duplicateSelected.set(item.local,item);else duplicateSelected.delete(item.local);duplicateSelection();};
    label.append(check,document.createTextNode(item.local));row.append(label,node('p','Останется: '+item.canonical),node('p',(item.type==='original'?'Оригинал':'Превью')+' · '+bytes(item.size)));duplicateRows.append(row);
  }
  if(!result.items.length)duplicateRows.append(node('p','Лишних копий в списке нет. Для поиска включите защиту от дубликатов и проверьте папку при дозагрузке.'));
  duplicateBack.disabled=duplicateBusy||!offset;duplicateNext.disabled=duplicateBusy||!result.has_more;duplicateRefresh.disabled=duplicateBusy;
  duplicateCancel.hidden=!cleanup.busy||cleanup.job!==id;duplicatePage.textContent='В списке: '+result.total+(result.items.length?' · '+(offset+1)+'–'+(offset+result.items.length):'');duplicateSelection();
  if(cleanup.busy)duplicatePolling=setTimeout(()=>safely(loadDuplicates),1000);
}
duplicateRemove.onclick=()=>{duplicateConfirm.hidden=false;duplicateConfirmText.textContent='Переместить выбранные копии ('+duplicateSelected.size+') в корзину Windows? Указанные выше оставшиеся файлы сохранятся. Перед очисткой содержимое проверяется заново. Изменённые или занятые файлы останутся на месте.';duplicateYes.focus();};
duplicateNo.onclick=()=>{duplicateConfirm.hidden=true;};
duplicateYes.onclick=()=>safely(async()=>{
  if(duplicateBusy||!duplicateSelected.size)return;
  duplicateBusy=true;duplicateConfirm.hidden=true;duplicateSelection();
  try{await api('/api/jobs/'+duplicateJob+'/duplicates',{confirmation:'recycle-selected',items:[...duplicateSelected.values()]});duplicateSelected.clear();}
  finally{await loadDuplicates();}
});
duplicateCancel.onclick=()=>safely(async()=>{await api('/api/jobs/'+duplicateJob+'/duplicates',{action:'cancel'});await loadDuplicates();});
duplicateRefresh.onclick=()=>safely(loadDuplicates);
duplicateBack.onclick=()=>{duplicateOffset=Math.max(0,duplicateOffset-100);duplicateConfirm.hidden=true;safely(loadDuplicates);};
duplicateNext.onclick=()=>{duplicateOffset+=100;duplicateConfirm.hidden=true;safely(loadDuplicates);};
duplicateClose.onclick=()=>duplicateDialog.close();duplicateDialog.addEventListener('close',()=>{++duplicateRevision;clearTimeout(duplicatePolling);duplicateConfirm.hidden=true;});
duplicateDialog.append(node('h2','Дубликаты'),node('p','Показываем только копии из описи загрузки. Отметьте лишние файлы и нажмите кнопку переноса в корзину. Одна проверенная копия останется; если корзина недоступна, файл сохраняется.','hint'),duplicateStatus,duplicateRows,duplicatePage,duplicateBack,duplicateNext,duplicateRefresh,duplicateRemove,duplicateConfirm,duplicateCancel,duplicateClose);document.body.append(duplicateDialog);
const reviewDuplicates=toolButton('Дубликаты',async()=>{if(!state.job)return;duplicateJob=state.job;duplicateOffset=0;duplicateSelected.clear();duplicateConfirm.hidden=true;duplicateDialog.showModal();await loadDuplicates();});reviewDuplicates.id='review-duplicates';$('job-details').before(reviewDuplicates);

const problemDialog=node('dialog',undefined,'tools-dialog');problemDialog.id='problem-groups';const problemRows=node('div');
const closeProblems=node('button','Закрыть');closeProblems.onclick=()=>problemDialog.close();problemDialog.append(node('h2','Что мешает завершить загрузку'),problemRows,toolButton('Открыть диагностику',async()=>{problemDialog.close();$('open-service').click();}),closeProblems);document.body.append(problemDialog);
const groupedProblems=toolButton('Разобрать проблемы',async()=>{
  if(!state.job)return;const id=state.job;problemRows.textContent='Собираем причины…';problemDialog.showModal();
  const result=await api('/api/jobs/'+id+'/problems');if(state.job!==id)return;problemRows.replaceChildren();
  for(const group of result.groups){
    const row=node('div',undefined,'tool-row');row.append(node('strong',group.source+' · файлов: '+group.files),node('p',group.error));
    if(group.action==='connect')row.append(toolButton('Подключить Диск',async()=>{problemDialog.close();$('auth-form').hidden=false;$('token').focus();}));
    if(group.action==='folder')row.append(toolButton('Открыть папку результатов',async()=>api('/api/jobs/'+id+'/open-folder',{})));
    const retry=toolButton('Повторить неудачные файлы',async()=>{await action('retry');problemDialog.close();});retry.disabled=state.jobStatus?.live||!state.connected;row.append(retry);
    row.append(toolButton('Диагностика',async()=>{problemDialog.close();$('open-service').click();}));
    row.append(toolButton('Посмотреть файлы',async()=>{problemDialog.close();fileJob=id;fileOffset=0;fileFilter.value='problems';fileDialog.showModal();await loadTaskFiles();}));problemRows.append(row);
  }
  if(!result.groups.length)problemRows.append(node('p','Ошибок файлов нет. Если приложение остановилось, проверьте сообщение в карточке задания и раздел диагностики.'));
});groupedProblems.id='grouped-problems';$('errors-detail').before(groupedProblems);

const previousExperience=renderExperience;
renderExperience=function(job){
  previousExperience(job);renderExclusionChoices();
  if(lastJobId!==job.id){lastJobId=job.id;verifySelect.value=job.settings.verification_mode||'fast';duplicateCheck.checked=!!job.settings.deduplicate;lastMovement=Date.now();}
  const movement=JSON.stringify([job.phase,job.processed,job.verification?.checked,job.active.map(file=>[file.path,file.done,file.size])]);
  if(movement!==movementKey){movementKey=movement;lastMovement=Date.now();}
  const message=job.network_waiting?job.network_warning:job.pause_reason||job.health?.warning||job.error||({'ready':'Проверьте план ниже и нажмите «Начать скачивание».','paused':'Работа на паузе. Нажмите «Продолжить», когда будете готовы.','scanning':'Получаем список с Яндекс.Диска. Скачивание ещё не началось.','preparing':'Проверяем скачанные файлы и готовим план. Журналы и отчёты не перечитываются как контент.','downloading':'Сохраняем выбранные файлы. Можно поставить на паузу или остановить с сохранением прогресса.','packing':'Скачивание окончено. Собираем ZIP из сохранённых результатов.'}[job.phase]||phases[job.phase]);
  const stage=JSON.stringify([job.id,message,job.queue_state,job.verification?.cached,job.verification?.hashed]);
  if(stage!==stageKey){stageKey=stage;taskStage.replaceChildren(node('strong',message));if(job.verification)taskStage.append(node('p','Без повторного чтения: '+(job.verification.cached||0)+' · Проверено по содержимому: '+(job.verification.hashed||0),'hint'));}
  groupedProblems.hidden=!(job.errors||job.scan_errors||job.error);
  safely(async()=>{try{await showPlan(job);}catch(error){planKey='';throw error;}});
  if(!job.live&&['completed','completed_with_errors','stopped'].includes(job.phase)&&!resultActions.querySelector('[data-result-folder]')){
    const open=toolButton('Открыть результаты',async()=>api('/api/jobs/'+job.id+'/open-folder',{}));open.dataset.resultFolder='true';resultActions.prepend(open);
  }
};

$('apply-profile').addEventListener('click',()=>{const profile=profiles.find(item=>item.name===$('profile-list').value);if(profile&&!profile.error)verifySelect.value=profile.settings.verification_mode||'fast';renderExclusionChoices();});
