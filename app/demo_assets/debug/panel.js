"use strict";
const $ = id => document.getElementById(id);
const phases = {
  ready:"Начало разговора",identifying:"Уточняем пациента",identified:"Пациент найден",offered:"Предложены варианты",held:"Ждём подтверждения записи",booked:"Запись создана",reconciling:"Проверяем результат МИС",handoff:"Передача сотруднику",ended:"Разговор завершён",interrupted:"Обработка прервана",verifying_owner:"Уточняем, чьи записи",visits_shown:"Показаны записи",cancel_choosing:"Выбираем запись для отмены",cancel_offer:"Обсуждаем отмену",cancel_confirm:"Ждём подтверждения отмены",cancel_reconciling:"Проверяем отмену",cancelled:"Запись отменена",move_choosing:"Выбираем запись для переноса",move_source:"Уточняем перенос",move_offered:"Выбираем новое время",move_confirm:"Ждём согласия на перенос",move_finish_confirm:"Подтверждаем завершение переноса",move_pending:"Проверяем перенос",rescheduled:"Запись перенесена",move_empty:"Нового времени пока нет"
}
;
const intents = {
  "I-01": "Запись на приём", "I-02": "Перенос записи", "I-03": "Отмена записи", "I-04": "Проверка своей записи", "I-05": "Вызов врача на дом", "I-06": "График врача или поликлиники", "I-07": "Готовность анализов", "I-08": "Платные услуги", "I-09": "Лист ожидания", "I-10": "Прикрепление, полис, документы и общие справки", "I-11": "Экстренная ситуация", "I-12": "Жалоба, конфликт", "I-13": "Медицинский вопрос", "I-14": "Прочее и нестандартное"
}
;
const slots = {
  specialty:"Специальность",doctor_id:"Врач",last_name:"Фамилия",first_name:"Имя",patronymic:"Отчество",middle_name:"Отчество",birth_date:"Дата рождения",date_from:"Дата с",date_to:"Дата по",time_from:"Время с",time_to:"Время по",answer:"Ответ",choice:"Выбор",choice_index:"Номер варианта",doctor:"Врач",time_pref:"Часть дня",on_behalf:"За другого человека",visit_ref:"Выбранная запись",policy_number:"Полис",exclude_weekdays:"Исключить дни недели",exclude_dates:"Исключить даты"
}
;
const answer = {
  yes:"Да",no:"Нет",choice:"Выбор варианта",maybe:"Не уверен",repeat:"Повторить",opt_out:"Отказ",free:"Свободная просьба",identity:"Данные пациента",confirmation:"Подтверждение",yes_no:"Да или нет",full_name:"ФИО пациента",birth_date:"Дата рождения",yes_no:"Да или нет",full_name:"ФИО пациента",birth_date:"Дата рождения"
}
;
let selected = null, cursor = 0, all = [], busy = false, generation = 0, lastTurn = null;
const node = (tag, cls, text) => {
  const n=document.createElement(tag);
  if(cls)n.className=cls;
  if(text!==undefined)n.textContent=text;
  return n;
}
;
const time = text => new Date(text).toLocaleTimeString("ru-RU",{
  hour:"2-digit",minute:"2-digit",second:"2-digit"
}
);
const show = value => typeof value === "object" ? JSON.stringify(value) : String(value);
async function get(url){
  const r=await fetch(url,{
    cache:"no-store"
  }
  );
  if(!r.ok)throw new Error(r.status===401?"Нужно войти под учётной записью команды":r.status===503?"Журнал недоступен. Звонок может продолжаться; часть событий не видна":"Не удалось прочитать журнал ("+r.status+")");
  return r.json();
}
function health(info){
  $("connection").textContent=info.healthy?"● Журнал подключён · обновление каждую секунду":"Журнал записывается с ошибками";
  if(info.dropped){
    $("connection").textContent+=" · пропущено событий этим процессом: "+info.dropped;
  }
}
function selectCall(id){
  selected=id;
  cursor=0;
  all=[];
  lastTurn=null;
  generation++;
  $("timeline").replaceChildren();
  $("stats").replaceChildren();
  $("title").textContent="Звонок "+id;
  $("subtitle").textContent="Показываем события после включения диагностики. Отсутствие события не доказывает отсутствие действия.";
  poll();
}
function describe(e){
  const d=e.data;
  let title=e.kind,body="",cls="",chips=[];
  if(d.recording==="opt_out")return {
    title:"Диагностика текста отключена по просьбе пациента",body:"Текст этой части разговора не показывается.",cls:"",chips:[]
  }
  ;
  switch(e.kind){
    case "voice.ready": title="Телефонный канал подключён";
    body=d.source==="SIP"?"Входящий SIP-звонок":"Голосовой канал браузера";
    break;
    case "voice.transcript": title=d.final?"ASR · итог распознавания":"ASR · промежуточный текст";
    body=d.text||"Распознавание не вернуло текст";
    cls=d.final?"patient":"patient partial";
    if(d.status)chips.push(d.status);
    break;
    case "dialogue.input":title=d.final?"Фраза поступила в диалог":"Промежуточная фраза · проверка безопасности";
    body=d.text;
    cls="patient";
    break;
    case "dialogue.input_started":title="Началась новая речь";
    body="Разрешение прошлого хода отозвано. Версия ввода: "+d.input_epoch;
    break;
    case "safety.checked":title="Проверка безопасности";
    body=d.rule?"Маршрут: "+d.rule:"Оснований для немедленного перевода нет";
    chips=d.flags||[];
    if(d.rule)cls="error";
    break;
    case "dialogue.state":title="Текущий шаг";
    body=(phases[d.phase]||d.phase)+" · сценарий: "+({
      booking:"запись",visits:"просмотр записей",cancel:"отмена",reschedule:"перенос"
    }
    [d.purpose]||d.purpose);
    chips=["непонятых реплик: "+d.misunderstandings,"версия: "+d.version];
    break;
    case "nlu.parsed":title="Что понял разбор фразы";
    body=(d.intents||[]).map(i=>intents[i]?i+" · "+intents[i]:(answer[i]||i)).join("\n")||"Новая просьба не выделена; проверяем данные текущего шага";
    const fields=Object.entries(d.slots||{
    }
    ).filter(([,v])=>v!==null&&v!==undefined&&(!Array.isArray(v)||v.length));
    if(fields.length)body+="\n"+fields.map(([k,v])=>(slots[k]||k)+": "+show(v)).join("\n");
    chips=["источник: "+d.source,...(d.flags||[])];
    break;
    case "dialogue.reply":title="Решение диалога";
    body=(d.utterances||[]).join("\n");
    cls="assistant";
    chips=[phases[d.phase]||d.phase,"ожидаем: "+(answer[d.expect]||d.expect)];
    if(d.code)chips.push(d.code);
    if(d.visit_id)chips.push("запись МИС №"+d.visit_id);
    if(d.retry_at)body+="\nПовторное обращение возможно: "+time(d.retry_at);
    if(d.transfer)chips.push(d.transfer.route+" · "+d.transfer.priority);
    if(["NOT_UNDERSTOOD","PROCESSING_INTERRUPTED","WAIT_MIS"].includes(d.code))cls="error";
    break;
    case "mis.request":title="МИС · запрос ожидает бюджета";
    body=d.method+" "+d.resource;
    break;
    case "mis.sent":title="МИС · запрос отправлен";
    body=d.method+" "+d.resource;
    break;
    case "mis.response":title="МИС · HTTP-ответ";
    body="Статус "+d.status+(d.status<300?" · тело ещё проверяется кодом":"");
    if(d.status>=400)cls="error";
    break;
    case "mis.finished":title="МИС · обращение завершено";
    body=d.elapsed_ms+" мс с учётом ожидания";
    break;
    case "mis.error":title="МИС · отказ или неопределённый результат";
    body=[d.code,d.error,d.retry_after?"Пауза: "+d.retry_after+" с":null].filter(Boolean).join(" · ");
    cls="error";
    break;
    case "voice.reply":title="Голосовой шлюз получил ответ";
    body=(d.utterances||[]).join("\n");
    chips=[d.code,d.phase].filter(Boolean);
    break;
    case "voice.speak":title="Ответ отправлен на воспроизведение";
    body=d.text;
    cls="assistant";
    break;
    case "voice.stop":title="Пациент перебил ответ";
    body="Шлюз остановил прежнюю реплику";
    break;
    case "voice.timing":title="Время обработки";
    body=["ASR: "+d.asr_ms+" мс","Диалог: "+d.dialog_ms+" мс","Синтез: "+d.tts_ms+" мс","После конца речи: "+d.since_speech_ms+" мс"].join(" · ");
    break;
    case "dialogue.error":title="Ошибка обработки диалога";
    body=d.error;
    cls="error";
    break;
    case "dialogue.ended":title="Диалог завершён";
    break;
    case "voice.closed":title="Телефонный канал закрыт";
    body="Завершено соединение с пациентом";
    break;
    case "voice.end":title="Шлюз завершает разговор";
    body=d.reason||"Завершение";
    if(d.route)body+=" · "+d.route;
    if(d.cause)chips.push(d.cause);
    break;
    case "voice.question":title=d.kind==="audibility"?"Шлюз спросил: «Вы меня слышите?»":"Шлюз предложил сотрудника";
    body="Ответ на этот вопрос не подтверждает действие диалога";
    break;
    case "voice.transfer_result":title="Исход перевода";
    body=[d.outcome,d.route,d.priority].filter(Boolean).join(" · ");
    cls=d.outcome==="connected"?"":"error";
    if(d.reason)chips.push(d.reason);
    break;
    case "privacy.opt_out":title="Пациент отказался от сохранения текста";
    body="История диагностики этого звонка очищена";
    break;
  }
  if(d.truncated)body="Содержимое события превышает ограничение журнала";
  if(d.unavailable){
    body=d.unavailable;
    cls="error";
  }
  return {
    title,body,cls,chips
  }
  ;
}
function renderEvent(e){
  if(e.data.diagnostic_dropped){
    $("notice").classList.add("error");
    $("notice").textContent="Часть событий потеряна: процесс "+e.producer+" сообщил о "+e.data.diagnostic_dropped+" пропусках. По этой истории нельзя делать вывод об отсутствии действия.";
  }
  if(e.kind==="privacy.opt_out"){
    all=[];
    $("timeline").replaceChildren();
    lastTurn=null;
  }
  if(e.turn_id!==null&&e.turn_id!==lastTurn){
    $("timeline").append(node("div","turn","РЕПЛИКА "+e.turn_id));
    lastTurn=e.turn_id;
  }
  const view=describe(e),card=node("article","event "+view.cls),head=node("div","event-head");
  head.append(node("b","",view.title),node("time","",time(e.created_at)));
  card.append(head,node("div","event-body",view.body));
  if(view.chips.length){
    const chips=node("div","chips");
    view.chips.forEach(c=>chips.append(node("span","chip",c)));
    card.append(chips);
  }
  const detail=node("details"),summary=node("summary","","Технические данные"),pre=node("pre","",JSON.stringify({
    event:e.kind,producer:e.producer,...e.data
  }
  ,null,2));
  detail.append(summary,pre);
  card.append(detail);
  $("timeline").append(card);
}
function stats(){
  const finals=all.filter(e=>e.kind==="dialogue.input"&&e.data.final),requests=all.filter(e=>e.kind==="mis.sent"),errors=all.filter(e=>["mis.error","dialogue.error"].includes(e.kind)),timings=all.filter(e=>e.kind==="voice.timing");
  $("stats").replaceChildren();
  for(const [value,label]of[[finals.length,"итоговых реплик"],[requests.length,"отправлено запросов МИС"],[errors.length,"ошибок интеграции"],[timings.length?timings.at(-1).data.since_speech_ms+" мс":"—","последний ответ после речи"]]){
    const box=node("div","stat");
    box.append(node("b","",String(value)),node("span","",label));
    $("stats").append(box);
  }
}
async function poll(){
  if(busy)return;
  busy=true;
  const version=generation;
  try{
    const data=await get("/api/debug/calls");
    health(data.recorder);
    $("calls").replaceChildren();
    for(const c of data.calls){
      const b=node("button",c.call_id===selected?"selected":"");
      b.append(node("b","",c.source+" · "+(c.ended?"Завершён":"Завершение не зафиксировано")),node("small","",c.call_id),node("small","",time(c.last_at)));
      b.onclick=()=>selectCall(c.call_id);
      $("calls").append(b);
    }
    if(!data.calls.length)$("calls").append(node("p","muted","Звонков пока нет. Диагностика показывает только новые события после её включения."));
    if(selected){
      const id=selected;
      let more=true,pages=0;
      while(more&&pages++<4){
        const result=await get("/api/debug/calls/"+encodeURIComponent(id)+"/events?after="+cursor);
        if(version!==generation)break;
        for(const e of result.events){
          renderEvent(e);
          all.push(e);
        }
        cursor=result.cursor;
        more=result.has_more;
        if(all.length>700){
          all=all.slice(-500);
          $("timeline").replaceChildren();
          lastTurn=null;
          all.forEach(renderEvent);
        }
        stats();
      }
      if($("follow").checked&&version===generation)$("timeline").scrollTop=$("timeline").scrollHeight;
    }
  }
  catch(error){
    $("connection").textContent=error.message;
  }
  finally{
    busy=false;
  }
}
$("refresh").onclick=poll;
setInterval(poll,1000);
poll();
