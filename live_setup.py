from __future__ import annotations

import asyncio
import base64
import hmac
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from browser_bridge import MESSENGER_URL, RUNTIME_STATE, messenger

LIVE_HTML = r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Messenger Remote Browser</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#111;color:#eee;font-family:system-ui,-apple-system,sans-serif}#top{height:44px;box-sizing:border-box;display:flex;align-items:center;gap:10px;padding:6px 10px;background:#1d1d1d;border-bottom:1px solid #333;font-size:13px}#dot{width:9px;height:9px;border-radius:50%;background:#d89a42;flex:none}#status{white-space:nowrap}#url{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#aaa;flex:1}button{border:0;border-radius:8px;padding:7px 10px;font-weight:600;cursor:pointer}#wrap{height:calc(100% - 44px);display:flex;align-items:center;justify-content:center;background:#080808;position:relative}canvas{display:block;background:#fff;max-width:100%;max-height:100%;outline:none;touch-action:none}#toast{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);background:#111d;padding:8px 12px;border-radius:9px;display:none;z-index:4;font-size:13px}#mobileInput{position:fixed;opacity:0;pointer-events:none;left:-1000px;bottom:0;width:1px;height:1px}.ok{background:#34a853!important}
</style></head><body>
<div id="top"><span id="dot"></span><span id="status">Đang kết nối…</span><span id="url"></span><button id="keyboard">⌨️</button><button id="save">Lưu phiên</button></div>
<div id="wrap"><canvas id="screen" width="1024" height="720" tabindex="0"></canvas><div id="toast"></div></div><textarea id="mobileInput" autocomplete="off" autocapitalize="off" spellcheck="false"></textarea>
<script>
const canvas=document.getElementById('screen'),ctx=canvas.getContext('2d'),dot=document.getElementById('dot'),statusEl=document.getElementById('status'),urlEl=document.getElementById('url'),toast=document.getElementById('toast'),mobile=document.getElementById('mobileInput');
const key=location.pathname.split('/').filter(Boolean).pop(); const proto=location.protocol==='https:'?'wss':'ws'; const ws=new WebSocket(`${proto}://${location.host}/live/${key}/ws`); ws.binaryType='blob';
let lastUrl='', logged=false, pressed=false, lastX=0,lastY=0;
function show(s){toast.textContent=s;toast.style.display='block';clearTimeout(show.t);show.t=setTimeout(()=>toast.style.display='none',1800)}
function pos(e){const r=canvas.getBoundingClientRect();return{x:(e.clientX-r.left)*1024/r.width,y:(e.clientY-r.top)*720/r.height}}
function send(o){if(ws.readyState===1)ws.send(JSON.stringify(o))}
ws.onopen=()=>{statusEl.textContent='Đã kết nối';canvas.focus()}; ws.onclose=()=>{statusEl.textContent='Mất kết nối';dot.style.background='#d93025'};
ws.onmessage=async e=>{if(typeof e.data==='string'){let j;try{j=JSON.parse(e.data)}catch{return}if(j.t==='status'){lastUrl=j.url||'';logged=!!j.logged_in;urlEl.textContent=lastUrl;statusEl.textContent=logged?'Messenger đã đăng nhập':'Messenger';dot.style.background=logged?'#34a853':'#d89a42'}else if(j.t==='saved'){show('Đã lưu phiên đăng nhập')}else if(j.t==='error'){show(j.message||'Có lỗi')}}else{const bmp=await createImageBitmap(e.data);ctx.drawImage(bmp,0,0,1024,720);bmp.close()}};
canvas.addEventListener('pointerdown',e=>{e.preventDefault();canvas.setPointerCapture(e.pointerId);canvas.focus();pressed=true;const p=pos(e);lastX=p.x;lastY=p.y;send({t:'mouse',a:'down',x:p.x,y:p.y,button:e.button})});
canvas.addEventListener('pointermove',e=>{if(!pressed)return;const p=pos(e);if(Math.abs(p.x-lastX)+Math.abs(p.y-lastY)>2){lastX=p.x;lastY=p.y;send({t:'mouse',a:'move',x:p.x,y:p.y})}});
canvas.addEventListener('pointerup',e=>{e.preventDefault();pressed=false;const p=pos(e);send({t:'mouse',a:'up',x:p.x,y:p.y,button:e.button});canvas.focus()});
canvas.addEventListener('wheel',e=>{e.preventDefault();send({t:'wheel',dx:e.deltaX,dy:e.deltaY})},{passive:false});
canvas.addEventListener('keydown',e=>{e.preventDefault();if((e.ctrlKey||e.metaKey||e.altKey)&&e.key.length===1){let parts=[];if(e.ctrlKey)parts.push('Control');if(e.metaKey)parts.push('Meta');if(e.altKey)parts.push('Alt');if(e.shiftKey)parts.push('Shift');parts.push(e.key.toUpperCase());send({t:'press',key:parts.join('+')});return}if(e.key.length===1)send({t:'text',text:e.key});else send({t:'press',key:e.key})});
canvas.addEventListener('paste',e=>{e.preventDefault();send({t:'text',text:(e.clipboardData||window.clipboardData).getData('text')})});
document.getElementById('save').onclick=()=>send({t:'save'});
document.getElementById('keyboard').onclick=()=>{mobile.value='';mobile.focus();show('Bàn phím đã mở — gõ nội dung rồi nhấn Enter')};
mobile.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();if(mobile.value){send({t:'text',text:mobile.value});mobile.value=''}canvas.focus()}else if(e.key==='Backspace'&&!mobile.value){send({t:'press',key:'Backspace'})}});
</script></body></html>'''


def register_live_setup(app: FastAPI, access_key: str) -> None:
    def valid(key: str) -> bool:
        return bool(access_key) and hmac.compare_digest(key, access_key)

    @app.get('/live/{key}', response_class=HTMLResponse)
    async def live_ui(request: Request, key: str) -> HTMLResponse:
        if not valid(key):
            raise HTTPException(401, 'Unauthorized')
        return HTMLResponse(LIVE_HTML, headers={'Cache-Control':'no-store, max-age=0'})

    @app.websocket('/live/{key}/ws')
    async def live_ws(ws: WebSocket, key: str) -> None:
        if not valid(key):
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            async with messenger._lock:
                page = await messenger._get_page_unlocked()
                if page.url == 'about:blank':
                    await page.goto(MESSENGER_URL, wait_until='domcontentloaded', timeout=35_000)
                    await page.wait_for_timeout(700)
                messenger._touch_unlocked()
        except Exception as exc:
            await ws.send_text(json.dumps({'t':'error','message':str(exc)}, ensure_ascii=False))
            await ws.close(code=1011)
            return

        async def frames() -> None:
            status_tick = 0
            while True:
                await asyncio.sleep(0.28)
                try:
                    async with messenger._lock:
                        page = await messenger._get_page_unlocked()
                        shot = await page.screenshot(type='jpeg', quality=52)
                        status_tick += 1
                        if status_tick >= 3:
                            status_tick = 0
                            logged = await messenger._is_logged_in(page)
                            await ws.send_text(json.dumps({'t':'status','url':page.url,'logged_in':logged}, ensure_ascii=False))
                        messenger._touch_unlocked()
                    await ws.send_bytes(shot)
                except Exception as exc:
                    try:
                        await ws.send_text(json.dumps({'t':'error','message':str(exc)}, ensure_ascii=False))
                    except Exception:
                        pass
                    return

        async def controls() -> None:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                typ = msg.get('t')
                try:
                    async with messenger._lock:
                        page = await messenger._get_page_unlocked()
                        if typ == 'mouse':
                            x=max(0,min(float(msg.get('x',0)),1024)); y=max(0,min(float(msg.get('y',0)),720)); a=msg.get('a')
                            if a == 'move': await page.mouse.move(x,y)
                            elif a == 'down': await page.mouse.move(x,y); await page.mouse.down(button=('right' if int(msg.get('button',0))==2 else 'left'))
                            elif a == 'up': await page.mouse.move(x,y); await page.mouse.up(button=('right' if int(msg.get('button',0))==2 else 'left'))
                        elif typ == 'wheel':
                            await page.mouse.wheel(float(msg.get('dx',0)), float(msg.get('dy',0)))
                        elif typ == 'text':
                            text=str(msg.get('text',''))[:4000]
                            if text: await page.keyboard.insert_text(text)
                        elif typ == 'press':
                            keyname=str(msg.get('key',''))[:100]
                            allowed={'Enter','Tab','Backspace','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Space','Delete','Home','End','PageUp','PageDown'}
                            if '+' in keyname or keyname in allowed: await page.keyboard.press(keyname)
                        elif typ == 'save':
                            if not await messenger._is_logged_in(page):
                                await ws.send_text(json.dumps({'t':'error','message':'Messenger chưa đăng nhập xong.'}, ensure_ascii=False))
                            else:
                                await messenger._persist_runtime_state_unlocked()
                                await ws.send_text(json.dumps({'t':'saved'}))
                        messenger._touch_unlocked()
                except Exception as exc:
                    await ws.send_text(json.dumps({'t':'error','message':str(exc)}, ensure_ascii=False))

        sender=asyncio.create_task(frames()); receiver=asyncio.create_task(controls())
        try:
            done,pending=await asyncio.wait({sender,receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending: task.cancel()
        except WebSocketDisconnect:
            sender.cancel(); receiver.cancel()
        except Exception:
            sender.cancel(); receiver.cancel()
