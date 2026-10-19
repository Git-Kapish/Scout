"""Lua Scripts for Atomic Fencing and Lease Management (PRD FR-4.1, FR-4.4).

Ensures that:
1. Advance Commit: atomically advances the commit pointer only if worker's fencing
   generation matches run:{id}:generation and sequence number strictly increases.
2. Fenced Writes: all read-model updates (meta, budget, findings, report) check fencing
   generation atomically before writing.
3. Acquire / Renew Lease: leases cannot be hijacked without bumping generation, and
   stale workers are evicted cleanly.
"""

from __future__ import annotations

import json
from typing import Any, Tuple

import redis

# 1. Advance Commit Pointer
# KEYS[1] = run:{id}:generation
# KEYS[2] = run:{id}:committed
# ARGV[1] = worker_generation (int)
# ARGV[2] = new_seq (int)
# ARGV[3] = committed_json_payload (str)
ADVANCE_COMMIT_LUA = """
local current_gen = tonumber(redis.call('GET', KEYS[1]) or '0')
local worker_gen = tonumber(ARGV[1])

if worker_gen ~= current_gen then
    return {0, 'stale_generation', tostring(current_gen)}
end

local current_committed = redis.call('GET', KEYS[2])
if current_committed then
    local ok, parsed = pcall(cjson.decode, current_committed)
    if ok and parsed and parsed.seq then
        local current_seq = tonumber(parsed.seq)
        local new_seq = tonumber(ARGV[2])
        if new_seq <= current_seq then
            return {0, 'stale_seq', tostring(current_seq)}
        end
    end
end

redis.call('SET', KEYS[2], ARGV[3])
return {1, 'ok', tostring(worker_gen)}
"""

# 2. Fenced Hash Set (for run meta, budget, findings)
# KEYS[1] = run:{id}:generation
# KEYS[2] = target_hash_key
# ARGV[1] = worker_generation (int)
# ARGV[2..N] = field, value, field, value...
FENCED_HSET_LUA = """
local current_gen = tonumber(redis.call('GET', KEYS[1]) or '0')
local worker_gen = tonumber(ARGV[1])

if worker_gen ~= current_gen then
    return {0, 'stale_generation', tostring(current_gen)}
end

for i = 2, #ARGV, 2 do
    redis.call('HSET', KEYS[2], ARGV[i], ARGV[i+1])
end

return {1, 'ok'}
"""

# 3. Fenced String Set (for run report, status)
# KEYS[1] = run:{id}:generation
# KEYS[2] = target_string_key
# ARGV[1] = worker_generation (int)
# ARGV[2] = value (str)
FENCED_SET_LUA = """
local current_gen = tonumber(redis.call('GET', KEYS[1]) or '0')
local worker_gen = tonumber(ARGV[1])

if worker_gen ~= current_gen then
    return {0, 'stale_generation', tostring(current_gen)}
end

redis.call('SET', KEYS[2], ARGV[2])
return {1, 'ok'}
"""

# 4. Fenced Stream Add (for events and trace)
# KEYS[1] = run:{id}:generation
# KEYS[2] = target_stream_key
# ARGV[1] = worker_generation (int)
# ARGV[2] = maxlen (int, 0 for unlimited)
# ARGV[3..N] = field, value, field, value...
FENCED_XADD_LUA = """
local current_gen = tonumber(redis.call('GET', KEYS[1]) or '0')
local worker_gen = tonumber(ARGV[1])

if worker_gen ~= current_gen then
    return {0, 'stale_generation', tostring(current_gen)}
end

local maxlen = tonumber(ARGV[2])
local fields = {}
for i = 3, #ARGV do
    table.insert(fields, ARGV[i])
end

if maxlen > 0 then
    redis.call('XADD', KEYS[2], 'MAXLEN', '~', maxlen, '*', unpack(fields))
else
    redis.call('XADD', KEYS[2], '*', unpack(fields))
end

return {1, 'ok'}
"""

# 5. Acquire Lease with Generation Bump
# KEYS[1] = lease:{run_id}
# KEYS[2] = run:{id}:generation
# KEYS[3] = runs:active
# ARGV[1] = worker_id (str)
# ARGV[2] = ttl_seconds (int)
# ARGV[3] = run_id (str)
# ARGV[4] = force ("1" or "0")
ACQUIRE_LEASE_LUA = """
local current_lease = redis.call('GET', KEYS[1])
local force = ARGV[4] == '1'
local worker_id = ARGV[1]

if current_lease and not force then
    local ttl = redis.call('TTL', KEYS[1])
    if ttl > 0 then
        local prefix = worker_id .. ':'
        if string.sub(current_lease, 1, string.len(prefix)) == prefix then
            redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
            local current_gen = redis.call('GET', KEYS[2])
            return {1, 'ok', tostring(current_gen)}
        end
        return {0, 'lease_active', current_lease, tostring(ttl)}
    end
end

local new_gen = redis.call('INCR', KEYS[2])
local lease_val = worker_id .. ':' .. tostring(new_gen)
redis.call('SET', KEYS[1], lease_val, 'EX', tonumber(ARGV[2]))
redis.call('SADD', KEYS[3], ARGV[3])

return {1, 'ok', tostring(new_gen)}
"""

