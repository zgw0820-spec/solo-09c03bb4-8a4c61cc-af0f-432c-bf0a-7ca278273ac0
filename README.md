# 设备诊断双代理熔断器（FastAPI + Redis）

机房场景演练：设备诊断后端连续超时，两台代理**一起停止转发**；后端恢复后，
经半开探测确认才重新放行。所有熔断状态放在 Redis 中由两个代理共享，
状态判断与切换全部通过 Lua 脚本原子完成。

## 目录结构

```
.
├── docker-compose.yml      # 5 个服务：redis / backend / proxy-a / proxy-b / web
├── Dockerfile              # 通用 FastAPI 镜像（构建参数 APP_DIR 区分服务）
├── redis/redis.conf        # AOF 持久化（appendfsync always），重启不丢熔断状态
├── proxy/                  # 诊断代理（同一份代码，两个实例 PROXY_ID 不同）
│   └── app/
│       ├── breaker.py            # Redis 熔断客户端
│       ├── preflight.lua         # 预检状态机（放行 / 冷却拒绝 / 半开唯一次探测）
│       ├── report_result.lua     # 结果上报状态机（含代数校验，迟到结果失效）
│       └── main.py               # /diagnose/{设备号}、/cb/status、/cb/reset、/health
├── backend/app/main.py     # 模拟诊断后端：normal 立即返回 / timeout 挂起并返回 504
├── web/app/main.py         # 操作网页 + 聚合 API（/api/overview、/api/diagnose ...）
└── tests/                  # Lua 单测 + 端到端 HTTP 测试 + 一键脚本
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开操作网页
open http://localhost:8080        # macOS；Linux 用浏览器访问 http://localhost:8080
```

端口映射：

| 服务      | 地址                  | 说明                     |
|-----------|-----------------------|--------------------------|
| web       | http://localhost:8080 | 运维操作网页             |
| proxy-a   | http://localhost:8081 | 代理 A（可直接 curl）    |
| proxy-b   | http://localhost:8082 | 代理 B                   |
| backend   | 仅 compose 内网       | 模拟诊断后端             |
| redis     | 仅 compose 内网       | 共享熔断状态，AOF 持久化 |

网页功能：输入设备号、选择任一代理发起**真实诊断**；一键切换后端
`正常 / 超时`；实时（1s 刷新）查看全局熔断状态、连续超时计数、冷却剩余、
半开探测归属、两代理各自的 Redis 连通性；提供熔断复位按钮。

## 熔断语义（与需求逐条对应）

1. **合计连续 3 次超时即熔断**：失败计数在 Redis 中共享，两个代理的超时
   合并计数（`failures` 达到 3 → `OPEN`）；任一成功会清零计数。
2. **熔断后快速拒绝**：`OPEN` 冷却期内请求直接返回 `503 reason=COOLDOWN`，
   不与后端建立连接（实测毫秒级返回）。
3. **冷却 5 秒后半开只放一个探测**：冷却到期后第一个请求成为唯一探测
   （`HALF_OPEN`），探测权写入带 10s TTL 的 key；其它请求返回
   `503 reason=PROBE_IN_PROGRESS`。
4. **探测成功才恢复**：成功 → `CLOSED`、计数清零；失败 → 重新 `OPEN`
   并重新计时 5 秒冷却。
5. **探测超时 / 代理退出后仍能恢复**：探测权是带 TTL 的租约，代理在探测
   途中崩溃不会留下永久锁，租约到期后下一个请求可领取新探测。
6. **迟到结果不能推翻新一轮**：每次状态切换 `generation + 1`，请求预检时
   记录代数，上报时 Lua 校验代数不一致则**直接忽略**（`applied=false`），
   旧请求迟到的成功/失败都无法影响当前状态。
7. **代理 / Redis 重启不提前放行**：代理无本地状态，重启后从 Redis 读取
   同一状态继续冷却；Redis 开启 AOF（`appendfsync always`），重启后
   `OPEN/opened_at/generation` 原样恢复。
8. **Redis 不可用明确拒绝**：代理对 Redis 设置 1s 连接/读写超时，任何
   Redis 异常都返回 `503 reason=REDIS_DOWN`，绝不绕过熔断直接转发；
   Redis 恢复后代理自动自愈。

### HTTP 接口速览

```bash
# 代理
GET  /diagnose/{device_id}   # 诊断（先过熔断再转发）
GET  /cb/status              # 熔断状态视图
POST /cb/reset               # 管理复位
GET  /health                 # 含 redis_ok 连通性

# 模拟后端
GET  /admin/mode             # 当前 normal / timeout
POST /admin/mode             # 切换模式 {"mode":"timeout"}
GET  /diagnose/{device_id}?delay=3   # delay 仅供自测制造慢响应

# 网页后端
GET  /api/overview           # 双代理 + 后端模式 + 共享熔断状态聚合
POST /api/diagnose           # {"device_id":"DEV-1","proxy":"a"|"b"}
POST /api/backend/mode       # {"mode":"normal"|"timeout"}
POST /api/reset              # 复位熔断器
```

## 自测

### Docker Compose 环境

```bash
docker compose run --rm -v "$PWD:/work" -w /work proxy sh -c \
  "pip install pytest && pytest tests/test_breaker_lua.py tests/test_e2e_http.py -v"
```
（容器内 `REDIS_URL` 指向 compose 的 redis 服务；端到端测试会自行拉起
backend/proxy 子进程，端口 18000-18002 需空闲。）

手动核对重启语义：

```bash
docker compose restart proxy-a      # 冷却期重启代理：仍 COOLDOWN，不提前放行
docker compose restart redis        # AOF 恢复：OPEN 状态与冷却计时原样保留
docker compose stop redis           # 期间诊断返回 503 REDIS_DOWN，恢复后自愈
```

### 本地直跑（本仓库当前开发环境用的方式）

需要一个本机 Redis 与 Python 虚拟环境：

```bash
tests/self_test.sh
# 或分步：
venv/bin/python -m pytest tests/test_breaker_lua.py -v   # 7 项状态机单测
venv/bin/python -m pytest tests/test_e2e_http.py -v      # 5 项端到端测试
```

## 关键参数

| 参数                | 位置              | 默认值 | 说明                         |
|---------------------|-------------------|--------|------------------------------|
| 失败阈值            | `proxy/app/breaker.py` | 3      | 两代理合计连续超时次数       |
| 冷却时长            | 同上              | 5000ms | OPEN → HALF_OPEN 的等待时间  |
| 探测租约 TTL        | 同上              | 10000ms | 代理退出后探测权自动释放    |
| 代理后端超时        | compose 环境变量  | 2.5s   | 后端 timeout 模式挂 2.0s     |
| Redis 操作超时      | `proxy/app/main.py` | 1s   | Redis 故障时快速明确拒绝     |
