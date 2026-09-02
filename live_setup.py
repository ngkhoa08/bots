from __future__ import annotations

import asyncio
import hmac
import json

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse

from browser_bridge import MESSENGER_URL, messenger

LIVE_HTML = r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Messenger Remote Browser</title>
<style>
html,body{margin:0;width:100%;height:100%;background:#101010;color:#eee;font-family:system-ui,-apple-system,sans-serif;overflow:hidden}#bar{height:48px;display:flex;align-items:center;gap:8px;padding:0 10px;background:#1c1c1c;border-bottom:1px solid #333;box-sizing:border-box}#dot{width:9px;height:9px;border-radius:50%;background:#d89a42}#status{font-size:13px;white-space:nowrap}#url{flex:1;color:#aaa;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}button{border:0;border-radius:8px;padding:8px 10px;font-weight:650;cursor:pointer}#wrap{height:calc(100% - 104px);display:flex;align-items:center;justify-content:center;background:#070707}canvas{display:block;background:#fff;max-width:100%;max-height:100%;outline:none;touch-action:manipulation;cursor:default}#inputbar{height:56px;display:flex;gap:8px;align-items:center;padding:8px;background:#1c1c1c;border-top:1px solid #333;box-sizing:border-box}#text{flex:1;min-width:0;height:38px;padding:0 11px;border-radius:9px;border:1px solid #555;background:#0f0f0f;color:#fff;font-size:16px}#send{height:38px}#toast{position:fixed;left:50%;bottom:66px;transform:translateX(-50%);background:#000d;padding:8px 12px;border-radius:9px;display:none;font-size:13px;z-index:5}
</style></head><body>
<div id="bar"><span id="dot"></span><span id="status">Đang kết nối…</span><span id="url"></span><button id="save">Lưu phiên</button></div>
<div id="wrap"><canvas id="screen" width="1024" height="720" tabindex="0"></canvas></div>
<div id="inputbar"><input id="text" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Sau khi click ô trên Messenger, gõ ở đây rồi nhấn Enter"><button id="send">Gõ</button><button id="back">⌫</button><button id="enter">Enter</button></div>
<div id="toast"></div>
<script>
const c=document.getElementById('screen'),ctx=c.getContext('2d'),dot=document.getElementById('dot'),st=document.getElementById('status'),urlEl=document.getElementById('url'),txt=document.getElementById('text'),toast=document.getElementById('toast');
const key=location.pathname.split('/').filter(Boolean).pop(), proto=location.protocol==='https:'?'wss':'ws';
const ws=new WebSocket(`${proto}://${location.host}/live/${key}/ws`);ws.binaryType='blob';
function say(s){toast.textContent=s;toast.style.display='block';clearTimeout(say.t);say.t=setTimeout(()=>toast.style.display='none',1400)}
function send(o){if(ws.readyState===1)ws.send(JSON.stringify(o))}
function pos(e){const r=c.getBoundingClientRect();return{x:(e.clientX-r.left)*1024/r.width,y:(e.clientY-r.top)*720/r.height}}
ws.onopen=()=>{st.textContent='Đã kết nối';dot.style.background='#34a853'};
ws.onclose=()=>{st.textContent='Mất kết nối';dot.style.background='#d93025'};
ws.onmessage=async e=>{if(typeof e.data==='string'){let j;try{j=JSON.parse(e.data)}catch{return}if(j.t==='status'){urlEl.textContent=j.url||'';st.textContent=j.logged_in?'Messenger đã đăng nhập':'Messenger';dot.style.background=j.logged_in?'#34a853':'#d89a42'}else if(j.t==='ack'){if(j.what==='tap')say('Đã click');else if(j.what==='text')say('Đã gõ');else if(j.what==='saved')say('Đã lưu phiên')}else if(j.t==='error')say(j.message||'Có lỗi')}else{const bmp=await createImageBitmap(e.data);ctx.drawImage(bmp,0,0,1024,720);bmp.close()}};
let down=null;
c.addEventListener('pointerdown',e=>{e.preventDefault();down=pos(e)});
c.addEventListener('pointerup',e=>{e.preventDefault();const p=pos(e);send({t:'tap',x:p.x,y:p.y});down=null});
c.addEventListener('wheel',e=>{e.preventDefault();send({t:'wheel',dx:e.deltaX,dy:e.deltaY})},{passive:false});
c.addEventListener('keydown',e=>{if(e.key.length===1){e.preventDefault();send({t:'text',text:e.key})}else if(['Enter','Tab','Backspace','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Delete'].includes(e.key)){e.preventDefault();send({t:'press',key:e.key})}});
function typeText(){const v=txt.value;if(!v)return;txt.value='';send({t:'text',text:v})}
document.getElementById('send').onclick=typeText;txt.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();typeText()}});
document.getElementById('back').onclick=()=>send({t:'press',key:'Backspace'});document.getElementById('enter').onclick=()=>send({t:'press',key:'Enter'});document.getElementById('save').onclick=()=>send({t:'save'});
</script></body></html>'''


def register_live_setup(app: FastAPI, access_key: str) -> None:
    def valid(key: str) -> bool:
        return bool(access_key) and hmac.compare_digest(key, access_key)

    @app.get('/live/{key}', response_class=HTMLResponse)
    async def live_ui(request: Request, key: str) -> HTMLResponse:
        if not valid(key):
            raise HTTPException(401, 'Unauthorized')
        return HTMLResponse(LIVE_HTML, headers={'Cache-Control': 'no-store, max-age=0'})

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
            tick = 0
            while True:
                await asyncio.sleep(0.55)
                try:
                    async with messenger._lock:
                        page = await messenger._get_page_unlocked()
                        shot = await page.screenshot(type='jpeg', quality=50)
                        tick += 1
                        status = None
                        if tick >= 3:
                            tick = 0
                            status = {'t':'status','url':page.url,'logged_in':await messenger._is_logged_in(page)}
                        messenger._touch_unlocked()
                    await ws.send_bytes(shot)
                    if status:
                        await ws.send_text(json.dumps(status, ensure_ascii=False))
                except Exception:
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
                        if typ == 'tap':
                            x=max(0,min(float(msg.get('x',0)),1024)); y=max(0,min(float(msg.get('y',0)),720))
                            await page.mouse.click(x,y)
                            await ws.send_text(json.dumps({'t':'ack','what':'tap'}))
                        elif typ == 'wheel':
                            await page.mouse.wheel(float(msg.get('dx',0)), float(msg.get('dy',0)))
                        elif typ == 'text':
                            text=str(msg.get('text',''))[:4000]
                            if text:
                                await page.keyboard.insert_text(text)
                                await ws.send_text(json.dumps({'t':'ack','what':'text'}))
                        elif typ == 'press':
                            k=str(msg.get('key',''))[:40]
                            if k in {'Enter','Tab','Backspace','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Delete'}:
                                await page.keyboard.press(k)
                        elif typ == 'save':
                            if not await messenger._is_logged_in(page):
                                await ws.send_text(json.dumps({'t':'error','message':'Messenger chưa đăng nhập xong.'}, ensure_ascii=False))
                            else:
                                await messenger._persist_runtime_state_unlocked()
                                await ws.send_text(json.dumps({'t':'ack','what':'saved'}))
                        messenger._touch_unlocked()
                except Exception as exc:
                    await ws.send_text(json.dumps({'t':'error','message':str(exc)}, ensure_ascii=False))

        a=asyncio.create_task(frames()); b=asyncio.create_task(controls())
        done,pending=await asyncio.wait({a,b}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
