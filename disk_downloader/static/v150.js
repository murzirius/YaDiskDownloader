'use strict';
if(['Лес','Слива'].includes(currentTheme.name)&&!installedThemes.some(theme=>theme.name===currentTheme.name))useTheme(baseThemes[0]);

// Every interface entry uses textContent; untrusted names never become markup.
const folderTools=node('div',undefined,'folder-count-tools'),countFolders=node('button','Посчитать папки'),cancelFolderCount=node('button','Остановить подсчёт'),folderCountStatus=node('span','Для папок показываем общий размер и дату последнего изменённого файла, включая вложенные папки.');
cancelFolderCount.hidden=true;folderTools.append(countFolders,cancelFolderCount,folderCountStatus);$('file-list').before(folderTools);
let folderStatsTimer=null;
async function updateFolderCounts(){
  const result=await api('/api/folder-stats');let changed=false;
  for(const item of state.items){const value=result.items[item.path];if(item.type==='dir'&&value&&JSON.stringify(item.folder_stats)!==JSON.stringify(value)){item.folder_stats=value;item.size=value.bytes;item.modified=value.latest_modified;changed=true;}}
  if(changed)renderFiles();
  countFolders.disabled=result.busy;cancelFolderCount.hidden=!result.busy;
  folderCountStatus.textContent=result.busy?'Считаем вложенные файлы: '+result.files.toLocaleString('ru-RU')+'. Просмотр и управление доступны.':result.error||'Размеры — сумма файлов во всех вложенных папках. «≥» означает неполные данные. Дата — самое позднее изменение файла; для пустых папок даты нет. Результат относится к моменту подсчёта.';
  clearTimeout(folderStatsTimer);if(result.busy)folderStatsTimer=setTimeout(()=>safely(updateFolderCounts),document.hidden?5000:1500);
}
countFolders.onclick=()=>safely(async()=>{const paths=state.items.filter(item=>item.type==='dir').map(item=>item.path);if(!paths.length)throw Error('На этой странице нет папок для подсчёта.');await api('/api/folder-stats',{paths});await updateFolderCounts();});
cancelFolderCount.onclick=()=>safely(async()=>{await api('/api/folder-stats',{action:'cancel'});await updateFolderCounts();});

const preferences=node('div',undefined,'v150-preferences');
function preference(label,control){const holder=node('label',label);holder.append(control);preferences.append(holder);return control;}
function savedPreference(key,fallback){try{return localStorage.getItem(key)||fallback;}catch{return fallback;}}
const appearance=node('select');for(const [value,label] of [['manual','Выбранная тема'],['system','Как в системе']])appearance.append(new Option(label,value));appearance.value=savedPreference('yadisk-theme-mode','manual');preference('Переключение темы',appearance);
const fontScale=node('select');for(const value of ['1','1.1','1.2','1.3'])fontScale.append(new Option(Math.round(Number(value)*100)+'%',value));fontScale.value=savedPreference('yadisk-font-scale','1');preference('Размер текста',fontScale);
const accent=node('input');accent.type='color';accent.value=currentTheme.colors.accent;preference('Акцентный цвет',accent);
const simple=node('input');simple.type='checkbox';simple.checked=savedPreference('yadisk-simple','true')==='true';preference('Простой режим — дополнительные параметры скрыты',simple);
const compact=node('input');compact.type='checkbox';compact.checked=savedPreference('yadisk-compact','false')==='true';preference('Компактный список',compact);
themeDialog.insertBefore(preferences,themeChoices);
function persist(key,value){try{localStorage.setItem(key,String(value));}catch{notice('Настройка действует до закрытия страницы: браузер запретил её сохранение.');}}
function applyInterfacePreferences(){document.documentElement.style.setProperty('--ui-scale',fontScale.value);document.body.classList.toggle('v150-simple',simple.checked);document.body.classList.toggle('v150-compact',compact.checked);}
fontScale.onchange=()=>{persist('yadisk-font-scale',fontScale.value);applyInterfacePreferences();};simple.onchange=()=>{persist('yadisk-simple',simple.checked);applyInterfacePreferences();};compact.onchange=()=>{persist('yadisk-compact',compact.checked);applyInterfacePreferences();};applyInterfacePreferences();
const extraSettings=node('button',simple.checked?'Показать все параметры':'Скрыть дополнительные параметры','quiet wide');$('settings-panel').querySelector('h1').after(extraSettings);const changeSimple150=simple.onchange;simple.onchange=()=>{changeSimple150();extraSettings.textContent=simple.checked?'Показать все параметры':'Скрыть дополнительные параметры';};extraSettings.onclick=()=>{simple.checked=!simple.checked;simple.onchange();};
const mediaTheme=matchMedia('(prefers-color-scheme: dark)');
function followSystem(){if(appearance.value==='system')useTheme(baseThemes.find(theme=>theme.name===(mediaTheme.matches?'Catppuccin Mocha':'Catppuccin Latte')),false);}
appearance.onchange=()=>{persist('yadisk-theme-mode',appearance.value);followSystem();};mediaTheme.addEventListener('change',followSystem);followSystem();
accent.onchange=()=>safely(()=>useTheme({name:currentTheme.name,colors:{...currentTheme.colors,accent:accent.value}}));

