"""诊断代理服务（部署两份实例 proxy-a / proxy-b，共享 Redis 熔断状态）。"""
from __future__ import annotations

import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis import Redis

from .breaker import CircuitBreaker, RedisUnavailable

PROXY_ID = os.environ.get("PROXY_ID", "proxy-unknown")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
# 模拟后端“超时”模式会挂 2 秒，代理 2.5 秒判定超时
BACKEND_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT", "2.5"))

app = FastAPI(title=f"诊断代理 {PROXY_ID}")

# socket_connect/socket_timeout 均为 1s：Redis 故障时快速失败、明确拒绝
redis_client = Redis.from_url(
    REDIS_URL,
    socket_connect_timeout=1.0,
    socket_timeout=1.0,
    decode_responses=True,
)
breaker = CircuitBreaker(redis_client, PROXY_ID)


def _cb_headers(info: dict) -> dict[str, str]:
    return {
        "X-CB-State": str(info.get("state", "")),
        "X-CB-Generation": str(info.get("generation", "")),
        "X-CB-Failures": str(info.get("failures", "")),
        "X-CB-Proxy": PROXY_ID,
    }


@app.get("/health")
def health() -> dict:
    ok = breaker.ping()
    return {"proxy": PROXY_ID, "redis_ok": ok}


@app.get("/cb/status")
def cb_status() -> JSONResponse:
    try:
        info = breaker.status()
    except RedisUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "proxy": PROXY_ID,
                "redis_ok": False,
                "error": f"Redis 不可用：{exc}",
            },
        )
    info["proxy"] = PROXY_ID
    info["redis_ok"] = True
    return JSONResponse(info)


@app.post("/cb/reset")
def cb_reset() -> JSONResponse:
    try:
        breaker.reset()
        return JSONResponse({"ok": True, "proxy": PROXY_ID})
    except RedisUnavailable as exc:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})


@app.get("/diagnose/{device_id}")
async def diagnose(device_id: str, request: Request) -> JSONResponse:
    """诊断入口：先过熔断器，再转发给模拟后端。"""
    request_id = uuid.uuid4().hex[:8]

    # 1) 熔断预检。Redis 不可用 -> 明确拒绝转发
    try:
        decision = breaker.preflight()
    except RedisUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "rejected": True,
                "reason": "REDIS_DOWN",
                "message": f"Redis 不可用，按安全策略拒绝转发：{exc}",
                "proxy": PROXY_ID,
                "request_id": request_id,
            },
            headers={"X-CB-Proxy": PROXY_ID, "X-CB-State": "UNKNOWN"},
        )

    if not decision["allowed"]:
        # 2) 熔断打开 / 半开期已有探测：快速拒绝，不碰后端
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "rejected": True,
                "reason": decision["reason"],
                "message": _reject_message(decision),
                "proxy": PROXY_ID,
                "request_id": request_id,
                "cb_state": decision["state"],
                "generation": decision["generation"],
            },
            headers=_cb_headers(decision),
        )

    # 3) 放行：转发到后端（探测或正常转发走同一条路径）
    is_probe = decision["state"] == "HALF_OPEN"
    success = False
    backend_payload: dict
    http_status = 200
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
            resp = await client.get(f"{BACKEND_URL}/diagnose/{device_id}")
            http_status = resp.status_code
            backend_payload = resp.json()
            success = resp.is_success
    except httpx.TimeoutException:
        backend_payload = {
            "ok": False,
            "error": "BACKEND_TIMEOUT",
            "message": f"后端在 {BACKEND_TIMEOUT}s 内未响应，判定为超时",
        }
        http_status = 504
    except httpx.HTTPError as exc:
        backend_payload = {
            "ok": False,
            "error": "BACKEND_UNREACHABLE",
            "message": f"后端连接失败：{exc}",
        }
        http_status = 502

    # 4) 结果上报。若 Redis 在请求期间挂掉，同样明确拒绝“假装成功”
    report_error = None
    report = None
    try:
        report = breaker.report(decision["generation"], success)
    except RedisUnavailable as exc:
        report_error = str(exc)

    body = {
        "ok": success,
        "proxy": PROXY_ID,
        "request_id": request_id,
        "is_probe": is_probe,
        "backend_status": http_status,
        "device_id": device_id,
        "backend": backend_payload,
    }
    if report is not None:
        body["cb"] = report
        headers = _cb_headers(report)
    else:
        body["cb"] = {"state": "UNKNOWN", "error": report_error}
        headers = {"X-CB-Proxy": PROXY_ID, "X-CB-State": "UNKNOWN"}

    # 转发失败用 5xx 体现，但熔断拒绝语义在 body.rejected 中区分
    return JSONResponse(status_code=http_status if not success else 200,
                        content=body, headers=headers)


def _reject_message(decision: dict) -> str:
    if decision["reason"] == "COOLDOWN":
        return "熔断打开，冷却中：请求被快速拒绝，未转发后端"
    if decision["reason"] == "PROBE_IN_PROGRESS":
        return "半开期全局只允许一个探测，已有探测在进行，本请求快速拒绝"
    return "熔断器拒绝本次请求"
