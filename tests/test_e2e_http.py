"""端到端 HTTP 自测：真实启动 backend + 两个 proxy 进程，打真实 HTTP 请求。

运行方式（仓库根目录）：
    venv/bin/python tests/test_e2e_http.py
需要一个本地 Redis（REDIS_URL，默认 redis://127.0.0.1:6379/0）。
脚本会自行拉起 3 个 uvicorn 子进程并在结束后清理。
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
BACKEND_PORT = 18000
PROXY_A_PORT = 18001
PROXY_B_PORT = 18002
PROCS: list[subprocess.Popen] = []


def _free_port_wait(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"端口 {port} 服务未在 {timeout}s 内就绪")


def _spawn(app_dir: str, port: int, env: dict) -> None:
    log = open(ROOT / "tests" / f"e2e_{env['PROXY_ID'] if 'PROXY_ID' in env else app_dir}.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT / app_dir),
        env={**os.environ, **env},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    PROCS.append(proc)
    _free_port_wait(port)


@pytest.fixture(scope="module", autouse=True)
def stack():
    # 清掉历史熔断状态
    import redis
    redis.Redis.from_url(REDIS_URL).delete("cb:state", "cb:state:probe")

    _spawn("backend", BACKEND_PORT, {"PROXY_ID": "backend", "BACKEND_HANG": "2"})
    _spawn("proxy", PROXY_A_PORT, {
        "PROXY_ID": "proxy-a", "REDIS_URL": REDIS_URL,
        "BACKEND_URL": f"http://127.0.0.1:{BACKEND_PORT}", "BACKEND_TIMEOUT": "2.5"})
    _spawn("proxy", PROXY_B_PORT, {
        "PROXY_ID": "proxy-b", "REDIS_URL": REDIS_URL,
        "BACKEND_URL": f"http://127.0.0.1:{BACKEND_PORT}", "BACKEND_TIMEOUT": "2.5"})
    yield
    for proc in PROCS:
        proc.send_signal(signal.SIGTERM)
    for proc in PROCS:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def client():
    with httpx.Client(timeout=8.0) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_breaker():
    with httpx.Client(timeout=5) as c:
        c.post(f"http://127.0.0.1:{PROXY_A_PORT}/cb/reset")
        c.post(f"http://127.0.0.1:{PROXY_B_PORT}/cb/reset")
        c.post(f"http://127.0.0.1:{BACKEND_PORT}/admin/mode", json={"mode": "normal"})
    yield


def _diag(client, port, dev="DEV-1"):
    return client.get(f"http://127.0.0.1:{port}/diagnose/{dev}")


def test_normal_diagnosis_both_proxies(client):
    for port in (PROXY_A_PORT, PROXY_B_PORT):
        r = _diag(client, port, "DEV-100")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["backend"]["device_id"] == "DEV-100"
        assert body["cb"]["state"] == "CLOSED"


def test_three_timeouts_across_proxies_trip_breaker(client):
    client.post(f"http://127.0.0.1:{BACKEND_PORT}/admin/mode", json={"mode": "timeout"})
    # a、b、a 各一次超时，合计三次
    r1 = _diag(client, PROXY_A_PORT)
    assert r1.status_code == 504 and r1.json()["ok"] is False
    assert r1.json()["cb"]["failures"] == 1
    r2 = _diag(client, PROXY_B_PORT)
    assert r2.json()["cb"]["failures"] == 2
    r3 = _diag(client, PROXY_A_PORT)
    assert r3.json()["cb"]["state"] == "OPEN"

    # 熔断后：立即快速拒绝，且没有真正打后端（响应毫秒级返回）
    t0 = time.time()
    r4 = _diag(client, PROXY_B_PORT)
    elapsed_ms = (time.time() - t0) * 1000
    assert r4.status_code == 503
    assert r4.json()["reason"] == "COOLDOWN"
    assert elapsed_ms < 300, f"快速拒绝不应等待后端，实际耗时 {elapsed_ms:.0f}ms"


def test_half_open_single_probe_and_recovery(client):
    client.post(f"http://127.0.0.1:{BACKEND_PORT}/admin/mode", json={"mode": "timeout"})
    for port in (PROXY_A_PORT, PROXY_B_PORT, PROXY_A_PORT):
        _diag(client, port)
    assert _diag(client, PROXY_B_PORT).json()["reason"] == "COOLDOWN"

    # 冷却 5 秒
    time.sleep(5.2)
    # 半开期第一个请求成为唯一探测；后端仍是 timeout -> 探测失败重新冷却
    probe = _diag(client, PROXY_A_PORT)
    assert probe.json()["is_probe"] is True
    assert probe.json()["cb"]["state"] == "OPEN"
    # 同代其它请求被拒
    other = _diag(client, PROXY_B_PORT)
    assert other.json()["reason"] == "COOLDOWN"

    # 再冷却一轮，这次后端恢复，探测成功
    time.sleep(5.2)
    client.post(f"http://127.0.0.1:{BACKEND_PORT}/admin/mode", json={"mode": "normal"})
    probe2 = _diag(client, PROXY_B_PORT)
    body = probe2.json()
    assert body["is_probe"] is True
    assert body["ok"] is True
    assert body["cb"]["state"] == "CLOSED"
    # 完全恢复
    assert _diag(client, PROXY_A_PORT, "DEV-9").status_code == 200


def test_only_one_probe_allowed_in_half_open(client):
    client.post(f"http://127.0.0.1:{BACKEND_PORT}/admin/mode", json={"mode": "timeout"})
    for port in (PROXY_A_PORT, PROXY_B_PORT, PROXY_A_PORT):
        _diag(client, port)
    time.sleep(5.2)
    # 用慢后端保证探测在途：backend timeout 模式挂 2s，先抢探测再立刻打第二个代理
    t0 = time.time()
    probe_resp = []
    import threading
    def fire(port, out):
        out.append(_diag(client, port))
    t = threading.Thread(target=fire, args=(PROXY_A_PORT, probe_resp))
    t.start()
    time.sleep(0.4)  # 等预检通过、探测在途
    r = _diag(client, PROXY_B_PORT)
    t.join()
    assert r.status_code == 503
    assert r.json()["reason"] == "PROBE_IN_PROGRESS"
    assert probe_resp[0].json()["is_probe"] is True
    assert time.time() - t0 < 3


def test_redis_down_explicitly_rejects_forwarding(client):
    """用 CLIENT PAUSE 模拟 Redis 不可达：代理必须明确 503 拒绝，不能放行。"""
    import redis
    rdb = redis.Redis.from_url(REDIS_URL)
    try:
        rdb.client_pause(4000)
        r = _diag(client, PROXY_A_PORT, "DEV-7")
        assert r.status_code == 503
        body = r.json()
        assert body["reason"] == "REDIS_DOWN"
        assert body["rejected"] is True
        s = client.get(f"http://127.0.0.1:{PROXY_B_PORT}/cb/status")
        assert s.status_code == 503 and s.json()["redis_ok"] is False
    finally:
        rdb.client_unpause()
    # 恢复后代理自愈
    assert _diag(client, PROXY_A_PORT, "DEV-7").status_code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
