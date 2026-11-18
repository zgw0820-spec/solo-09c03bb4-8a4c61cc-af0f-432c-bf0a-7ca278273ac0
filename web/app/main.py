"""运维操作网页后端：聚合两台代理状态、转发诊断、切换模拟后端模式。"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

PROXY_A = os.environ.get("PROXY_A_URL", "http://proxy-a:8000")
PROXY_B = os.environ.get("PROXY_B_URL", "http://proxy-b:8000")
BACKEND = os.environ.get("BACKEND_URL", "http://backend:8000")
HTTP_TIMEOUT = float(os.environ.get("WEB_HTTP_TIMEOUT", "6"))

app = FastAPI(title="设备诊断熔断演练台")
PROXIES = {"a": PROXY_A, "b": PROXY_B}


class DiagnoseBody(BaseModel):
    device_id: str
    proxy: str = "a"


class ModeBody(BaseModel):
    mode: str


async def _get(url: str) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url)
            return resp.status_code, resp.json()
    except httpx.HTTPError as exc:
        return 503, {"ok": False, "error": "UPSTREAM_UNREACHABLE", "message": str(exc)}


async def _post(url: str, json: dict | None = None) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=json)
            return resp.status_code, resp.json()
    except httpx.HTTPError as exc:
        return 503, {"ok": False, "error": "UPSTREAM_UNREACHABLE", "message": str(exc)}


@app.get("/api/overview")
async def overview() -> dict:
    proxies: dict[str, dict] = {}
    for name, base in PROXIES.items():
        health_code, health = await _get(f"{base}/health")
        cb_code, cb = await _get(f"{base}/cb/status")
        proxies[name] = {
            "base": base,
            "health": health,
            "health_http": health_code,
            "cb": cb,
            "cb_http": cb_code,
        }
    _, backend_mode = await _get(f"{BACKEND}/admin/mode")
    # 两台代理看到的是同一份 Redis 状态，任一可用即可作为全局视图
    shared = next(
        (p["cb"] for p in proxies.values() if p["cb_http"] == 200),
        {"state": "UNKNOWN"},
    )
    return {"proxies": proxies, "backend_mode": backend_mode, "shared_cb": shared}


@app.post("/api/diagnose")
async def diagnose(body: DiagnoseBody) -> dict:
    base = PROXIES.get(body.proxy)
    if base is None:
        return {"ok": False, "error": f"未知代理：{body.proxy}（应为 a 或 b）"}
    code, payload = await _get(f"{base}/diagnose/{body.device_id}")
    return {"http_status": code, **payload}


@app.post("/api/backend/mode")
async def backend_mode(body: ModeBody) -> dict:
    code, payload = await _post(f"{BACKEND}/admin/mode", {"mode": body.mode})
    return {"http_status": code, **payload}


@app.post("/api/reset")
async def reset() -> dict:
    results = {}
    for name, base in PROXIES.items():
        code, payload = await _post(f"{base}/cb/reset")
        results[name] = {"http_status": code, **payload}
    return {"results": results}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>设备诊断熔断演练台</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
         background:#0f1420; color:#e6e9ef; }
  header { padding:20px 28px; background:#161d2e; border-bottom:1px solid #243049; }
  header h1 { margin:0; font-size:20px; }
  header p { margin:6px 0 0; color:#8b96ad; font-size:13px; }
  main { display:grid; grid-template-columns: 380px 1fr; gap:18px; padding:22px 28px; }
  .card { background:#161d2e; border:1px solid #243049; border-radius:12px; padding:18px; }
  .card h2 { margin:0 0 14px; font-size:15px; color:#9db2d8; }
  label { display:block; font-size:13px; color:#aeb8cc; margin:10px 0 6px; }
  input, select { width:100%; padding:10px 12px; border-radius:8px;
        border:1px solid #2c3a57; background:#0f1420; color:#e6e9ef; font-size:14px; }
  button { cursor:pointer; border:0; border-radius:8px; padding:10px 16px;
        font-size:14px; font-weight:600; margin-top:12px; }
  .primary { background:#3b82f6; color:#fff; width:100%; }
  .primary:hover { background:#2f6fe0; }
  .row { display:flex; gap:10px; }
  .row button { flex:1; }
  .normal { background:#16a34a; color:#fff; }
  .timeout { background:#dc2626; color:#fff; }
  .ghost { background:#2c3a57; color:#cdd6ea; }
  .badge { display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:700; }
  .b-CLOSED { background:#14532d; color:#86efac; }
  .b-OPEN { background:#7f1d1d; color:#fca5a5; }
  .b-HALF_OPEN { background:#78350f; color:#fcd34d; }
  .b-UNKNOWN { background:#374151; color:#d1d5db; }
  .kv { display:grid; grid-template-columns:120px 1fr; gap:6px 10px; font-size:13px; margin-top:8px; }
  .kv .k { color:#8b96ad; }
  .proxy-box { border:1px solid #243049; border-radius:10px; padding:12px; margin-top:10px; }
  .proxy-box h3 { margin:0 0 6px; font-size:13px; display:flex; justify-content:space-between; }
  .ok { color:#4ade80; } .bad { color:#f87171; } .warn { color:#fbbf24; }
  pre { background:#0b1020; border:1px solid #243049; border-radius:10px;
        padding:14px; font-size:12.5px; overflow:auto; max-height:46vh; white-space:pre-wrap; }
  .meta { font-size:12px; color:#8b96ad; margin:4px 0 10px; }
  .pill { font-size:11px; padding:2px 8px; border-radius:6px; background:#243049; color:#aeb8cc; }
  @media (max-width: 980px){ main{grid-template-columns:1fr;} }
</style>
</head>
<body>
<header>
  <h1>设备诊断 · 双代理熔断器演练台</h1>
  <p>两台代理共享 Redis 熔断状态：合计连续 3 次超时即熔断，冷却 5 秒后半开只放一个探测，探测成功才恢复。</p>
</header>
<main>
  <section>
    <div class="card">
      <h2>① 模拟后端模式</h2>
      <div class="row">
        <button class="normal" onclick="setMode('normal')">切到正常</button>
        <button class="timeout" onclick="setMode('timeout')">切到超时</button>
      </div>
      <p class="meta" id="backendMode">当前模式：读取中…</p>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>② 发起真实诊断</h2>
      <label>设备号</label>
      <input id="deviceId" value="DEV-10086" placeholder="例如 DEV-10086">
      <label>入口代理（两代理共享同一熔断器）</label>
      <select id="proxySel"><option value="a">代理 A (proxy-a)</option><option value="b">代理 B (proxy-b)</option></select>
      <button class="primary" onclick="diagnose()">发起诊断</button>
      <button class="ghost" style="width:100%" onclick="resetCb()">复位熔断器（管理操作）</button>
    </div>
  </section>
  <section>
    <div class="card">
      <h2>③ 熔断与代理状态 <span class="pill">每 1 秒自动刷新</span></h2>
      <div id="cbPanel">加载中…</div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>④ 最近一次响应</h2>
      <p class="meta" id="respMeta">尚未发起请求</p>
      <pre id="respBox">{}</pre>
    </div>
  </section>
</main>
<script>
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function badge(state){ return `<span class="badge b-${state}">${state}</span>`; }
async function refresh(){
  const r = await fetch('/api/overview'); const d = await r.json();
  const cb = d.shared_cb || {};
  const remain = cb.cooldown_remaining_ms ? (cb.cooldown_remaining_ms/1000).toFixed(1)+' s' : '—';
  let html = `<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <div>全局熔断状态：${badge(cb.state || 'UNKNOWN')}</div>
      <div class="meta" style="margin:0">连续超时：<b>${cb.failures ?? '—'}</b> / 3</div>
      <div class="meta" style="margin:0">冷却剩余：<b>${remain}</b></div>
      <div class="meta" style="margin:0">代数 generation：<b>${cb.generation ?? '—'}</b></div>
      <div class="meta" style="margin:0">探测中：<b>${cb.probe_in_progress ? '是（'+esc(cb.probe_owner)+'）' : '否'}</b></div>
    </div>`;
  for (const [name, p] of Object.entries(d.proxies)) {
    const redisOk = p.health?.redis_ok;
    const state = p.cb?.state || 'UNKNOWN';
    html += `<div class="proxy-box">
      <h3><span>代理 ${name.toUpperCase()}（${esc(p.base)}）</span>
        <span class="${redisOk ? 'ok':'bad'}">Redis ${redisOk ? '连通' : '不可用'}</span></h3>
      <div class="kv">
        <span class="k">熔断视图</span><span>${badge(state)}</span>
        <span class="k">健康检查 HTTP</span><span>${p.health_http}</span>
        <span class="k">状态接口 HTTP</span><span>${p.cb_http}</span>
        <span class="k">错误信息</span><span>${esc(p.cb?.error || p.health?.error || '无')}</span>
      </div></div>`;
  }
  document.getElementById('cbPanel').innerHTML = html;
  const mode = d.backend_mode?.mode;
  document.getElementById('backendMode').innerHTML =
    '当前模式：<b class="'+(mode==='timeout'?'bad':'ok')+'">'+esc(mode || '未知')+'</b>';
}
async function setMode(mode){
  const r = await fetch('/api/backend/mode', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode})});
  const d = await r.json(); refresh();
  document.getElementById('respMeta').textContent = '后端模式切换 → HTTP ' + d.http_status;
  document.getElementById('respBox').textContent = JSON.stringify(d, null, 2);
}
async function diagnose(){
  const body = {device_id: document.getElementById('deviceId').value || 'DEV-0000',
                proxy: document.getElementById('proxySel').value};
  const t0 = performance.now();
  const r = await fetch('/api/diagnose', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const ms = Math.round(performance.now()-t0);
  const d = await r.json();
  document.getElementById('respMeta').textContent =
    `入口 proxy-${body.proxy} · HTTP ${d.http_status} · 耗时 ${ms} ms · request_id ${d.request_id || '-'}`;
  document.getElementById('respBox').textContent = JSON.stringify(d, null, 2);
  refresh();
}
async function resetCb(){
  const r = await fetch('/api/reset', {method:'POST'}); const d = await r.json();
  document.getElementById('respMeta').textContent = '熔断器已复位';
  document.getElementById('respBox').textContent = JSON.stringify(d, null, 2);
  refresh();
}
refresh(); setInterval(refresh, 1000);
</script>
</body>
</html>
"""
