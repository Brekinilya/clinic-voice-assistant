"""Protected operator queue for manual callback work."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.handoffs.ports import HandoffConflict

router = APIRouter()


class OwnerInput(BaseModel):
    owner: str = Field(min_length=1, max_length=64, pattern=r"^\S(?:.*\S)?$")


class StartInput(OwnerInput):
    version: int = Field(ge=1)


class FinishInput(BaseModel):
    owner: str = Field(min_length=1, max_length=64, pattern=r"^\S(?:.*\S)?$")
    version: int = Field(ge=1)
    outcome: Literal["connected", "no_answer", "failed", "cancelled", "unknown"]
    reason: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9_:-]+$")


class TextRequestInput(BaseModel):
    route: str = Field(min_length=4, max_length=4, pattern=r"^R-\d{2}$")
    priority: Literal["P1", "P2", "P3"]
    summary: str = Field(min_length=1, max_length=200)


class CancelInput(OwnerInput):
    version: int = Field(ge=1)


def _service(request: Request):
    service = getattr(request.app.state.dialogue, "handoffs", None)
    if service is None:
        raise HTTPException(404, "operator queue is disabled")
    return service


def _task(task):
    return {
        "task_id": task.task_id,
        "request_id": task.request_id,
        "state": task.state.value,
        "priority": task.priority,
        "deadline": task.deadline.isoformat(),
        "preferred_at": task.preferred_at.isoformat() if task.preferred_at else None,
        "next_at": task.next_at.isoformat(),
        "contact_masked": task.contact_masked,
        "attempts": task.attempts,
        "active_attempt_id": task.active_attempt_id,
        "owner": task.owner,
        "version": task.version,
        "sms_pending": task.sms_pending,
    }


@router.get("/api/operator/callbacks")
async def callback_queue(request: Request, response: Response, limit: int = 50, offset: int = 0):
    response.headers["Cache-Control"] = "no-store"
    if not 1 <= limit <= 50 or offset < 0:
        raise HTTPException(422, "invalid page")
    service = _service(request)
    tasks = await service.store.list_callback_tasks(limit=limit, offset=offset)
    items = []
    for task in tasks:
        item = _task(task)
        record = await service.store.get_request(task.request_id)
        item["summary"] = record.summary if record else ""
        item["route"] = record.route if record else ""
        item["attempt_history"] = [
            {
                "attempt_id": attempt.attempt_id,
                "started_at": attempt.started_at.isoformat(),
                "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
                "outcome": attempt.outcome.value,
                "reason": attempt.reason,
            }
            for attempt in await service.callback_attempt_history(task.task_id)
        ]
        items.append(item)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/api/operator/requests")
async def handoff_requests(request: Request, response: Response, limit: int = 50, offset: int = 0):
    response.headers["Cache-Control"] = "no-store"
    if not 1 <= limit <= 50 or offset < 0:
        raise HTTPException(422, "invalid page")
    service = _service(request)
    records = await service.store.list_requests(limit=limit, offset=offset)
    return {
        "items": [
            {
                "request_id": item.request_id,
                "source": item.source,
                "created_at": item.created_at.isoformat(),
                "route": item.route,
                "priority": item.priority,
                "identity_status": item.identity_status,
                "purpose": item.purpose,
                "summary": item.summary,
                "transfer_state": item.transfer_state.value,
                "transfer_reason": item.transfer_reason,
                "mis_state": item.mis_state.value,
                "mis_operation_id": item.mis_operation_id,
                "mis_escalation_id": item.mis_escalation_id,
                "callback_state": item.callback_state.value if item.callback_state else None,
                "callback_deadline": (
                    item.callback_deadline.isoformat() if item.callback_deadline else None
                ),
            }
            for item in records
        ],
        "limit": limit,
        "offset": offset,
    }


@router.post("/api/operator/requests")
async def create_text_request(body: TextRequestInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    record = await service.create_text_request(body.route, body.priority, body.summary)
    return {
        "request_id": record.request_id,
        "source": record.source,
        "route": record.route,
        "priority": record.priority,
        "identity_status": record.identity_status,
        "purpose": record.purpose,
        "summary": record.summary,
        "transfer_state": record.transfer_state.value,
        "mis_state": record.mis_state.value,
    }


@router.post("/api/operator/callbacks/{task_id}/claim")
async def claim_callback(task_id: str, body: OwnerInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        task = await _service(request).operator_claim(task_id, body.owner)
    except HandoffConflict as error:
        raise HTTPException(409, "task is no longer available") from error
    return _task(task)


@router.post("/api/operator/callbacks/{task_id}/attempts")
async def start_callback(task_id: str, body: StartInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        task, attempt = await _service(request).operator_start_attempt(
            task_id, body.owner, body.version
        )
    except HandoffConflict as error:
        raise HTTPException(409, "task state changed") from error
    return {"task": _task(task), "attempt_id": attempt.attempt_id}


@router.post("/api/operator/attempts/{attempt_id}/finish")
async def finish_callback(attempt_id: str, body: FinishInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        task = await _service(request).operator_finish_attempt(
            attempt_id, body.outcome, owner=body.owner,
            version=body.version, reason=body.reason
        )
    except HandoffConflict as error:
        raise HTTPException(409, "attempt was already finished or is missing") from error
    return _task(task)


@router.post("/api/operator/callbacks/{task_id}/contact")
async def reveal_callback_contact(
    task_id: str, body: OwnerInput, request: Request, response: Response
):
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    try:
        contact = await service.reveal_contact_for_owner(task_id, body.owner)
    except HandoffConflict as error:
        raise HTTPException(409, "task is not actively owned") from error
    if contact is None:
        raise HTTPException(410, "contact is unavailable")
    return JSONResponse({"contact": contact}, headers={"Cache-Control": "no-store"})


@router.post("/api/operator/callbacks/{task_id}/cancel")
async def cancel_callback(task_id: str, body: CancelInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        task = await _service(request).operator_cancel(task_id, body.owner, body.version)
    except HandoffConflict as error:
        raise HTTPException(409, "task is no longer cancellable") from error
    return _task(task)


@router.get("/operator", response_class=HTMLResponse)
async def operator_page():
    return HTMLResponse(
        """<!doctype html><meta charset=utf-8><title>Очередь обращений</title>