const dates=node('details',undefined,'date-settings'),datesSummary=node('summary','Период и приоритет загрузки');dates.append(datesSummary);
function dateInput(label,type='date'){const holder=node('label',label,'field'),input=node('input');input.type=type;holder.append(input);dates.append(holder);return input;}
const dateFrom=dateInput('С даты'),dateTo=dateInput('По дату включительно');
const dateField=node('select');dateField.append(new Option('Изменение файла','modified'),new Option('Создание файла','created'),new Option('Дата съёмки, если Яндекс её передал','taken'));const dateLabel=node('label','Какую дату учитывать','field');dateLabel.append(dateField);dates.append(dateLabel);
const unknownDates=node('input');unknownDates.type='checkbox';unknownDates.checked=true;const unknownLabel=node('label',undefined,'check');unknownLabel.append(unknownDates,document.createTextNode('Включать файлы без выбранной даты'));dates.append(unknownLabel);
const priorityPaths=node('textarea');priorityPaths.rows=3;priorityPaths.placeholder='disk:/Семейные фото\ndisk:/Документы';const priorityLabel=node('label','Сначала скачать эти папки — по одному пути на строку','field');priorityLabel.append(priorityPaths);dates.append(priorityLabel,node('p','Сначала обрабатываются файлы без готовой копии; внутри этой группы — приоритетные папки. Фильтр дат не изменяет файлы на Яндекс.Диске.','hint'));
const prioritizeSelection=node('button','Сделать выбранные папки приоритетными');prioritizeSelection.onclick=()=>{priorityPaths.value=[...selected].join('\n');notice('Приоритет добавлен. Убедитесь, что эти папки входят в выбор загрузки.');};dates.append(prioritizeSelection);stepBoxes[0].append(dates);
const verificationOptions=node('details',undefined,'advanced-settings');verificationOptions.append(node('summary','Проверка файлов и защита от дубликатов'));
const duplicateExplanation=duplicateField.nextElementSibling;
verificationOptions.append(verifyLabel,verificationHint,duplicateField);
if(duplicateExplanation?.tagName==='P')verificationOptions.append(duplicateExplanation);
stepBoxes[2].insertBefore(verificationOptions,confirmation);
const oldAdvanced150=advancedSettings;advancedSettings=function(){return {...oldAdvanced150(),date_from:dateFrom.value,date_to:dateTo.value,date_field:dateField.value,include_unknown_dates:unknownDates.checked,priority_paths:priorityPaths.value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean)};};
const oldApply150=applyDownloadSettings;applyDownloadSettings=function(value){dateFrom.value=value.date_from||'';dateTo.value=value.date_to||'';dateField.value=value.date_field||'modified';unknownDates.checked=value.include_unknown_dates!==false;priorityPaths.value=(value.priority_paths||[]).join('\n');return oldApply150(value);};

const liveDetails=node('details'),liveSummary=node('summary','Изменить скорость и потоки'),liveControls=node('div',undefined,'live-controls');liveDetails.append(liveSummary,liveControls);
function liveNumber(label,min,max,step){const holder=node('label',label,'field'),input=node('input');Object.assign(input,{type:'number',min:String(min),max:String(max),step:String(step)});holder.append(input);liveControls.append(holder);return input;}
const liveWorkers=liveNumber('Потоки',1,20,1),liveSpeed=liveNumber('Общая скорость, МиБ/с · 0 без лимита',0,10000,.1),applyLive=node('button','Применить');liveControls.append(applyLive);$('job-details').before(liveDetails);liveWorkers.value='8';liveSpeed.value='0';
liveDetails.addEventListener('toggle',()=>{if(liveDetails.open&&state.jobStatus){liveWorkers.value=state.jobStatus.settings.workers;liveSpeed.value=state.jobStatus.settings.speed_limit_mib||0;}});
applyLive.onclick=()=>safely(async()=>{if(!state.job)throw Error('Выберите задание.');const applied=await api('/api/jobs/'+state.job+'/limits',{workers:Number(liveWorkers.value),speed_limit_mib:Number(liveSpeed.value)});$('workers').value=applied.workers;$('speed-limit').value=applied.speed_limit_mib;state.renderKey=null;await pollJob();notice('Ограничения применены. При уменьшении потоков текущие файлы завершатся, новые начнутся в пределах нового лимита.');});

