"""基于 Redis 的全局熔断器客户端（两台代理共享同一份状态）。

设计要点：
- 所有状态判断与转换都在 Redis Lua 脚本中原子完成，避免多代理竞争。
- generation（代数）在每次状态切换时递增：请求带着预检时的代数上报结果，
  代数过期的“迟到结果”会被脚本直接忽略，不能推翻新一轮成功。
- 探测权通过带 TTL 的 key 发放：代理在探测途中退出，租约到期后其它代理
  仍可发起下一轮探测，恢复流程不会卡死。
- Redis 不可用时抛出 RedisUnavailable，上层明确拒绝转发，绝不放行。
"""
from __future__ import annotations

import time
from pathlib import Path

from redis import Redis
from redis.exceptions import RedisError

STATE_KEY = "cb:state"
PROBE_KEY = "cb:state:probe"

# 连续超时阈值与冷却时长：题目固定要求
FAILURE_THRESHOLD = 3
COOLDOWN_MS = 5_000
# 探测权租约：必须大于单次探测的 HTTP 超时，留足余量
PROBE_TTL_MS = 10_000

_HERE = Path(__file__).parent


class RedisUnavailable(RedisError):
    """Redis 不可用（连接失败、超时等），调用方必须拒绝转发。"""


class CircuitBreaker:
    def __init__(self, redis_client: Redis, proxy_id: str) -> None:
        self._r = redis_client
        self._proxy_id = proxy_id
        self._preflight = self._load_script("preflight.lua")
        self._report = self._load_script("report_result.lua")

    def _load_script(self, name: str):
        script = (_HERE / name).read_text(encoding="utf-8")
        # register_script 返回可调用对象，自动使用 EVALSHA 并在 NOSCRIPT 时回退
        return self._r.register_script(script)

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def preflight(self) -> dict:
        """请求前检查。返回 {allowed, state, generation, reason}。"""
        try:
            resp = self._preflight(
                keys=[STATE_KEY],
                args=[
                    self._now_ms(),
                    COOLDOWN_MS,
                    FAILURE_THRESHOLD,
                    self._proxy_id,
                    PROBE_TTL_MS,
                ],
            )
        except RedisError as exc:
            raise RedisUnavailable(str(exc)) from exc
        allowed, state, generation, reason = resp
        return {
            "allowed": bool(allowed),
            "state": state,
            "generation": int(generation),
            "reason": reason,
        }

    def report(self, generation: int, success: bool) -> dict:
        """上报请求结果。返回 {applied, state, generation, failures}。"""
        try:
            resp = self._report(
                keys=[STATE_KEY],
                args=[
                    self._now_ms(),
                    FAILURE_THRESHOLD,
                    COOLDOWN_MS,
                    self._proxy_id,
                    generation,
                    1 if success else 0,
                    PROBE_TTL_MS,
                ],
            )
        except RedisError as exc:
            raise RedisUnavailable(str(exc)) from exc
        applied, state, gen, failures = resp
        return {
            "applied": bool(applied),
            "state": state,
            "generation": int(gen),
            "failures": int(failures),
        }

    def status(self) -> dict:
        """读取当前熔断状态（只读，供状态展示页使用）。"""
        try:
            exists = self._r.exists(STATE_KEY)
            if not exists:
                return {
                    "state": "CLOSED",
                    "generation": 0,
                    "failures": 0,
                    "cooldown_remaining_ms": 0,
                    "probe_in_progress": False,
                    "probe_owner": None,
                }
            data = self._r.hgetall(STATE_KEY)
            probe_owner = self._r.get(PROBE_KEY)
            now = self._now_ms()
            state = data.get("state", "CLOSED")
            remaining = 0
            if state == "OPEN":
                remaining = max(
                    0,
                    COOLDOWN_MS - (now - int(data.get("opened_at", now))),
                )
            return {
                "state": state,
                "generation": int(data.get("generation", 0)),
                "failures": int(data.get("failures", 0)),
                "cooldown_remaining_ms": remaining,
                "probe_in_progress": probe_owner is not None,
                "probe_owner": probe_owner,
            }
        except RedisError as exc:
            raise RedisUnavailable(str(exc)) from exc

    def reset(self) -> None:
        """管理操作：清空熔断状态（仅用于演示/测试复位）。"""
        try:
            self._r.delete(STATE_KEY, PROBE_KEY)
        except RedisError as exc:
            raise RedisUnavailable(str(exc)) from exc

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except RedisError:
            return False
