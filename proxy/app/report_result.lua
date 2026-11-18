-- 结果上报脚本：请求结束后把成功/失败结果交回熔断器
--
-- 入参：
--   ARGV[1] now_ms
--   ARGV[2] threshold       熔断阈值
--   ARGV[3] cooldown_ms
--   ARGV[4] proxy_id
--   ARGV[5] req_generation  请求预检时拿到的代数
--   ARGV[6] success         '1' 成功 / '0' 失败
--   ARGV[7] probe_ttl_ms    重新冷却时清理探测租约用
--
-- 返回：
--   { applied, state, generation, failures }
--   applied=0 表示这是上一代“迟到的结果”，已被忽略（不能推翻新一轮状态）
local key = KEYS[1]
local now = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local cooldown = tonumber(ARGV[3])
local proxy_id = ARGV[4]
local req_generation = tonumber(ARGV[5])
local is_success = (ARGV[6] == '1')

if redis.call('EXISTS', key) == 0 then
    -- 状态键被清空/重建过：旧结果直接忽略
    return { 0, 'CLOSED', 0, 0 }
end

local state = redis.call('HGET', key, 'state')
local generation = tonumber(redis.call('HGET', key, 'generation'))

-- 代数不一致：请求发起后状态机已经进入新一轮，迟到结果一律忽略
if generation ~= req_generation then
    return { 0, state, generation, tonumber(redis.call('HGET', key, 'failures')) or 0 }
end

local new_generation = generation

if is_success then
    if state == 'HALF_OPEN' then
        -- 探测成功：恢复正常转发
        new_generation = generation + 1
        redis.call('HSET', key, 'state', 'CLOSED',
                           'generation', new_generation,
                           'failures', 0,
                           'opened_at', 0,
                           'probe_owner', '')
        redis.call('DEL', key .. ':probe')
    elseif state == 'CLOSED' then
        -- 正常流量成功：连续失败计数清零
        redis.call('HSET', key, 'failures', 0)
    end
    -- OPEN 状态下理论上不会有在途请求，忽略即可
else
    if state == 'CLOSED' then
        local failures = tonumber(redis.call('HGET', key, 'failures')) + 1
        if failures >= threshold then
            -- 两台代理合计连续失败达到阈值：熔断
            new_generation = generation + 1
            redis.call('HSET', key, 'state', 'OPEN',
                               'generation', new_generation,
                               'failures', failures,
                               'opened_at', now,
                               'probe_owner', '')
            redis.call('DEL', key .. ':probe')
        else
            redis.call('HSET', key, 'failures', failures)
        end
    elseif state == 'HALF_OPEN' then
        -- 探测失败：重新进入冷却，代数+1使该探测及同代迟到结果全部失效
        new_generation = generation + 1
        redis.call('HSET', key, 'state', 'OPEN',
                           'generation', new_generation,
                           'opened_at', now,
                           'probe_owner', '')
        redis.call('DEL', key .. ':probe')
    end
end

return { 1,
         redis.call('HGET', key, 'state'),
         new_generation,
         tonumber(redis.call('HGET', key, 'failures')) or 0 }