const doctorDialog=node('dialog',undefined,'tools-dialog'),doctorTitle=node('h2','Проверка приложения'),doctorBody=node('div'),doctorClose=node('button','Закрыть'),doctorAgain=node('button','Проверить ещё раз');doctorClose.onclick=()=>doctorDialog.close();doctorDialog.append(doctorTitle,doctorBody,doctorAgain,doctorClose);document.body.append(doctorDialog);
let doctorTimer=null;
async function showDoctor(start=false){const result=await api('/api/doctor',start?{}:undefined);doctorBody.replaceChildren();for(const check of result.checks){const row=node('div',check.source+': '+check.message,'doctor-check');row.dataset.ok=String(check.ok);doctorBody.append(row);}if(result.busy)doctorBody.append(node('p','Проверяем историю в фоне…'));clearTimeout(doctorTimer);if(result.busy&&doctorDialog.open)doctorTimer=setTimeout(()=>safely(()=>showDoctor()),1000);}
const doctorButton=node('button','Проверить приложение');doctorButton.onclick=()=>safely(async()=>{doctorDialog.showModal();await showDoctor(true);});doctorAgain.onclick=()=>safely(()=>showDoctor(true));$('open-diagnostics').before(doctorButton);

const chartDetails=node('details'),chartTitle=node('summary','График работы за последние 5 минут'),chart=document.createElementNS('http://www.w3.org/2000/svg','svg'),chartLine=document.createElementNS('http://www.w3.org/2000/svg','polyline'),chartStatus=node('p','Наблюдение начнётся при открытии графика.','hint');chart.classList.add('work-chart');chart.setAttribute('viewBox','0 0 300 100');chart.setAttribute('role','img');chart.setAttribute('aria-label','График скорости передачи данных');chart.append(chartLine);chartDetails.append(chartTitle,chart,chartStatus);$('job-details').before(chartDetails);
const chartSamples=[];let chartJob=null;
function updateWorkChart(job){if(document.hidden||!chartDetails.open)return;if(chartJob!==job.id){chartSamples.length=0;chartJob=job.id;}const last=chartSamples.at(-1);if(last&&last.time>Date.now()-5000)return;const now=Date.now(),previous=chartSamples.at(-1),transferred=job.transferred||0,speed=previous?Math.max(0,transferred-previous.transferred)/Math.max(.1,(now-previous.time)/1000):0;chartSamples.push({time:now,transferred,speed});while(chartSamples.length&&chartSamples[0].time<Date.now()-300000)chartSamples.shift();const maximum=Math.max(1,...chartSamples.map(sample=>sample.speed));chartLine.setAttribute('points',chartSamples.map((sample,index)=>`${chartSamples.length===1?0:index/(chartSamples.length-1)*300},${95-sample.speed/maximum*90}`).join(' '));chartStatus.textContent=(phases[job.phase]||job.phase)+' · '+bytes(speed)+'/с · пик '+bytes(maximum)+'/с. Скорость — переданные байты за интервал наблюдения.';}