<h1>Очередь обращений</h1><p>Callback — ручная очередь. Автоматический исходящий звонок не выполняется.</p>
<label>Оператор <input id=owner maxlength=64 autocomplete=off></label>
<button id=refresh>Обновить</button><h2>Карточки</h2><div id=requests></div>
<h2>Обратные звонки</h2><div id=callbacks></div>
<h2>Добавить текстовое обращение</h2><form id=text-request>
<label>Маршрут <input name=route value=R-08 maxlength=16 required></label>
<label>Приоритет <select name=priority><option>P3</option><option>P2</option><option>P1</option></select></label>
<label>Краткая причина <input name=summary maxlength=200 required></label><button>Создать карточку</button></form>
<script>
const owner=document.getElementById('owner'), cards=document.getElementById('requests'), callbacks=document.getElementById('callbacks');
async function request(path,body){const r=await fetch(path,{method:body?'POST':'GET',cache:'no-store',headers:body?{'content-type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const d=await r.json();if(!r.ok){alert(d.detail||'Ошибка');return null}return d}
function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=fn;return b}
function section(title,detail){const x=document.createElement('section'),h=document.createElement('h3'),p=document.createElement('p');h.textContent=title;p.textContent=detail;x.append(h,p);return x}
async function load(){const [rd,cd]=await Promise.all([request('/api/operator/requests'),request('/api/operator/callbacks')]);if(!rd||!cd)return;cards.replaceChildren();callbacks.replaceChildren();
for(const r of rd.items){cards.append(section(`${r.priority} · ${r.source} · ${r.transfer_state}`,`${r.summary} · ${r.route} · identity ${r.identity_status} · MIS ${r.mis_state} · callback ${r.callback_state||'—'} · ${r.transfer_reason||''}`))}
for(const t of cd.items){const x=section(`${t.priority} · ${t.state}`,`${t.summary} · ${t.contact_masked} · срок до ${t.deadline} · попыток ${t.attempts}/3`);x.append(document.createTextNode(`История попыток: ${t.attempt_history.map(a=>`${a.outcome}${a.reason?' ('+a.reason+')':''}`).join(', ')||'нет'}`));
if(t.state==='queued'||t.state==='claimed'&&t.owner===owner.value)x.append(button('Взять задачу',async()=>{if(await request(`/api/operator/callbacks/${t.task_id}/claim`,{owner:owner.value}))load()}));
if(t.state==='claimed'&&t.owner===owner.value){x.append(button('Показать номер',async()=>{const q=await request(`/api/operator/callbacks/${t.task_id}/contact`,{owner:owner.value});if(q)alert(q.contact)}));x.append(button('Начать ручную попытку',async()=>{if(await request(`/api/operator/callbacks/${t.task_id}/attempts`,{owner:owner.value,version:t.version}))load()}))}
if(t.state==='in_progress'&&t.owner===owner.value&&t.active_attempt_id){for(const [label,outcome] of [['Связались','connected'],['Не ответил','no_answer'],['Сбой','failed'],['Исход неизвестен','unknown']])x.append(button(label,async()=>{if(await request(`/api/operator/attempts/${t.active_attempt_id}/finish`,{owner:owner.value,version:t.version,outcome}))load()}))}
if(['queued','claimed'].includes(t.state)&&(!t.owner||t.owner===owner.value))x.append(button('Отменить задачу',async()=>{if(await request(`/api/operator/callbacks/${t.task_id}/cancel`,{owner:owner.value,version:t.version}))load()}));callbacks.append(x)}}
document.getElementById('refresh').onclick=load;
document.getElementById('text-request').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);if(await request('/api/operator/requests',{route:f.get('route'),priority:f.get('priority'),summary:f.get('summary')})){e.target.reset();load()}};
load();
</script>""",
        headers={"Cache-Control": "no-store"},
    )
