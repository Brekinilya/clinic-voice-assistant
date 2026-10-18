"""Protected operator queue for manual callback work."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.handoffs.ports import HandoffConflict

router = APIRouter()


class OwnerInput(BaseModel):
    owner: str = Field(min_length=1, max_length=64)


class StartInput(OwnerInput):
    version: int = Field(ge=1)


class FinishInput(BaseModel):
    outcome: Literal["connected", "no_answer", "failed", "cancelled", "unknown"]
    reason: str | None = Field(default=None, max_length=64)


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
        "owner": task.owner,
        "version": task.version,
        "sms_pending": task.sms_pending,
    }


@router.get("/api/operator/callbacks")
async def callback_queue(request: Request, limit: int = 50, offset: int = 0):
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
        items.append(item)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/api/operator/requests")
async def handoff_requests(request: Request, limit: int = 50, offset: int = 0):
    if not 1 <= limit <= 50 or offset < 0:
        raise HTTPException(422, "invalid page")
    service = _service(request)
    records = await service.store.list_requests(limit=limit, offset=offset)
    return {
        "items": [
            {
                "request_id": item.request_id,
                "created_at": item.created_at.isoformat(),
                "route": item.route,
                "priority": item.priority,
                "identity_status": item.identity_status,
                "purpose": item.purpose,
                "summary": item.summary,
                "transfer_state": item.transfer_state.value,
                "transfer_reason": item.transfer_reason,
                "mis_state": item.mis_state.value,
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


@router.post("/api/operator/callbacks/{task_id}/claim")
async def claim_callback(task_id: str, body: OwnerInput, request: Request):
    try:
        task = await _service(request).operator_claim(task_id, body.owner)
    except HandoffConflict as error:
        raise HTTPException(409, "task is no longer available") from error
    return _task(task)


@router.post("/api/operator/callbacks/{task_id}/attempts")
async def start_callback(task_id: str, body: StartInput, request: Request):
    try:
        task, attempt = await _service(request).operator_start_attempt(
            task_id, body.owner, body.version
        )
    except HandoffConflict as error:
        raise HTTPException(409, "task state changed") from error
    return {"task": _task(task), "attempt_id": attempt.attempt_id}


@router.post("/api/operator/attempts/{attempt_id}/finish")
async def finish_callback(attempt_id: str, body: FinishInput, request: Request):
    try:
        task = await _service(request).operator_finish_attempt(
            attempt_id, body.outcome, body.reason
        )
    except HandoffConflict as error:
        raise HTTPException(409, "attempt was already finished or is missing") from error
    return _task(task)


@router.post("/api/operator/callbacks/{task_id}/contact")
async def reveal_callback_contact(task_id: str, request: Request):
    service = _service(request)
    task = await service.store.get_callback_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    contact = service.reveal_contact(task)
    if contact is None:
        raise HTTPException(410, "contact is unavailable")
    return JSONResponse({"contact": contact}, headers={"Cache-Control": "no-store"})


@router.get("/operator", response_class=HTMLResponse)
async def operator_page():
    return HTMLResponse(
        """<!doctype html><meta charset=utf-8><title>Очередь callback</title>
<h1>Очередь обратных звонков</h1><label>Оператор <input id=owner maxlength=64></label>
<button onclick=load()>Обновить</button><div id=items></div>
<script>
const root=document.getElementById('items'), ownerInput=document.getElementById('owner');
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)alert(d.detail||'Ошибка');return r.ok?d:null}
function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=fn;return b}
async function load(){const r=await fetch('/api/operator/callbacks');const d=await r.json();root.replaceChildren();
for(const t of d.items){const x=document.createElement('section'),h=document.createElement('h2'),s=document.createElement('p');
h.textContent=`${t.priority} · ${t.state}`;s.textContent=`${t.summary} · ${t.contact_masked} · до ${t.deadline} · попыток ${t.attempts}/3`;x.append(h,s);
if(t.state==='queued'||(t.state==='claimed'&&t.owner===ownerInput.value)){x.append(button('Взять задачу',async()=>{if(await post(`/api/operator/callbacks/${t.task_id}/claim`,{owner:ownerInput.value}))load()}))}
if(t.state==='claimed'&&t.owner===ownerInput.value){
x.append(button('Показать номер',async()=>{const q=await post(`/api/operator/callbacks/${t.task_id}/contact`,{});if(q)alert(q.contact)}));
x.append(button('Начать ручную попытку',async()=>{const q=await post(`/api/operator/callbacks/${t.task_id}/attempts`,{owner:ownerInput.value,version:t.version});if(!q)return;
const label=document.createElement('p');label.textContent='Попытка начата. После ручного звонка отметьте результат:';x.append(label);
for(const [title,outcome] of [['Связались','connected'],['Не ответил','no_answer'],['Сбой','failed'],['Исход неизвестен','unknown']])x.append(button(title,async()=>{if(await post(`/api/operator/attempts/${q.attempt_id}/finish`,{outcome}))load()}));
}))}
root.append(x)}}
load();
</script>"""
    )
