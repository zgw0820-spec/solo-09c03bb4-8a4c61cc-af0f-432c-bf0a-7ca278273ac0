-- 预检脚本：决定当前请求是否允许通过熔断器
--
-- 全局熔断器状态存放在 KEYS[1] 这个 hash 中，字段：
--   state       CLOSED / OPEN / HALF_OPEN
--   generation  状态机代数，每次状态切换 +1；用于让上一代“迟到的请求”结果失效
--   failures    CLOSED 下的连续超时计数（两台代理合计）
--   opened_at   进入 OPEN 的毫秒时间戳
--   probe_owner 半开期内持有唯一次探测权的代理标识
--
-- 入参：
--   ARGV[1] now_ms      代理当前毫秒时间戳
--   ARGV[2] cooldown_ms 冷却时长（毫秒）
--   ARGV[3] threshold   熔断阈值（连续超时次数）
--   ARGV[4] proxy_id    发起请求的代理标识
--   ARGV[5] probe_ttl_ms 探测权租约 TTL（毫秒），代理退出时自动释放
--
-- 返回：
--   { allow, state, generation, reason }
--   allow=1 放行；allow=0 快速拒绝
--   reason: OK / COOLDOWN / PROBE_IN_PROGRESS / STALE_OPEN
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])
local proxy_id = ARGV[4]
local probe_ttl = ARGV[5]

if redis.call('EXISTS', key) == 0 then
    -- 首次访问：初始化为 CLOSED
    redis.call('HSET', key, 'state', 'CLOSED', 'generation', 0,
                       'failures', 0, 'opened_at', 0, 'probe_owner', '')
end

local state = redis.call('HGET', key, 'state')
local generation = tonumber(redis.call('HGET', key, 'generation'))

if state == 'OPEN' then
    local opened_at = tonumber(redis.call('HGET', key, 'opened_at'))
    if now - opened_at < cooldown then
        -- 冷却中：快速拒绝
        return { 0, 'OPEN', generation, 'COOLDOWN' }
    end
    -- 冷却结束 -> 进入 HALF_OPEN，全局只放出一个探测
    generation = generation + 1
    redis.call('HSET', key, 'state', 'HALF_OPEN',
                       'generation', generation,
                       'probe_owner', proxy_id)
    redis.call('PEXPIRE', key, tonumber(probe_ttl) + 3600000)
    -- 探测权租约：代理在探测途中退出时，租约到期自动恢复
    redis.call('SET', key .. ':probe', proxy_id, 'PX', probe_ttl)
    return { 1, 'HALF_OPEN', generation, 'OK' }
end

if state == 'HALF_OPEN' then
    local owner = redis.call('GET', key .. ':probe')
    if owner == proxy_id then
        -- 同一个代理重入（一般不会发生）：仍只认这一个探测
        return { 1, 'HALF_OPEN', generation, 'OK' }
    end
    if owner then
        -- 已有一个探测在进行：其它请求一律快速拒绝
        return { 0, 'HALF_OPEN', generation, 'PROBE_IN_PROGRESS' }
    end
    -- 探测租约过期（代理退出/探测超时后状态未及时推进）：
    -- 重新发放唯一次探测，保证恢复流程仍可继续
    redis.call('SET', key .. ':probe', proxy_id, 'PX', probe_ttl)
    return { 1, 'HALF_OPEN', generation, 'OK' }
end

-- CLOSED：正常转发
return { 1, 'CLOSED', generation, 'OK' }