# 6. Renew Lease (Heartbeat)
# KEYS[1] = lease:{run_id}
# KEYS[2] = run:{id}:generation
# ARGV[1] = worker_id (str)
# ARGV[2] = generation (int)
# ARGV[3] = ttl_seconds (int)
RENEW_LEASE_LUA = """
local current_gen = tonumber(redis.call('GET', KEYS[2]) or '0')
local worker_gen = tonumber(ARGV[2])

if worker_gen ~= current_gen then
    return {0, 'stale_generation', tostring(current_gen)}
end

local current_lease = redis.call('GET', KEYS[1])
local expected_lease = ARGV[1] .. ':' .. tostring(worker_gen)

if current_lease ~= expected_lease then
    return {0, 'lease_mismatch', tostring(current_lease)}
end

redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return {1, 'ok'}
"""

# 7. Release Lease
# KEYS[1] = lease:{run_id}
# KEYS[2] = run:{id}:generation
# KEYS[3] = runs:active
# ARGV[1] = worker_id (str)
# ARGV[2] = generation (int)
# ARGV[3] = run_id (str)
# ARGV[4] = is_terminal ("1" or "0")
RELEASE_LEASE_LUA = """
local current_gen = tonumber(redis.call('GET', KEYS[2]) or '0')
local worker_gen = tonumber(ARGV[2])

if worker_gen ~= current_gen then
    return {0, 'stale_generation', tostring(current_gen)}
end

local current_lease = redis.call('GET', KEYS[1])
local expected_lease = ARGV[1] .. ':' .. tostring(worker_gen)

if current_lease == expected_lease then
    redis.call('DEL', KEYS[1])
end

if ARGV[4] == '1' then
    redis.call('SREM', KEYS[3], ARGV[3])
end

return {1, 'ok'}
"""


class ScriptRegistry:
    """Manages loaded SHA1 hashes for Redis Lua scripts to execute via EVALSHA."""

    def __init__(self, r: redis.Redis):
        self.r = r
        self.sha_advance_commit = self.r.script_load(ADVANCE_COMMIT_LUA)
        self.sha_fenced_hset = self.r.script_load(FENCED_HSET_LUA)
        self.sha_fenced_set = self.r.script_load(FENCED_SET_LUA)
        self.sha_fenced_xadd = self.r.script_load(FENCED_XADD_LUA)
        self.sha_acquire_lease = self.r.script_load(ACQUIRE_LEASE_LUA)
        self.sha_renew_lease = self.r.script_load(RENEW_LEASE_LUA)
        self.sha_release_lease = self.r.script_load(RELEASE_LEASE_LUA)

    def advance_commit(
        self,
        gen_key: str,
        commit_key: str,
        generation: int,
        seq: int,
        committed_payload: dict[str, Any],
    ) -> Tuple[bool, str]:
        payload_str = json.dumps(committed_payload)
        res = self.r.evalsha(
            self.sha_advance_commit,
            2,
            gen_key,
            commit_key,
            generation,
            seq,
            payload_str,
        )
        # res format: [status_code (1 or 0), reason]
        success = bool(res[0])
        reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
        return success, reason

    def fenced_hset(
        self,
        gen_key: str,
        target_hash: str,
        generation: int,
        mapping: dict[str, str],
    ) -> Tuple[bool, str]:
        args: list[Any] = [generation]
        for k, v in mapping.items():
            args.extend([k, v])
        res = self.r.evalsha(self.sha_fenced_hset, 2, gen_key, target_hash, *args)
        success = bool(res[0])
        reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
        return success, reason

    def fenced_set(
        self,
        gen_key: str,
        target_key: str,
        generation: int,
        value: str,
    ) -> Tuple[bool, str]:
        res = self.r.evalsha(self.sha_fenced_set, 2, gen_key, target_key, generation, value)
        success = bool(res[0])
        reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
        return success, reason

    def fenced_xadd(
        self,
        gen_key: str,
        target_stream: str,
        generation: int,
        fields: dict[str, str],
        maxlen: int = 1000,
    ) -> Tuple[bool, str]:
        args: list[Any] = [generation, maxlen]
        for k, v in fields.items():
            args.extend([k, v])
        res = self.r.evalsha(self.sha_fenced_xadd, 2, gen_key, target_stream, *args)
        success = bool(res[0])
        reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
        return success, reason

    def acquire_lease(
        self,
        lease_key: str,
        gen_key: str,
        active_runs_key: str,
        worker_id: str,
        ttl_seconds: int,
        run_id: str,
        force: bool = False,
    ) -> Tuple[bool, str, int]:
        res = self.r.evalsha(
            self.sha_acquire_lease,
            3,
            lease_key,
            gen_key,
            active_runs_key,
            worker_id,
            ttl_seconds,
            run_id,
            "1" if force else "0",
        )
        success = bool(res[0])
        reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
        new_gen = int(res[2]) if success and len(res) > 2 else 0
        return success, reason, new_gen

    def renew_lease(
        self,
        lease_key: str,
        gen_key: str,
        worker_id: str,
        generation: int,
        ttl_seconds: int,
    ) -> Tuple[bool, str]:
        res = self.r.evalsha(
            self.sha_renew_lease,
            2,
            lease_key,
            gen_key,
            worker_id,
            generation,
            ttl_seconds,
        )
        success = bool(res[0])
        reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
        return success, reason

    def release_lease(
        self,
        lease_key: str,
        gen_key: str,
        active_runs_key: str,
        worker_id: str,
        generation: int,
        run_id: str,
        is_terminal: bool = False,
    ) -> Tuple[bool, str]:
        res = self.r.evalsha(
            self.sha_release_lease,
            3,
            lease_key,
            gen_key,
            active_runs_key,
            worker_id,
            generation,
            run_id,
            "1" if is_terminal else "0",
        )
        success = bool(res[0])
        reason = res[1].decode("utf-8") if isinstance(res[1], bytes) else str(res[1])
        return success, reason
