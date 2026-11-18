"""本地模拟诊断后端。

- 默认 normal：立即返回设备诊断结果。
- 切换为 timeout：请求挂起 delay 秒（模拟后端连续超时）。
- 模式保存在进程内存中，可通过管理接口切换；另支持单请求 delay 参数便于自测。
"""
from __future__ import annotations

import asyncio
import os
import random
import time

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="模拟设备诊断后端")

# timeout 模式下挂起的时长：大于代理 2.5s 的超时阈值
HANG_SECONDS = float(os.environ.get("BACKEND_HANG", "2.0"))

_state: dict[str, str] = {"mode": "normal"}

_FAULTS = ["主板温度正常", "风扇转速正常", "电源模块正常", "内存自检通过", "磁盘 SMART 正常"]


class ModeBody(BaseModel):
    mode: str


@app.get("/health")
def health() -> dict:
    return {"service": "backend", "mode": _state["mode"]}


@app.post("/admin/mode")
async def set_mode(body: ModeBody) -> dict:
    if body.mode not in ("normal", "timeout"):
        return {"ok": False, "error": "mode 只支持 normal / timeout"}
    _state["mode"] = body.mode
    return {"ok": True, "mode": _state["mode"]}


@app.get("/admin/mode")
def get_mode() -> dict:
    return {"mode": _state["mode"]}


@app.get("/diagnose/{device_id}")
async def diagnose(
    device_id: str,
    delay: float = Query(0.0, description="额外挂起秒数，供自测使用"),
) -> dict:
    hang = delay
    if _state["mode"] == "timeout":
        hang += HANG_SECONDS
    if hang > 0:
        await asyncio.sleep(hang)

    if _state["mode"] == "timeout":
        # 模拟超时：返回 504，代理据此计入一次失败
        # （若代理自身的 HTTP 超时更短，则会先在代理侧判定为超时，效果一致）
        return JSONResponse(
            status_code=504,
            content={
                "ok": False,
                "device_id": device_id,
                "error": "SIMULATED_TIMEOUT",
                "message": f"后端处于 timeout 模式，已挂起 {hang:.1f}s",
            },
        )

    rng = random.Random(f"{device_id}-{int(time.time() // 30)}")
    return {
        "ok": True,
        "device_id": device_id,
        "diagnosed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": {
            "cpu_temp_c": round(rng.uniform(42, 61), 1),
            "memory_used_pct": round(rng.uniform(28, 57), 1),
            "disk_used_pct": round(rng.uniform(35, 72), 1),
            "checks": rng.sample(_FAULTS, k=3),
        },
    }
