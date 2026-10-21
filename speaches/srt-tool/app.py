#!/usr/bin/env python3
# PRO-337 — srt-tool: klickbare Upload-Seite für Video→SRT/VTT/Text.
# Siehe docker-compose.yml (Kommentarblock oben).
#   Browser -> https://srt.mueller-nas.de (Traefik + authentik) -> diese Seite
#   Browser -> https://srt.mueller-nas.de/v1/* (Traefik + whisper-unload) -> speaches-srt
# API-Key: Auto-Inject — /session (SSO-geschuetzt via authentik-Middleware) liefert den
# Key aus der Container-Umgebung (docker-compose: SPEACHES_API_KEY), die Seite legt ihn im
# Browser-LocalStorage ab. Manuelles Fieldset bleibt als Fallback. Nicht ins Repo/Logs.
#
# 12.10.2026 — Baustein 2: Verarbeitungs-Fortschritt in Prozent (ETA-vernessen).
#   Befund (SSE-Probe 13:26 + Quellcode executors/whisper.py): der Server-Stream liefert
#   NUR Text-Deltas, keine Zeitstempel — "echter" Prozent via Timing deshalb unmoeglich.
#   Kalibrierte Formel: ETA[s] = 8 + 0.018 * Audiolaenge[s]
#   (Serverlog 13:00:34: 1964.8s Audio in 24.5s Transkription + 8.7s VAD => ~0.017-0.018 RTF).
#   Prozent cap bei 97 % bis das Ergebnis da ist; ohne auslesbare Dauer nur Laufzeitanzeige.
#   XHR-Upload mit Fortschrittsbalken (% + MB/GB), Drag&Drop, 3-Phasen-Stepper
#   (Upload / Whisper-Verarbeitung / Download), Ergebnis-Karte (Download/Kopieren/Neue Datei),
#   Klartext-Fehlerboxen mit Rohtext per <details>, Datei-Entfernen/Reset.
#   Flask, /session, /healthz und der direkte Browser->speaches-srt-Post sind unveraendert.
from flask import Flask, Response, jsonify
import os