const scheduleDetails=node('details',undefined,'schedule-settings');scheduleDetails.append(node('summary','Расписание, тихие часы и сон устройства'));
function scheduleField(label,type){const holder=node('label',label,'field'),input=node('input');input.type=type;holder.append(input);scheduleDetails.append(holder);return input;}
const scheduledAt=scheduleField('Начать загрузку не раньше (местное время)','datetime-local'),quietEnabled=scheduleField('Включить тихие часы','checkbox'),quietStart=scheduleField('Тихие часы: начало (час 0–23)','number'),quietEnd=scheduleField('Окончание (час 0–23)','number'),quietSpeed=scheduleField('Скорость в тихие часы, МиБ/с; 0 без лимита','number'),quietMute=scheduleField('Не показывать системные уведомления в тихие часы','checkbox'),preventSleep=scheduleField('Не давать устройству заснуть во время работы','checkbox');
quietStart.min=quietEnd.min='0';quietStart.max=quietEnd.max='23';quietStart.value='23';quietEnd.value='8';quietSpeed.min='0';quietSpeed.max='10000';quietSpeed.step='.1';quietSpeed.value='0';quietMute.checked=true;
scheduleDetails.append(node('p','Расписание действует для задания в очереди, пока приложение запущено и ключ подключён. После перезапуска нужно подключить ключ заново. Компьютер не включится самостоятельно. Запрет сна снимается на паузе и после завершения.','hint'));
stepBoxes[2].append(scheduleDetails);
const settingsBeforeSchedule=advancedSettings;advancedSettings=function(){return {...settingsBeforeSchedule(),run_at:scheduledAt.value?new Date(scheduledAt.value).toISOString():'',quiet_hours:{enabled:quietEnabled.checked,start:Number(quietStart.value),end:Number(quietEnd.value),speed_limit_mib:Number(quietSpeed.value),mute:quietMute.checked},prevent_sleep:preventSleep.checked};};
const applyBeforeSchedule=applyDownloadSettings;applyDownloadSettings=function(value){const quiet=value.quiet_hours||{};if(value.run_at){const stamp=new Date(value.run_at);scheduledAt.value=new Date(stamp-stamp.getTimezoneOffset()*60000).toISOString().slice(0,16);}else scheduledAt.value='';quietEnabled.checked=!!quiet.enabled;quietStart.value=quiet.start??23;quietEnd.value=quiet.end??8;quietSpeed.value=quiet.speed_limit_mib||0;quietMute.checked=quiet.mute!==false;preventSleep.checked=!!value.prevent_sleep;return applyBeforeSchedule(value);};
const refreshSource=node('button','Проверить новые и изменённые файлы');refreshSource.onclick=()=>safely(async()=>{if(!state.job)throw Error('Выберите прежнее задание.');const result=await api('/api/jobs/'+state.job+'/refresh-source',{});await openJob(result.id);notice('Запущен новый обход с прежним выбором. После проверки появится отчёт об изменениях; скачивание начнётся по вашей команде.');});$('show-changes').after(refreshSource);

const useThemeBefore150=useTheme;
useTheme=function(theme,save=true){useThemeBefore150(theme,save);accent.value=theme.colors.accent;const rgb=[1,3,5].map(i=>parseInt(theme.colors.accent.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);document.documentElement.style.setProperty('--accent-text',rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722>.179?'#000000':'#ffffff');if(save){appearance.value='manual';persist('yadisk-theme-mode','manual');}for(const button of themeChoices.querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.textContent===theme.name));};
useTheme(currentTheme,false);

const experienceBefore150=renderExperience;
renderExperience=function(job){experienceBefore150(job);updateWorkChart(job);};

const actionBeforeScheduled150=action;
action=async function(verb){const job=state.jobStatus,stamp=job?.settings.run_at?new Date(job.settings.run_at):null;if(verb==='start'&&stamp&&stamp.getTime()>Date.now()){if(state.actionPending)return;state.actionPending=true;$('start-job').disabled=true;try{await api('/api/queue',{action:'add',job:job.id});state.renderKey=null;notice('Задание в очереди. Начнём не раньше '+stamp.toLocaleString('ru-RU')+'. Оставьте приложение запущенным и ключ подключённым.');await pollJob();}finally{state.actionPending=false;state.renderKey=null;}return;}return actionBeforeScheduled150(verb);};
const renderBeforeScheduled150=renderExperience;
renderExperience=function(job){renderBeforeScheduled150(job);if(job.phase!=='ready')return;const stamp=job.settings.run_at?new Date(job.settings.run_at):null;if(stamp&&stamp.getTime()>Date.now()){$('start-job').textContent='В очередь на '+stamp.toLocaleString('ru-RU');if(job.queue_state!=='waiting'&&taskStage.textContent!=='Задание подготовлено. Нажмите кнопку «В очередь на…», чтобы включить отложенный запуск.')taskStage.replaceChildren(node('strong','Задание подготовлено. Нажмите кнопку «В очередь на…», чтобы включить отложенный запуск.'));}if(job.queue_state==='waiting'){$('start-job').textContent='Задание в очереди';$('start-job').disabled=true;taskStage.replaceChildren(node('strong',stamp&&stamp.getTime()>Date.now()?'Запланировано: не раньше '+stamp.toLocaleString('ru-RU'):'Ждём своей очереди'),node('p','Порядок и пауза очереди доступны в «Инструменты → Очередь». Приложение должно работать, ключ — оставаться подключённым.','hint'));}};
const setupBeforeScheduled150=renderSetup;
renderSetup=function(){setupBeforeScheduled150();if(scheduledAt.value)confirmation.append(node('p','Запуск через очередь не раньше '+new Date(scheduledAt.value).toLocaleString('ru-RU')));};
scheduledAt.addEventListener('change',()=>renderSetup());
