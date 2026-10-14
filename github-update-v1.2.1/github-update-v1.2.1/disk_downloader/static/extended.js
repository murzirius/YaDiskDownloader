let inventoryVersion=0;
let inventory={mode:'folders',path:'disk:/',offset:0,job:null,more:false};
for(const id of ['speed-limit','exclusions'])$(id).onchange=()=>{state.limitsTouched=true;};
$('exclude-selected').onclick=()=>{if(!selected.size){notice('Выберите файлы или папки в списке');return;}excludePaths([...selected]);};
$('close-inventory').onclick=()=>$('inventory-dialog').close();
async function renderInventory(){
  const version=++inventoryVersion;
  const changes=inventory.mode==='changes';$('inventory-title').textContent=changes?'Отчёт об изменениях':'Объём по папкам';$('change-filter').hidden=!changes;$('open-changes-report').hidden=!changes||!state.jobStatus?.settings.changes_local;
  const url='/api/jobs/'+inventory.job+(changes?'/changes?':'/folders?')+new URLSearchParams({path:inventory.path,offset:inventory.offset,status:$('change-filter').value||'all'});
  const result=await api(url);if(version!==inventoryVersion)return;inventory.more=result.has_more;$('inventory-prev').disabled=inventory.offset===0;$('inventory-next').disabled=!inventory.more;$('inventory-nav').replaceChildren();$('inventory-head').replaceChildren();$('inventory-body').replaceChildren();
  if(changes){
    $('inventory-note').textContent=(result.summary.compared?'Сравнение с предыдущим импортом':'Первый импорт: предыдущего обхода для сравнения пока нет')+(!result.summary.complete?' · Обход ещё не завершён':'');
    const choice=$('change-filter').value||'all';$('change-filter').replaceChildren(new Option('Все изменения','all'));for(const [key,label] of Object.entries(result.summary.labels))$('change-filter').append(new Option(label+' · '+result.summary.counts[key],key));$('change-filter').value=choice;
    const head=node('tr');for(const text of ['Изменение','Размер: до → после','Путь'])head.append(node('th',text));$('inventory-head').append(head);
    for(const item of result.items){const row=node('tr');row.append(node('td',result.summary.labels[item.status]),node('td',bytes(item.before_size)+' → '+bytes(item.after_size)),node('td',item.path));$('inventory-body').append(row);}
  }else{
    $('inventory-note').textContent='Объём исходных оригиналов; размер превью заранее неизвестен. Данные уточняются во время обхода.';
    const up=node('button','↑ К родительской папке');up.disabled=inventory.path==='disk:/';up.onclick=()=>safely(async()=>{inventory.path=inventory.path.replace(/\/[^/]+$/,'')||'disk:/';if(inventory.path==='disk:')inventory.path='disk:/';inventory.offset=0;await renderInventory();});$('inventory-nav').append(up,node('span',inventory.path,'hint'));
    const head=node('tr');for(const text of ['Папка','Файлы','Объём'])head.append(node('th',text));$('inventory-head').append(head);
    for(const item of result.items){const row=node('tr'),cell=node('td');if(item.direct)cell.textContent='Файлы этой папки';else{const button=node('button',item.path.slice(6),'name-button');button.onclick=()=>safely(async()=>{inventory.path=item.path;inventory.offset=0;await renderInventory();});cell.append(button);}row.append(cell,node('td',String(item.files)),node('td',bytes(item.known_bytes)+(item.unknown_sizes?' + неизвестно ('+item.unknown_sizes+')':'')));$('inventory-body').append(row);}
  }
  if(!result.items.length){const row=node('tr'),cell=node('td','Нет файлов для отображения','hint');cell.colSpan=3;row.append(cell);$('inventory-body').append(row);}
}
function openInventory(mode){return safely(async()=>{if(!state.job)throw new Error('Сначала оцените выбранное или выберите задание');inventory={mode,path:'disk:/',offset:0,job:state.job,more:false};$('change-filter').replaceChildren(new Option('Все изменения','all'));await renderInventory();if(!$('inventory-dialog').open)$('inventory-dialog').showModal();});}
$('folder-volumes').onclick=()=>openInventory('folders');$('show-changes').onclick=()=>openInventory('changes');$('change-filter').onchange=()=>safely(async()=>{inventory.offset=0;await renderInventory();});
$('inventory-prev').onclick=()=>safely(async()=>{inventory.offset=Math.max(0,inventory.offset-100);await renderInventory();});$('inventory-next').onclick=()=>safely(async()=>{inventory.offset+=100;await renderInventory();});
$('open-changes-report').onclick=()=>safely(()=>api('/api/jobs/'+inventory.job+'/open-changes',{}));
const issueButton=node('button','Сообщить об ошибке');issueButton.id='report-issue';$('service-dialog').querySelector('hr').before(issueButton);
const trayStatus=node('p','','hint');trayStatus.id='tray-status';const exitButton=node('button','Завершить приложение');exitButton.id='exit-app';exitButton.hidden=true;$('service-dialog').append(trayStatus,exitButton);
issueButton.onclick=()=>{if($('service-dialog').open)$('service-dialog').close();$('issue-preview').hidden=true;$('issue-dialog').showModal();};$('close-issue').onclick=()=>$('issue-dialog').close();
for(const id of ['issue-title','issue-description'])$(id).oninput=()=>{$('issue-preview').hidden=true;$('issue-link').removeAttribute('href');};
$('prepare-issue').onclick=()=>safely(async()=>{const result=await api('/api/issue-draft',{title:$('issue-title').value,description:$('issue-description').value,job_id:state.job});$('issue-content').textContent=result.title+'\n\n'+result.body;$('issue-link').href=result.url;$('issue-preview').hidden=false;});
exitButton.onclick=()=>safely(async()=>{await api('/api/exit',{});exitButton.disabled=true;trayStatus.textContent='Приложение завершается. Недокачанные части и история сохраняются.';});
api('/api/runtime').then(result=>{exitButton.hidden=!result.can_exit;trayStatus.textContent=result.tray.visible?'Загрузка продолжится после закрытия браузера. Управление доступно через значок в трее.':result.tray.error;if(result.migration?.jobs)trayStatus.textContent+=' Перенесено прежних заданий: '+result.migration.jobs+'.';if(result.migration?.failed_sources)trayStatus.textContent+=' Часть старых папок занята или недоступна; перенос повторится при следующем запуске.';}).catch(()=>{});