HTML = r"""<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SRT-Tool — Video → Untertitel</title>
<style>
 :root{--blue:#0078d4;--green:#1a7f37;--red:#8b1d1d;--bg:#f6f8fa;--line:#d0d7de;--muted:#57606a}
 *{box-sizing:border-box}
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:1.5rem 1rem 3rem;background:var(--bg);color:#1f2328}
 .card{max-width:680px;margin:0 auto;background:#fff;border:1px solid var(--line);border-radius:12px;padding:1.4rem;box-shadow:0 1px 3px rgba(0,0,0,.07)}
 h1{font-size:1.25rem;margin:.1rem 0 .35rem}
 .hint{color:var(--muted);font-size:.9rem;margin:.2rem 0 1.1rem;line-height:1.45}
 button{font-family:inherit;font-size:1rem;border-radius:8px;cursor:pointer}
 .primary{background:var(--blue);color:#fff;border:0;min-height:44px;padding:.6rem 1.4rem}
 .primary:disabled{opacity:.55;cursor:default}
 .ghost{background:#fff;border:1px solid var(--line);color:#1f2328;min-height:40px;padding:.4rem .8rem}
 .ghost:hover{background:#f3f4f6}
 .row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
 .dropzone{border:2px dashed var(--line);border-radius:10px;padding:1.5rem 1rem;text-align:center;cursor:pointer;color:var(--muted);transition:border-color .15s,background .15s;user-select:none}
 .dropzone.over{border-color:var(--blue);background:#eaf3fb;color:var(--blue)}
 .dropzone.has{border-style:solid;border-color:var(--green);color:#1f2328;text-align:left;background:#f0fff4}
 #dropHint .small{font-size:.82rem;color:#8c959f}
 #finfo{display:flex;gap:.7rem;align-items:center;justify-content:space-between;font-weight:600}
 .opt{display:flex;gap:1rem;flex-wrap:wrap;margin-top:1rem}
 .opt label{font-size:.85rem;color:var(--muted);display:flex;flex-direction:column;gap:.3rem}
 .opt input,.opt select{max-width:100%;padding:.55rem .6rem;border:1px solid var(--line);border-radius:8px;font-size:1rem;background:#fff;color:#1f2328}
 #lang{width:9ch}
 details.keybox{margin-top:1.2rem;border:1px solid var(--line);border-radius:10px;padding:.55rem .8rem}
 details.keybox summary{cursor:pointer;color:var(--muted);font-size:.9rem}
 details.keybox input{width:100%;margin-top:.55rem;padding:.55rem .6rem;border:1px solid var(--line);border-radius:8px;font-size:1rem;display:block}
 .steps{display:flex;gap:.6rem;list-style:none;padding:0;margin:1.2rem 0 0}
 .steps li{flex:1;text-align:center;font-size:.8rem;color:var(--muted)}
 .steps li::before{content:attr(data-num);display:inline-flex;width:1.7rem;height:1.7rem;border-radius:50%;border:2px solid var(--line);align-items:center;justify-content:center;background:#fff;font-weight:700;margin-bottom:.25rem}
 .steps li.active::before{border-color:var(--blue);color:var(--blue)}
 .steps li.done::before{content:"✓";border-color:var(--green);background:var(--green);color:#fff}
 .prog{margin-top:1rem}
 .bar{height:10px;border-radius:6px;background:#eaeef2;overflow:hidden}
 .bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--blue),#6cb8f7);transition:width .2s}
 #progText{display:block;margin-top:.4rem;font-size:.88rem;color:var(--muted)}
 .err{margin-top:1rem;border:1px solid #e3a7ac;background:#fff1f2;color:var(--red);border-radius:10px;padding:.8rem 1rem;font-size:.92rem}
 .err details{margin-top:.6rem}
 .err summary{cursor:pointer;font-size:.85rem}
 .err pre{white-space:pre-wrap;word-break:break-word;font-size:.78rem;color:#5a1719}
 .result{margin-top:1rem;border:1px solid #bfe3c6;background:#f0fff4;border-radius:10px;padding:.9rem 1rem}
 .okline{color:var(--green);font-weight:600}
 .okline .resSize{font-weight:400;color:var(--muted);font-size:.9rem}
 .downloads{margin-top:.8rem}
</style></head>
<body>
<div class="card">
 <h1>Video → Untertitel (SRT / VTT / Text)</h1>
 <p class="hint">Der Upload läuft an die zentrale Whisper-Instanz. Whisper-Turbo lädt beim ersten Job selbst (dauert ~10–20 s länger) und entlädt sich nach 5 Minuten ohne Nutzung wieder aus dem VRAM.</p>

 <div id="drop" class="dropzone">
   <div id="dropHint">Datei hierher ziehen — oder klicken zum Auswählen<br><span class="small">Video oder Audio (MP4, MKV, MOV, MP3, WAV …)</span></div>
   <input id="file" type="file" accept="video/*,audio/*" style="display:none">
   <div id="finfo" hidden><span id="fmeta"></span><button id="clear" type="button" class="ghost">Entfernen</button></div>
 </div>

 <div class="opt">
  <label>Sprache (leer = automatisch)<input id="lang" type="text" placeholder="de"></label>
  <label>Ausgabeformat<select id="fmt">
    <option value="srt" selected>SRT (Untertitel)</option>
    <option value="vtt">VTT (Web-Untertitel)</option>
    <option value="text">reiner Text</option></select></label>
 </div>

 <button id="go" type="button" class="primary" style="margin-top:1.1rem;width:100%">Transkribieren</button>

 <details class="keybox" id="keyBox"><summary id="keySum">API-Key (Fallback — manuell einfügen)</summary>
  <input id="key" type="password" placeholder="API-Key" autocomplete="off">
 </details>

 <ol id="steps" class="steps" hidden>
  <li data-num="1">Upload</li><li data-num="2">Whisper verarbeitet</li><li data-num="3">Download</li>
 </ol>

 <div id="prog" class="prog" hidden>
  <div class="bar"><i id="progBar"></i></div><span id="progText"></span>
 </div>

 <div id="error" class="err" hidden></div>

 <div id="result" class="result" hidden>
  <div class="okline">Fertig ✓ — <span id="resName"></span> <span id="resSize" class="resSize"></span></div>
  <div class="row downloads">
   <button id="dl" type="button" class="primary">Download</button>
   <button id="copy" type="button" class="ghost">Kopieren</button>
   <button id="again" type="button" class="ghost">Neue Datei</button>
  </div>
 </div>
</div>

<script>
"use strict";
const K="srt_tool_key";
const $=id=>document.getElementById(id);
const MODEL="deepdml/faster-whisper-large-v3-turbo-ct2";
const fmtSize=b=> b>=1e9 ? (b/1e9).toLocaleString("de-DE",{maximumFractionDigits:2})+" GB"
              : b>=1e6 ? (b/1e6).toLocaleString("de-DE",{maximumFractionDigits:1})+" MB"
              : Math.max(1,Math.round(b/1024))+" kB";
let file=null, blobObj=null, blobUrl=null, timer=null, fileDur=null;
const fmtClock=s=>{ s=Math.max(0,Math.round(s)); const h=Math.floor(s/3600), m=Math.floor(s%3600/60), ss=s%60;
  return (h ? h+":"+String(m).padStart(2,"0") : m ? m+":"+String(ss).padStart(2,"0") : "0:"+String(ss).padStart(2,"0")); };

function probeDuration(f){
  try{
    const isVid = (f.type&&f.type.startsWith("video/")) || /\.(mp4|mkv|mov|avi|webm|m4v)$/i.test(f.name);
    const el=document.createElement(isVid?"video":"audio");
    const url=URL.createObjectURL(f);
    const done=ok=>{ el.onloadedmetadata=el.onerror=el.ondurationchange=null;
      URL.revokeObjectURL(url); el.remove();
      try{
        if(ok && isFinite(el.duration) && el.duration>0){
          fileDur=el.duration;
          $("fmeta").textContent=f.name+" — "+fmtSize(f.size)+" — Dauer "+fmtClock(fileDur);
        }
      }catch(e){}
    };
    el.preload="metadata"; el.muted=true; el.src=url;
    el.onloadedmetadata=()=>done(true);
    el.onerror=()=>done(false);
    el.ondurationchange=()=>{ // Infinity-Fallback (z.B. mkv/Streaming-Container)
      if(el.duration===Infinity){ el.currentTime=1e101; setTimeout(()=>{ el.currentTime=0; }, 80); }
    };
    setTimeout(()=>done(!!(el.duration&&isFinite(el.duration))),5000);
  }catch(e){}
}
const stepEls=[...document.querySelectorAll("#steps li")];

function setStep(n){
  stepEls.forEach((el,i)=>{ el.classList.toggle("done",i<n-1); el.classList.toggle("active",i===n-1); });
}
function setBar(p){ $("progBar").style.width=p+"%"; }
function flash(btn,msg){ const o=btn.textContent; btn.textContent=msg; setTimeout(()=>btn.textContent=o,1600); }

function showErr(msg,raw){
  if(timer){clearInterval(timer);timer=null;}
  $("prog").hidden=true; $("steps").hidden=true;
  const box=$("error"); box.hidden=false; box.textContent="";
  const head=document.createElement("div"); head.textContent="⚠ "+msg; box.appendChild(head);
  if(raw){
    const det=document.createElement("details");
    const sum=document.createElement("summary"); sum.textContent="Technische Details";
    const pre=document.createElement("pre"); pre.textContent=raw;
    det.appendChild(sum); det.appendChild(pre); box.appendChild(det);
  }
  $("go").disabled=false;
}

function uiIdle(){
  if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;blobObj=null;}
  file=null; fileDur=null; $("file").value=""; $("drop").classList.remove("has");
  $("finfo").hidden=true; $("dropHint").hidden=false;
  $("go").disabled=false; $("steps").hidden=true; $("prog").hidden=true; $("error").hidden=true; $("result").hidden=true;
  stepEls.forEach(el=>el.classList.remove("done","active"));
}

/* --- API-Key: Auto-Inject, sonst Fallback-Feld aufklappen --- */
$("key").value=localStorage.getItem(K)||"";
(function initKey(){
  const done=auto=>{
    if($("key").value){ $("keySum").textContent=auto?"API-Key — automatisch übernommen ✓":"API-Key — vorhanden ✓"; $("keyBox").open=false; }
    else { $("keySum").textContent="API-Key (Fallback — manuell einfügen)"; $("keyBox").open=true; }
  };
  done(false);
  (async()=>{
    try{
      const r=await fetch("/session");
      if(!r.ok) return;
      const d=await r.json();
      if(d.api_key){ localStorage.setItem(K,d.api_key); $("key").value=d.api_key; done(true); }
    }catch(e){}
  })();
})();

/* --- Datei wählen: Klick oder Drag & Drop --- */
const dz=$("drop"), fi=$("file");
function setFile(f){
  if(!f) return;
  file=f; fileDur=null;
  $("fmeta").textContent=f.name+" — "+fmtSize(f.size);
  $("drop").classList.add("has"); $("dropHint").hidden=true; $("finfo").hidden=false;
  $("error").hidden=true;
  probeDuration(f);
}
fi.addEventListener("change",()=>setFile(fi.files[0]));
$("clear").addEventListener("click",e=>{ e.stopPropagation(); uiIdle(); });
dz.addEventListener("click",()=>fi.click());
["dragover","dragenter"].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add("over");}));
["dragleave","dragend"].forEach(ev=>dz.addEventListener(ev,()=>dz.classList.remove("over")));
dz.addEventListener("drop",e=>{
  e.preventDefault(); dz.classList.remove("over");
  if(e.dataTransfer.files&&e.dataTransfer.files[0]){
    try{ fi.files=e.dataTransfer.files; }catch(err){}
    setFile(e.dataTransfer.files[0]);
  }
});

/* --- Fehlerhinweise --- */
function hintFor(s){
  if(s===0) return "Netzwerkfehler — Server nicht erreicht. Erneut versuchen (ggf. Netzwerk/Traefik prüfen).";
  if(s===401||s===403) return "API-Key fehlt oder ist falsch — bitte unten unter „API-Key“ korrigieren.";
  if(s===413) return "Datei zu groß für das Server-Upload-Limit.";
  if(s===429) return "Server ist beschäftigt — kurz warten und erneut senden.";
  if(s===502||s===503||s===504) return "Whisper-Server startet oder ist überlastet (Cold Start ~10–20 s) — kurz warten, dann erneut senden.";
  if(s>=500) return "Serverfehler bei der Transkription — Details unten.";
  return null;
}

/* --- Hauptablauf: XHR (einziger Weg zum echten Upload-Fortschritt) --- */
$("go").addEventListener("click",()=>{
  if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null;blobObj=null;}
  const key=$("key").value.trim();
  if(!key){ showErr("API-Key fehlt — bitte unten unter „API-Key“ einfügen."); $("keyBox").open=true; return; }
  localStorage.setItem(K,key);
  if(!file){ showErr("Keine Datei ausgewählt — Datei hierher ziehen oder klicken."); return; }
  const fmt=$("fmt").value, lang=$("lang").value.trim();
  const outName=file.name.replace(/\.[^.]+$/,"")+"."+(fmt==="text"?"txt":fmt);

  const fd=new FormData();
  fd.append("file",file);
  fd.append("model",MODEL);
  if(lang) fd.append("language",lang);
  fd.append("response_format",fmt);

  $("error").hidden=true; $("result").hidden=true;
  $("steps").hidden=false; setStep(1);
  $("prog").hidden=false; setBar(0); $("progText").textContent="Upload startet …";
  $("go").disabled=true;

  const xhr=new XMLHttpRequest();
  let uploaded=false, t0=Date.now();
  timer=setInterval(()=>{
    if(uploaded){
      const sec=(Date.now()-t0)/1000;
      if(fileDur){
        const p=Math.min(97,Math.round(sec/(8+0.018*fileDur)*100));
        setBar(p);
        $("progText").textContent="Whisper verarbeitet … ≈"+p+" %  (Dauer "+fmtClock(fileDur)+" · Lauf "+fmtClock(sec)+")";
      }else{
        $("progText").textContent="Whisper verarbeitet … ("+fmtClock(sec)+") — Dauer nicht auslesbar, Prozent nicht verfügbar";
      }
    }
  },1000);

  xhr.open("POST","/v1/audio/transcriptions");
  xhr.setRequestHeader("Authorization","Bearer "+key);
  xhr.upload.onprogress=e=>{
    if(e.lengthComputable){
      $("progText").textContent="Upload … "+Math.round(e.loaded/e.total*100)+" % ("+fmtSize(e.loaded)+" / "+fmtSize(e.total)+")";
      setBar(e.loaded/e.total*100);
    }
  };
  xhr.upload.onloadend=()=>{
    uploaded=true; t0=Date.now(); setBar(0);
    $("progText").textContent="Whisper verarbeitet … (Cold Start möglich)";
    setStep(2);
  };
  xhr.onloadend=()=>{
    if(timer){clearInterval(timer);timer=null;}
    const okStatus=(xhr.status>=200&&xhr.status<300);
    if(okStatus){
      blobObj=new Blob([xhr.responseText],{type:"text/plain;charset=utf-8"});
      blobUrl=URL.createObjectURL(blobObj);
      $("resName").textContent=outName;
      $("resSize").textContent="("+fmtSize(blobObj.size)+")";
      setStep(4); $("prog").hidden=true; $("error").hidden=true; $("result").hidden=false;
    }else{
      showErr(hintFor(xhr.status)||"Fehler "+(xhr.status||0),(xhr.responseText||"").slice(0,2000));
    }
  };
  xhr.send(fd);
});

/* --- Ergebnis-Aktionen --- */
$("dl").addEventListener("click",()=>{
  if(!blobUrl) return;
  const a=document.createElement("a"); a.href=blobUrl; a.download=$("resName").textContent;
  document.body.appendChild(a); a.click(); a.remove();
});
$("copy").addEventListener("click",async()=>{
  if(!blobObj) return;
  let txt="";
  try{ txt=await blobObj.text(); }catch(e){}
  try{
    await navigator.clipboard.writeText(txt); flash($("copy"),"Kopiert ✓");
  }catch(e){
    const ta=document.createElement("textarea"); ta.value=txt; document.body.appendChild(ta); ta.select();
    try{ document.execCommand("copy"); flash($("copy"),"Kopiert ✓"); }catch(e2){}
    ta.remove();
  }
});
$("again").addEventListener("click",uiIdle);
</script>
</body></html>"""

app = Flask(__name__)

@app.route("/")
def index():
    return Response(HTML, content_type="text/html; charset=utf-8")

@app.route("/session")
def session():
    # Nur nutzbar, wenn der Besucher die Seite (SSO) erreichen konnte —
    # das Routing: Host(srt.mueller-nas.de) ohne /v1 -> authentik@docker -> hierher.
    return jsonify({"api_key": os.environ.get("SPEACHES_API_KEY", "")})

@app.route("/healthz")
def healthz():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
