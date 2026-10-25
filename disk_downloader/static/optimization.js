const economyMode=node('input');economyMode.type='checkbox';economyMode.id='economy-mode';
const earlyDownload=node('input');earlyDownload.type='checkbox';earlyDownload.id='early-download';
$('auto-start').closest('label').after(toolField('Экономить запросы: использовать свежий список для проверки оригиналов',economyMode),toolField('Начинать загрузку готовых страниц до конца обхода',earlyDownload),node('p','Ранний старт — только для новой загрузки без поиска дубликатов и приоритетных папок. Дозагрузка сначала полностью проверяет папку. Размеры и контрольные суммы оригиналов проверяются всегда.','hint'));
// Optional performance controls; existing download defaults stay unchanged.
const verifyThreads=node('select');verifyThreads.id='verification-workers';
for(const [value,label] of [[1,'1 — бережно для жёсткого диска'],[2,'2 — умеренно'],[4,'4 — быстрее для SSD и сети']])verifyThreads.append(new Option(label,value));
verifyThreads.value='4';
$('speed-limit').closest('label').before(toolField('Потоки проверки файлов на устройстве',verifyThreads));
const settingsBeforePerformance=advancedSettings;
advancedSettings=function(){return {...settingsBeforePerformance(),verification_workers:Number(verifyThreads.value),economy_mode:economyMode.checked,early_download:earlyDownload.checked};};
const applyBeforePerformance=applyDownloadSettings;
applyDownloadSettings=function(value){verifyThreads.value=String(value.verification_workers||4);economyMode.checked=!!value.economy_mode;earlyDownload.checked=!!value.early_download;return applyBeforePerformance(value);};
const finishCurrent=toolButton('Завершить текущие файлы и остановиться',async()=>{
  await api('/api/jobs/'+state.job+'/action',{action:'finish_current'});state.renderKey=null;await pollJob();
});finishCurrent.id='finish-current';finishCurrent.hidden=true;
$('stop-job').after(finishCurrent);
const trafficDetails=node('details');trafficDetails.className='advanced-settings';
const trafficSummary=node('summary','Трафик и скорость работы');
const trafficLimit=toolInput('number','0');trafficLimit.min='0';trafficLimit.max='1000000';trafficLimit.step='1';trafficLimit.id='traffic-limit';
const trafficStats=node('p',undefined,'hint');trafficStats.id='traffic-stats';
trafficDetails.append(trafficSummary,toolField('Дневной лимит трафика, МБ',trafficLimit),node('p','0 — без ограничения. Лимит общий для загрузок с Яндекс.Диска и миниатюр. Обновления программы и служебный трафик сети не входят. После сбоя возможен запас до 1 МБ. При достижении лимита задание встанет на паузу.','hint'),toolButton('Сохранить лимит',async()=>{await api('/api/traffic',{limit_mb:Number(trafficLimit.value)});notice('Лимит сохранён. Для продолжения задания после паузы нажмите «Продолжить».');}),trafficStats);
$('job-details').before(trafficDetails);
let lastTraffic=0;
async function refreshTraffic(){if(!trafficDetails.open||document.hidden||Date.now()-lastTraffic<5000)return;lastTraffic=Date.now();const value=await api('/api/traffic');if(document.activeElement!==trafficLimit)trafficLimit.value=value.limit_mb;trafficStats.textContent='Сегодня: '+bytes(value.bytes)+' · запросов в этой сессии: '+value.requests+' · миниатюр в памяти: '+bytes(value.cache.bytes)+' / '+bytes(value.cache.limit)+'. Ожидание ответа сервера: '+Math.round(value.wait_seconds)+' с; получение данных: '+Math.round(value.receive_seconds)+' с. Проверка файлов на устройстве: '+Math.round(state.jobStatus?.timings?.local_check||0)+' с; запись и сохранение на диск: '+Math.round((state.jobStatus?.timings?.write||0)+(state.jobStatus?.timings?.flush||0))+' с. Время суммируется по потокам.';}
trafficDetails.addEventListener('toggle',()=>{if(trafficDetails.open){lastTraffic=0;safely(refreshTraffic);}});
const pollBeforePerformance=pollJob;
pollJob=async function(){await pollBeforePerformance();if(trafficDetails.open)await refreshTraffic();const job=state.jobStatus;if(job&&state.restoreLimits&&state.performanceJob!==job.id){state.performanceJob=job.id;verifyThreads.value=String(job.settings.verification_workers||4);economyMode.checked=!!job.settings.economy_mode;earlyDownload.checked=!!job.settings.early_download;}finishCurrent.hidden=!job?.live||job.phase!=='downloading';finishCurrent.disabled=!!job?.finishing_current||state.actionPending;finishCurrent.textContent=job?.finishing_current?'Завершаем текущие файлы…':'Завершить текущие файлы и остановиться';};
// Find settings without having to know which panel contains them.
const findSettings=node('input');findSettings.type='search';findSettings.placeholder='Найти настройку: скорость, ZIP, потоки…';findSettings.setAttribute('aria-label','Найти настройку');
const foundSettings=node('div');foundSettings.className='settings-search-results';foundSettings.hidden=true;
toolsBody.before(findSettings,foundSettings);
findSettings.oninput=()=>{foundSettings.replaceChildren();const query=findSettings.value.trim().toLocaleLowerCase('ru');foundSettings.hidden=!query;if(!query)return;
  const labels=[...document.querySelectorAll('#settings-panel label')];
  for(const label of labels.filter(item=>item.textContent.toLocaleLowerCase('ru').includes(query)).slice(0,12)){
    const control=label.querySelector('input,select,textarea,button');if(!control||control===findSettings)continue;
    const caption=label.cloneNode(true);caption.querySelectorAll('input,select,textarea,.custom-select').forEach(item=>item.remove());foundSettings.append(toolButton(caption.textContent.trim().slice(0,100),async()=>{findSettings.closest('dialog')?.close();welcome.hidden=true;workspace.hidden=false;showPanel('settings');wizardStep=Number(control.closest('[id^=setup-step-]')?.id.split('-').pop())||3;renderSetup();for(let p=control.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;syncSelects();const focusControl=control.closest('.custom-select')?.querySelector('.select-choice')||control;focusControl.scrollIntoView({block:'center',behavior:'smooth'});focusControl.focus({preventScroll:true});}));
  }
  for(const button of [...toolsTabs.querySelectorAll('button')].filter(item=>item.textContent.toLocaleLowerCase('ru').includes(query)))foundSettings.append(toolButton('Инструмент: '+button.textContent,async()=>button.click()));
  if(!foundSettings.childElementCount)foundSettings.append(node('p','Не нашли. Попробуйте «скорость», «потоки» или «ZIP».','hint'));
};

const trafficDialog=node('dialog',undefined,'tools-dialog');trafficDialog.id='traffic-dialog';const trafficClose=toolButton('Закрыть',async()=>trafficDialog.close());trafficDialog.append(node('h2','Трафик и скорость работы'),trafficClose);document.body.append(trafficDialog);
trafficDialog.addEventListener('close',()=>{$('job-details').before(trafficDetails);trafficDetails.open=false;});
const openTraffic=toolButton('Трафик',async()=>{toolsDialog.close();trafficDialog.insertBefore(trafficDetails,trafficClose);trafficDialog.showModal();trafficDetails.open=true;lastTraffic=0;await refreshTraffic();});openTraffic.id='open-traffic';toolsTabs.append(openTraffic);
const bottleneck=node('p',undefined,'hint');bottleneck.id='bottleneck';trafficDetails.append(bottleneck);
const refreshTrafficBeforeCause=refreshTraffic;
refreshTraffic=async function(){await refreshTrafficBeforeCause();const job=state.jobStatus,t=job?.timings||{};if(!job?.live){bottleneck.textContent='Подсказка о скорости появится во время работы задания.';return;}
 const network=(t.server||0)+(t.receive||0),local=(t.write||0)+(t.flush||0)+(t.local_check||0);
 bottleneck.textContent=network+local<2?'Собираем замеры.':local>network*1.5?'По замерам больше времени занимает устройство: проверка или запись файлов. Для жёсткого диска попробуйте 1–2 потока проверки.':t.server>(t.receive||0)*1.5?'По замерам больше времени занимает ожидание ответа сервера. Проверьте предупреждения Яндекс.Диска; при ограничениях уменьшите потоки.':'По замерам больше времени занимает получение данных из сети. Проверьте ограничение скорости и соединение. Это приблизительная оценка, время потоков суммируется.';
};

const installDialog=node('dialog',undefined,'tools-dialog');installDialog.id='windows-install-dialog';const installInfo=node('p',undefined,'hint');
const installAction=toolButton('Установить и добавить в «Пуск»',async()=>{const result=await api('/api/windows-install',{});installInfo.textContent=result.message;installAction.disabled=result.busy;checkInstall();});
let installTimer;
async function checkInstall(){if(!installDialog.open)return;const value=await api('/api/windows-install');installAction.hidden=!value.available;installAction.disabled=value.busy;installInfo.textContent=value.message||(!value.available?'Установка доступна в готовом Windows EXE.':'ZIP нужно распаковать целиком. Установим программу для текущего пользователя, без прав администратора.');if(value.busy)installTimer=setTimeout(()=>safely(checkInstall),1000);}
installDialog.append(node('h2','Установить на этот ПК'),node('p','Программа будет храниться в папке приложений текущего пользователя. Скачанные файлы, история и профили останутся на месте. Для удаления этой копии закройте приложение и удалите его папку и ярлык; историю и папки загрузок удалять не нужно.','hint'),installInfo,installAction,toolButton('Закрыть',async()=>installDialog.close()));
installDialog.addEventListener('close',()=>clearTimeout(installTimer));document.body.append(installDialog);
const installTool=toolButton('Установить на ПК',async()=>{toolsDialog.close();installDialog.showModal();await checkInstall();});installTool.id='install-windows';toolsTabs.append(installTool);

let trafficTimer;
async function tickTraffic(){if(!trafficDialog.open)return;await safely(refreshTraffic);trafficTimer=setTimeout(tickTraffic,5000);}
trafficDialog.addEventListener('close',()=>clearTimeout(trafficTimer));
const openTrafficOriginal=openTraffic.onclick;
openTraffic.onclick=async()=>{await openTrafficOriginal();if(trafficDialog.open){clearTimeout(trafficTimer);trafficTimer=setTimeout(tickTraffic,5000);}};
