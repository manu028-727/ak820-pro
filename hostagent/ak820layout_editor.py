"""The layout editor: `ak820layout.py serve`.

A local web page, served by Python's own http.server on 127.0.0.1 only, that
draws the keyboard's 128x128 screen with the board's real art (decoded from
assets-src/current/flash_assets.bin, the same bytes the board draws from) and
lets you drag the dashboard's elements anywhere, resize the four bands, hide
four of them, and add images and text labels. Collisions are outlined in red. Every change is checked by ak820layout.py's rules -- the same
rules the firmware applies -- and Upload writes it to the keyboard.

Nothing to install: any browser, and the Python the agent already uses.
Layouts and images are read from and saved to one folder (--dir).
"""
import base64, http.server, json, os, re, socketserver, threading, urllib.parse, webbrowser

import ak820layout as L

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOB = os.path.join(ROOT, "assets-src", "current", "flash_assets.bin")
SAFE = re.compile(r"^[A-Za-z0-9._ -]{1,80}$")


def _safe(name, exts):
    name = os.path.basename(name or "")
    if not SAFE.match(name) or not name.lower().endswith(exts) or name.startswith("."):
        raise L.LayoutError(f"bad file name {name!r}")
    return name


class Handler(http.server.BaseHTTPRequestHandler):
    workdir = "."
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _local(self):
        """Only this machine's own pages: a Host other than 127.0.0.1 or
        localhost is a DNS-rebinding page, and a POST without our header is
        another site's form (a browser can't add the header cross-origin
        without a preflight this server never answers)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "forbidden"})
            return False
        if self.command == "POST" and self.headers.get("X-AK820") != "1":
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._local():
            return
        path = self.path.split("?")[0]
        try:
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/assets.bin":
                return self._send(200, open(BLOB, "rb").read(), "application/octet-stream")
            if path == "/api/files":
                names = sorted(os.listdir(self.workdir))
                return self._send(200, {"layouts": [n for n in names if n.endswith(".json")],
                                        "images": [n for n in names if n.lower().endswith((".png", ".gif"))],
                                        "dir": self.workdir})
            if path.startswith("/file/"):
                name = _safe(path[6:].replace("%20", " "), (".png", ".gif", ".json"))
                data = open(os.path.join(self.workdir, name), "rb").read()
                ctype = {"png": "image/png", "gif": "image/gif"}.get(name.lower().rsplit(".", 1)[-1], "application/json")
                return self._send(200, data, ctype)
            if path == "/api/frames":
                # The picture as the keyboard will show it: every frame over
                # black, in the panel's 16-bit colours, RGB bytes base64'd.
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                name = _safe((q.get("src") or [""])[0], (".png", ".gif"))
                full = os.path.join(self.workdir, name)
                sw, sh, _ = L.picture_info(full)
                fw, fh = L.fit_size(sw, sh)
                w = int((q.get("w") or [fw])[0]); h = int((q.get("h") or [fh])[0])
                if not (1 <= w <= 128 and 1 <= h <= 128):
                    raise L.LayoutError("a size from 1 to 128")
                frames = L.load_frames(full, w, h)
                def rgb(px):
                    out = bytearray()
                    for r, g, b, a in px:
                        r, g, b = r * a // 255, g * a // 255, b * a // 255
                        out += bytes((r & 0xF8, g & 0xFC, b & 0xF8))
                    return base64.b64encode(bytes(out)).decode()
                return self._send(200, {"w": w, "h": h, "delays": [d if d >= 2 else 10 for d, _ in frames],
                                        "frames": [rgb(f) for _, f in frames]})
            if path == "/api/default":
                return self._send(200, L.default_doc())
            self._send(404, {"error": "not found"})
        except (OSError, L.LayoutError) as e:
            self._send(400, {"error": str(e)})

    def do_POST(self):
        if not self._local():
            return
        path = self.path.split("?")[0]
        try:
            if path == "/api/image":
                name = _safe(self.headers.get("X-Name"), (".png", ".gif"))
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n)
                tmp = os.path.join(self.workdir, name)
                open(tmp, "wb").write(data)
                try:
                    w, h, n = L.picture_info(tmp)
                    if n > L.FRAMES_MAX:
                        raise L.LayoutError(f"{n} frames: the keyboard plays at most {L.FRAMES_MAX}")
                except Exception as e:
                    os.remove(tmp)
                    raise L.LayoutError(f"{name}: {e}") if not isinstance(e, L.LayoutError) else e
                fw, fh = L.fit_size(w, h)   # bigger than the screen: shrunk to fit, aspect kept
                return self._send(200, {"name": name, "w": w, "h": h, "fit_w": fw, "fit_h": fh, "frames": n})
            doc = self._json()
            if path == "/api/check":
                els, ws = L.resolve(doc.get("layout"), self.workdir, strict=False)
                probs = [{"items": list(items), "why": why} for items, why in L.collisions(els, ws)]
                size = len(L.pack(els, ws))
                return self._send(200, {"ok": not probs, "problems": probs, "error": probs[0]["why"] if probs else None,
                                        "bytes": size, "seconds": round(size / 2700 + 1)})
            if path == "/api/save":
                name = _safe(doc.get("name"), (".json",))
                L.resolve(doc.get("layout"), self.workdir, strict=False)   # a draft may still collide: Upload checks
                with open(os.path.join(self.workdir, name), "w") as f:
                    json.dump(doc["layout"], f, indent=2)
                return self._send(200, {"ok": True, "name": name})
            with self.lock:
                if path == "/api/upload":
                    els, ws = L.resolve(doc.get("layout"), self.workdir)
                    lines = []
                    L.upload(els, ws, log=lines.append)
                    return self._send(200, {"ok": True, "log": "\n".join(lines)})
                if path == "/api/info":
                    b = L.Board()
                    try:
                        return self._send(200, {"ok": True, "log": L.describe(b.info())})
                    finally:
                        b.close()
                if path == "/api/reset":
                    lines = []
                    L.reset(log=lines.append)
                    return self._send(200, {"ok": True, "log": "\n".join(lines)})
            self._send(404, {"error": "not found"})
        except L.LayoutError as e:
            self._send(200, {"ok": False, "error": str(e)})
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def serve(port, workdir, open_browser=True):
    os.makedirs(workdir, exist_ok=True)
    Handler.workdir = workdir
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as srv:
        url = f"http://127.0.0.1:{port}/"
        print(f"layout editor at {url}  (files in {workdir}; Ctrl+C stops it)")
        if open_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AK820 screen editor</title>
<style>
:root{--bg:#f4f4f2;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#d9d9d4;--accent:#2f6fde;--bad:#c62828;--ok:#2e7d32}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#a3a39d;--card:#20201e;--line:#3a3a36;--accent:#6a9cff;--bad:#ff6b6b;--ok:#7bd88f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0 12px 0 0}main{display:flex;gap:20px;padding:20px;flex-wrap:wrap;align-items:flex-start}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
#stage{position:relative;width:520px;height:520px;padding:4px;background:#2b2b28;border-radius:14px}
canvas{image-rendering:pixelated;display:block;width:512px;height:512px;cursor:default}
.side{flex:1;min-width:300px;display:flex;flex-direction:column;gap:14px}
button,select,input{font:inherit;padding:6px 10px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button{cursor:pointer}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
label.small{color:var(--muted);font-size:12px}
#status{padding:8px 10px;border-radius:7px;font-size:13px}#status.ok{color:var(--ok)}#status.bad{color:var(--bad)}
#log{white-space:pre-wrap;font:12px ui-monospace,monospace;color:var(--muted);max-height:140px;overflow:auto}
ul#items{list-style:none;padding:0;margin:0}ul#items li{display:flex;justify-content:space-between;align-items:center;padding:5px 8px;border-radius:6px;cursor:pointer}
ul#items li.hid{color:var(--muted)}ul#items li .tog{padding:2px 8px;font-size:12px}
ul#items li.sel{background:color-mix(in srgb,var(--accent) 18%,transparent)}
.num{width:64px}
</style></head><body>
<header><h1>AK820 screen editor</h1>
<button id="new">New (today's dashboard)</button>
<select id="layouts"><option value="">Open a saved layout…</option></select>
<input id="name" value="my-layout.json" size="18"><button id="save">Save</button>
<span style="flex:1"></span>
<button id="info">Keyboard info</button><button id="reset">Back to default</button>
<button id="upload" class="primary">Upload to keyboard</button></header>
<main>
<div><div id="stage"><canvas id="cv" width="128" height="128"></canvas></div>
<div class="row"><label><input type="checkbox" id="grid" checked> pixel grid</label>
<label><input type="checkbox" id="boxes" checked> outlines</label><span class="small" id="cursor"></span></div></div>
<div class="side">
<div class="card"><div id="status">Loading…</div><div id="log"></div></div>
<div class="card"><b>Elements and widgets</b> <span class="small">click to select, drag anywhere, arrow keys nudge (Shift+arrows change a band's width)</span>
<ul id="items"></ul>
<div class="row"><button id="addlabel">Add label</button>
<label class="small">Add image or GIF <input type="file" id="addimg" accept="image/png,image/gif"></label></div></div>
<div class="card" id="props"></div>
</div></main>
<script>
const Z=4, cv=document.getElementById('cv'), ctx=cv.getContext('2d');
let assets=null, layout=null, sel=null, images={}, checkTimer=null;
const FONT={small:{id:1,adv:6,h:14},large:{id:2,adv:10,h:23}};
const BUILTIN=['os','conn','text','clock','lock','status'], HIDEABLE=BUILTIN;
const NAMES={os:'OS logo',conn:'Connection icons',text:'Song text',clock:'Clock',lock:'Lock row',status:'Battery row'};
const SIZE={os:[24,24],conn:[93,24],text:[128,31],clock:[128,23],lock:[128,23],status:[128,23]};
const BANDS=['text','clock','lock','status'], MINW={text:32,clock:72,lock:40,status:52};
let problems=[], bad=new Set(), keepRatio=true;

async function loadAssets(){
  const b=new Uint8Array(await (await fetch('/assets.bin')).arrayBuffer()), dv=new DataView(b.buffer), out={};
  for(let i=0;i<b[5];i++){const p=8+16*i;const id=dv.getUint16(p,true);
    out[id]={off:b[p+2]|b[p+3]<<8|b[p+4]<<16,fmt:b[p+5],w:dv.getUint16(p+6,true),h:dv.getUint16(p+8,true),cw:b[p+10],ch:b[p+11],first:b[p+12],count:b[p+13]};}
  return {b,idx:out};
}
function px565(v){return [((v>>11)&31)*255/31|0,((v>>5)&63)*255/63|0,(v&31)*255/31|0]}
function blitRaw(img,ox,oy,src,w,h){const b=assets.b;for(let y=0;y<h;y++)for(let x=0;x<w;x++){
  const X=ox+x,Y=oy+y;if(X<0||Y<0||X>127||Y>127)continue;const o=src+2*(y*w+x),c=px565(b[o+1]<<8|b[o]),q=4*(Y*128+X);
  img.data[q]=c[0];img.data[q+1]=c[1];img.data[q+2]=c[2];img.data[q+3]=255}}
function asset(img,id,x,y){const a=assets.idx[id];if(a)blitRaw(img,x,y,a.off,a.w,a.h)}
function glyph(img,id,ch,x,y){const a=assets.idx[id];if(!a)return;const c=ch.charCodeAt(0);if(c<a.first||c>=a.first+a.count)return;
  blitRaw(img,x,y,a.off+(c-a.first)*a.cw*a.ch*2,a.cw,a.ch)}
function text(img,id,s,x,y,right){const a=assets.idx[id];for(let i=0;i<s.length;i++){if(right!==undefined&&x+(i+1)*a.cw>right)break;glyph(img,id,s[i],x+i*a.cw,y)}}
function rect(img,x,y,w,h,c){for(let j=y;j<y+h;j++)for(let i=x;i<x+w;i++){if(i<0||j<0||i>127||j>127)continue;const q=4*(j*128+i);img.data[q]=c[0];img.data[q+1]=c[1];img.data[q+2]=c[2];img.data[q+3]=255}}
function el(k){return layout.elements[k]}
function hidden(k){return !!el(k).hidden}
function bbox(item){ if(typeof item==='string'){const e=el(item),[w,h]=SIZE[item];return [e.x||0,e.y,BANDS.includes(item)?(e.w||128):w,h]}
  const w=layout.widgets[item]; if(w.type==='label'){const f=FONT[w.font||'small'];return [w.x,w.y,f.adv*w.text.length,f.h]}
  return [w.x,w.y,w.w||16,w.h||16]}
function draw(){
  const img=ctx.createImageData(128,128);for(let i=3;i<img.data.length;i+=4)img.data[i]=255;
  const E=layout.elements, W='#fff', white=[255,255,255];
  if(!hidden('os'))asset(img,assets.idx[12]?12:11,E.os.x,E.os.y);
  if(!hidden('conn'))asset(img,9,E.conn.x,E.conn.y);
  // the song text: the play icon at x, the lines at x+14 and x+2, cut at the band's edge (display.c)
  if(!hidden('text')){const t=E.text,r=t.x+t.w;for(let j=0;j<9;j++)for(let i=0;i<=Math.min(j,8-j);i++)rect(img,t.x+3+i,t.y+2+j,1,1,white);
   text(img,1,'Song title here',t.x+14,t.y,r);text(img,1,'Artist name',t.x+2,t.y+15,r)}
  // the clock: its own face, or the 20px one, or the small one 4 rows down, by width
  if(!hidden('clock')){const c=E.clock,s='12:34:56';let id=3,adv=15,y=c.y;if(8*15>c.w){if(8*10<=c.w){id=2;adv=10}else{id=1;adv=6;y=c.y+4}}
   const w=s.length*adv;text(img,id,s,c.x+Math.max(0,(c.w-w)>>1),y,c.x+c.w)}
  if(!hidden('lock')){const l=E.lock,cp=l.w<126,mini=l.w<86,px=l.x+(mini?1:cp?2:4),y=l.y+3;
    rect(img,px+4,y,5,1,[255,224,0]);rect(img,px+3,y+1,7,1,[255,224,0]);rect(img,px+2,y+2,2,6,[255,224,0]);rect(img,px+9,y+2,2,6,[255,224,0]);rect(img,px,y+7,13,9,[255,224,0]);
    if(mini)text(img,1,'C',l.x+17,l.y+4);else if(cp)text(img,1,'CAPS',l.x+18,l.y+4);else text(img,2,'CAPS',l.x+20,l.y)}
  if(!hidden('status')){const st=E.status,x=st.x+5,y=st.y+5;rect(img,x,y,24,1,white);rect(img,x,y+11,24,1,white);rect(img,x,y,1,12,white);rect(img,x+23,y,1,12,white);rect(img,x+24,y+4,2,4,white);rect(img,x+2,y+2,17,8,[80,200,90]);
    const right=st.x+st.w-4,room=right-(st.x+48),txt=room>=60?'85% 6h':room>=30?'85%':'';if(txt)text(img,2,txt,right-txt.length*10,st.y)}
  layout.widgets.forEach(w=>{ if(w.type==='label')text(img,FONT[w.font||'small'].id,w.text,w.x,w.y);
    else{const exact=images[w.src+'|'+w.w+'x'+w.h],im=exact||images[w.src+'|'];if(!exact)want(w);if(im){let k=0;if(im.frames.length>1){let t=(performance.now()/10)%im.total;while(t>=im.delays[k]){t-=im.delays[k];k++}}
      // the exact frames the keyboard gets; while resizing, the nearest pixel of the last ones
      const d=im.frames[k];for(let y=0;y<w.h;y++)for(let x=0;x<w.w;x++){const X=w.x+x,Y=w.y+y;if(X>127||Y>127)continue;const sx=Math.floor(x*im.w/w.w),sy=Math.floor(y*im.h/w.h),s=3*(sy*im.w+sx),q=4*(Y*128+X);
      img.data[q]=d[s];img.data[q+1]=d[s+1];img.data[q+2]=d[s+2]}}}});
  ctx.putImageData(img,0,0); overlay();
}
const ov=document.createElement('canvas');ov.width=ov.height=512;ov.style.cssText='position:absolute;left:4px;top:4px;width:512px;height:512px;pointer-events:none';document.getElementById('stage').appendChild(ov);
function overlay(){const o=ov.getContext('2d');o.clearRect(0,0,512,512);
  if(document.getElementById('grid').checked){o.strokeStyle='rgba(255,255,255,.06)';o.beginPath();for(let i=0;i<=128;i++){o.moveTo(i*Z+.5,0);o.lineTo(i*Z+.5,512);o.moveTo(0,i*Z+.5);o.lineTo(512,i*Z+.5)}o.stroke()}
  const items=[...BUILTIN.filter(k=>!hidden(k)),...layout.widgets.map((_,i)=>i)];
  for(const it of items){const [x,y,w,h]=bbox(it),s=it===sel,b=bad.has(it);if(!s&&!b&&!document.getElementById('boxes').checked)continue;
    if(b){o.fillStyle='rgba(255,60,60,.18)';o.fillRect(x*Z,y*Z,w*Z,h*Z)}
    o.strokeStyle=b?'#ff4040':s?'#6a9cff':(typeof it==='string'?'rgba(255,200,80,.5)':'rgba(120,220,140,.6)');o.lineWidth=(s||b)?2:1;o.strokeRect(x*Z+1,y*Z+1,w*Z-2,h*Z-2);
    if(s&&typeof it==='string'&&BANDS.includes(it)){o.fillStyle='#6a9cff';o.fillRect((x+w)*Z-5,(y+h/2)*Z-8,4,16)}
    if(s&&typeof it==='number'&&layout.widgets[it].type==='image'){o.fillStyle='#6a9cff';o.fillRect((x+w)*Z-9,(y+h)*Z-9,9,9)}}}
function itemsList(){const ul=document.getElementById('items');ul.innerHTML='';
  const add=(it,label,extra)=>{const li=document.createElement('li');const sp=document.createElement('span');sp.textContent=label;li.append(sp);if(extra)li.append(extra);if(it===sel)li.className='sel';else if(typeof it==='string'&&hidden(it))li.className='hid';li.onclick=()=>{sel=it;refresh()};ul.append(li)};
  BUILTIN.forEach(k=>{const b=document.createElement('button');b.className='tog';b.textContent=hidden(k)?'Show':'Hide';
    b.title=hidden(k)?'Put it back on the screen':'Take it off the screen (it stays in this list)';
    b.onclick=ev=>{ev.stopPropagation();el(k).hidden=!hidden(k);if(hidden(k)&&sel===k)sel=null;changed()};add(k,NAMES[k]+(hidden(k)?' (hidden)':''),b)});
  layout.widgets.forEach((w,i)=>add(i,(w.type==='label'?'Label: "'+w.text+'"':'Image: '+w.src)));}
function props(){const p=document.getElementById('props');p.innerHTML='';if(sel===null){p.innerHTML='<span class="small">Select something to edit it.</span>';return}
  const num=(lab,obj,key,max)=>{const l=document.createElement('label');l.className='small';l.textContent=lab+' ';const i=document.createElement('input');i.type='number';i.className='num';i.min=0;i.max=max;i.value=obj[key]??0;i.oninput=()=>{obj[key]=parseInt(i.value)||0;changed()};l.append(i);return l};
  const row=document.createElement('div');row.className='row';
  if(typeof sel==='string'){const e=el(sel);p.innerHTML='<b>'+NAMES[sel]+'</b>';
    row.append(num('x',e,'x',127),num('y',e,'y',127));if(BANDS.includes(sel))row.append(num('width',e,'w',128));
    if(HIDEABLE.includes(sel)){const l=document.createElement('label');const c=document.createElement('input');c.type='checkbox';c.checked=!!e.hidden;c.onchange=()=>{e.hidden=c.checked;changed()};l.append(c,' hidden');row.append(l)}
    if(sel==='text')p.insertAdjacentHTML('beforeend','<div class="small">Hiding it also hides the keyboard\'s messages (connection, alerts, brightness), which show here.</div>');
    p.append(row);if(BANDS.includes(sel))p.insertAdjacentHTML('beforeend',`<div class="small">Drag the blue handle on its right edge to change the width (at least ${MINW[sel]} px). Narrower, it draws smaller: ${{text:'the song steps down to the small font, then its lines are cut',clock:'the clock steps down to a smaller font',lock:'compact labels below 126 px, single letters (C W F) below 86',status:'the battery text is shortened, then left out'}[sel]}.</div>`)}
  else{const w=layout.widgets[sel];p.innerHTML='<b>'+(w.type==='label'?'Label':'Image')+'</b>';row.append(num('x',w,'x',127),num('y',w,'y',127));
    if(w.type==='image'){const base=images[w.src+'|'],ratio=base?base.w/base.h:1;
      const wi=num('width',w,'w',128),hi=num('height',w,'h',128),kc=document.createElement('label');kc.className='small';const k=document.createElement('input');k.type='checkbox';k.checked=keepRatio;k.onchange=()=>{keepRatio=k.checked};kc.append(k,' keep ratio');
      wi.querySelector('input').oninput=e=>{w.w=Math.max(1,Math.min(128,parseInt(e.target.value)||1));if(keepRatio)w.h=Math.max(1,Math.min(128,Math.round(w.w/ratio)));changed()};
      hi.querySelector('input').oninput=e=>{w.h=Math.max(1,Math.min(128,parseInt(e.target.value)||1));if(keepRatio)w.w=Math.max(1,Math.min(128,Math.round(w.h*ratio)));changed()};
      const fit=document.createElement('button');fit.textContent='Full screen';fit.title='As big as fits 128x128, aspect kept, centred';
      fit.onclick=()=>{if(!base)return;const k=Math.min(128/base.w,128/base.h);w.w=Math.max(1,Math.min(128,Math.round(base.w*k)));w.h=Math.max(1,Math.min(128,Math.round(base.h*k)));w.x=(128-w.w)>>1;w.y=(128-w.h)>>1;changed()};
      row.append(wi,hi,kc,fit)}
    if(w.type==='label'){const t=document.createElement('input');t.value=w.text;t.maxLength=21;t.oninput=()=>{w.text=t.value.replace(/[^\x20-\x7e]/g,'');changed()};
      const f=document.createElement('select');f.innerHTML='<option value="small">small (6x14)</option><option value="large">large (10x23)</option>';f.value=w.font||'small';f.onchange=()=>{w.font=f.value;changed()};row.append(t,f)}
    const d=document.createElement('button');d.textContent='Remove';d.onclick=()=>{layout.widgets.splice(sel,1);sel=null;changed()};row.append(d);p.append(row)}}
function refresh(){draw();itemsList();props()}
function changed(){refresh();clearTimeout(checkTimer);checkTimer=setTimeout(check,150)}
async function api(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-AK820':'1'},body:JSON.stringify(body||{})});return r.json()}
function status(ok,msg){const s=document.getElementById('status');s.className=ok?'ok':'bad';s.textContent=msg;document.getElementById('upload').disabled=!ok}
async function check(){const r=await api('/api/check',{layout});
  problems=r.problems||[];bad=new Set(problems.flatMap(p=>p.items));overlay();
  if(r.ok)status(true,`Valid. ${r.bytes} bytes, about ${r.seconds} s to upload.`);
  else status(false,problems.length?problems.map(p=>p.why).slice(0,3).join('. ')+(problems.length>3?` (and ${problems.length-3} more)`:'')+'. Fix the red ones to upload.':r.error)}
function log(t){document.getElementById('log').textContent=t||''}
async function fetchFrames(src,w,h){const r=await (await fetch('/api/frames?src='+encodeURIComponent(src)+(w?`&w=${w}&h=${h}`:''))).json();if(r.error)return null;
  const frames=r.frames.map(b=>Uint8Array.from(atob(b),c=>c.charCodeAt(0)));return {w:r.w,h:r.h,frames,delays:r.delays,total:r.delays.reduce((a,b)=>a+b,0)}}
// images[src+'|'] is the fitted size, the fallback; images[src+'|WxH'] the exact frames at a widget's size
async function loadImage(src){try{const f=await fetchFrames(src);if(f)images[src+'|']=f}catch(e){}}
const wanted={};let wantTimer=null;
function want(w){const k=w.src+'|'+w.w+'x'+w.h;if(wanted[k])return;wanted[k]=1;clearTimeout(wantTimer);
  wantTimer=setTimeout(async()=>{const f=await fetchFrames(w.src,w.w,w.h).catch(()=>null);if(f){images[k]=f;draw()}delete wanted[k]},drag?400:0)}
// GIFs play in the preview at their own speed
(function tick(){if(layout&&layout.widgets.some(w=>w.type==='image'&&images[w.src+'|']&&images[w.src+'|'].frames.length>1))draw();setTimeout(tick,40)})();
async function setLayout(doc){layout={elements:Object.assign({},(await (await fetch('/api/default')).json()).elements),widgets:[]};
  for(const k of BUILTIN)layout.elements[k]=Object.assign({},layout.elements[k],(doc.elements||{})[k]||{});
  layout.widgets=(doc.widgets||[]).map(w=>Object.assign({},w));for(const w of layout.widgets)if(w.type==='image'){await loadImage(w.src);const f=images[w.src+'|'];if(f&&!w.w){w.w=f.w;w.h=f.h}}sel=null;changed()}
async function files(){const r=await (await fetch('/api/files')).json();const s=document.getElementById('layouts');s.length=1;r.layouts.forEach(n=>s.add(new Option(n,n)))}
// dragging
let drag=null;
cv.onmousedown=e=>{const r=cv.getBoundingClientRect(),x=(e.clientX-r.left)/Z|0,y=(e.clientY-r.top)/Z|0;
  const items=[...layout.widgets.map((_,i)=>i).reverse(),...BUILTIN.filter(k=>!hidden(k))];
  const fx=(e.clientX-r.left)/Z;
  if(typeof sel==='string'&&BANDS.includes(sel)){const [bx,by,bw,bh]=bbox(sel);if(Math.abs(fx-(bx+bw))<1.5&&y>=by&&y<by+bh){drag={it:sel,resize:true};return}}
  const fy=(e.clientY-r.top)/Z;
  if(typeof sel==='number'&&layout.widgets[sel].type==='image'){const [bx,by,bw,bh]=bbox(sel);if(Math.abs(fx-(bx+bw))<2.5&&Math.abs(fy-(by+bh))<2.5){const b=images[layout.widgets[sel].src+'|'];drag={it:sel,corner:true,ratio:b?b.w/b.h:bw/bh};return}}
  for(const it of items){const [bx,by,bw,bh]=bbox(it);if(x>=bx&&x<bx+bw&&y>=by&&y<by+bh){sel=it;const o=typeof it==='string'?el(it):layout.widgets[it];drag={it,dx:x-(o.x||0),dy:y-o.y};refresh();return}}
  sel=null;refresh()};
window.onmousemove=e=>{const r=cv.getBoundingClientRect(),x=Math.floor((e.clientX-r.left)/Z),y=Math.floor((e.clientY-r.top)/Z);
  if(x>=0&&y>=0&&x<128&&y<128)document.getElementById('cursor').textContent=`x ${x}, y ${y}`;
  if(typeof sel==='string'&&BANDS.includes(sel)&&!drag){const [bx,by,bw,bh]=bbox(sel),fx=(e.clientX-r.left)/Z;cv.style.cursor=(Math.abs(fx-(bx+bw))<1.5&&y>=by&&y<by+bh)?'ew-resize':'default'}
  if(typeof sel==='number'&&layout.widgets[sel]&&layout.widgets[sel].type==='image'&&!drag){const [bx,by,bw,bh]=bbox(sel),fx=(e.clientX-r.left)/Z,fy=(e.clientY-r.top)/Z;cv.style.cursor=(Math.abs(fx-(bx+bw))<2.5&&Math.abs(fy-(by+bh))<2.5)?'nwse-resize':'default'}
  if(!drag)return;const o=typeof drag.it==='string'?el(drag.it):layout.widgets[drag.it];
  if(drag.resize){o.w=Math.max(1,Math.min(128-o.x,Math.round((e.clientX-r.left)/Z)-o.x));changed();return}
  if(drag.corner){let nw=Math.max(1,Math.min(128-o.x,Math.round((e.clientX-r.left)/Z)-o.x)),nh=Math.max(1,Math.min(128-o.y,Math.round((e.clientY-r.top)/Z)-o.y));
    if(keepRatio&&!e.shiftKey){nh=Math.max(1,Math.round(nw/drag.ratio));if(o.y+nh>128){nh=128-o.y;nw=Math.max(1,Math.round(nh*drag.ratio))}}o.w=nw;o.h=nh;changed();return}
  const [,,bw,bh]=bbox(drag.it);o.x=Math.max(0,Math.min(128-Math.min(bw,128),x-drag.dx));o.y=Math.max(0,Math.min(128-Math.min(bh,128),y-drag.dy));changed()};
window.onmouseup=()=>{const was=drag;drag=null;if(was&&was.corner)draw()};
window.onkeydown=e=>{if(sel===null||['INPUT','SELECT'].includes(document.activeElement.tagName))return;const o=typeof sel==='string'?el(sel):layout.widgets[sel];
  const d={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];
  if(d){e.preventDefault();if(e.shiftKey&&typeof sel==='string'&&BANDS.includes(sel))o.w=Math.max(1,Math.min(128-o.x,o.w+d[0]));else{o.x=Math.max(0,Math.min(127,(o.x||0)+d[0]));o.y=Math.max(0,Math.min(127,o.y+d[1]))}changed()}
  if((e.key==='Delete'||e.key==='Backspace')&&typeof sel==='number'){layout.widgets.splice(sel,1);sel=null;changed()}};
document.getElementById('grid').onchange=document.getElementById('boxes').onchange=overlay;
document.getElementById('new').onclick=()=>setLayout({});
document.getElementById('layouts').onchange=async e=>{if(!e.target.value)return;document.getElementById('name').value=e.target.value;setLayout(await (await fetch('/file/'+encodeURIComponent(e.target.value))).json())};
document.getElementById('addlabel').onclick=()=>{layout.widgets.push({type:'label',x:2,y:90,text:'hello',font:'small'});sel=layout.widgets.length-1;changed()};
document.getElementById('addimg').onchange=async e=>{const f=e.target.files[0];if(!f)return;const name=f.name.replace(/[^A-Za-z0-9._ -]/g,'_');
  const r=await (await fetch('/api/image',{method:'POST',headers:{'X-Name':name,'X-AK820':'1'},body:f})).json();e.target.value='';
  if(r.error||r.ok===false){status(false,r.error);return}await loadImage(r.name);layout.widgets.push({type:'image',x:0,y:0,w:r.fit_w,h:r.fit_h,src:r.name});
  if(r.fit_w!==r.w||r.fit_h!==r.h)log(`${r.name} is ${r.w}x${r.h}: shrunk to ${r.fit_w}x${r.fit_h} to fit the screen. Drag its corner to resize.`);sel=layout.widgets.length-1;changed()};
document.getElementById('save').onclick=async()=>{let n=document.getElementById('name').value.trim();if(!n.endsWith('.json'))n+='.json';const r=await api('/api/save',{name:n,layout});
  if(r.ok){log('saved '+r.name);files()}else status(false,r.error)};
for(const [id,path,busy] of [['upload','/api/upload','Uploading… (the agent is paused meanwhile)'],['info','/api/info','Asking the keyboard…'],['reset','/api/reset','Erasing the layout slots…']])
  document.getElementById(id).onclick=async()=>{log(busy);const r=await api(path,{layout});log(r.ok?r.log:'');if(!r.ok)status(false,r.error);else check()};
(async()=>{assets=await loadAssets();await files();await setLayout({})})();
</script></body></html>
"""
