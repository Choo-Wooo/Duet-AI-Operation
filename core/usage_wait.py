"""Retry only explicit provider usage-limit failures, without blocking the UI."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import math
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def reset_time(value):
    try:
        stamp = float(value)
        if stamp > 100000000000:
            stamp /= 1000
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo else None
        except ValueError:
            return None


def classify(result, payload=None, now=None):
    """Read error metadata only; successful model/tool text is never evidence."""
    now = time.time() if now is None else now
    if result.interrupted or result.context_overflow or result.ok:
        return result
    data = payload if isinstance(payload, dict) else {}
    text = str(result.error or "")
    code = str(data.get("codexErrorInfo") or data.get("code") or data.get("type") or "")
    probe = (text + " " + code).lower()
    if any(x in probe for x in ("insufficient_quota", "billing", "credit balance", "payment required", "authentication", "unauthorized")):
        result.usage_limited = False
        return result
    if not (result.usage_limited or re.search(
        r"usage.?limit|rate.?limit|hit your limit|usage cap|too many requests|resource_exhausted|사용량.{0,12}(한도|제한)|사용 한도", probe)):
        return result
    result.usage_limited = True
    for key in ("resetsAt", "resets_at", "reset_at", "resetAt"):
        stamp = reset_time(data.get(key))
        if stamp:
            result.usage_reset_at = stamp
            return result
    for key in ("retry_after", "retryAfter", "retry_after_seconds"):
        try:
            seconds = float(data[key])
            if math.isfinite(seconds) and seconds >= 0:
                result.usage_reset_at = now + seconds
                return result
        except (KeyError, TypeError, ValueError):
            pass
    match = re.search(r"(?:resets?(?:_at)?|retry[_ -]after|try again (?:in|after))[: =]+(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?\b", text, re.I)
    if match:
        value, unit = float(match[1]), (match[2] or "").lower()
        result.usage_reset_at = (value / 1000 if value > 100000000000 else value) if value > 1000000000 and not unit else now + value * (3600 if unit.startswith('h') else 60 if unit.startswith('m') else 1)
    else:
        iso = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", text)
        if iso:
            result.usage_reset_at = reset_time(iso[0])
        else:
            clock = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", text, re.I)
            if clock:
                try:
                    zone = ZoneInfo({'KST':'Asia/Seoul','UTC':'UTC'}.get(clock[4], clock[4]))
                    local = datetime.fromtimestamp(now, zone)
                    hour = int(clock[1]) % 12 + (12 if clock[3].lower() == 'pm' else 0)
                    if not 1 <= int(clock[1]) <= 12:
                        return result
                    target = local.replace(hour=hour, minute=int(clock[2] or 0), second=0, microsecond=0)
                    if target <= local:
                        target += timedelta(days=1)
                    result.usage_reset_at = target.timestamp()
                except (ZoneInfoNotFoundError, ValueError):
                    pass
    return result


class UsageWait:
    def __init__(self, orch):
        self.orch = orch
        self.waits = {}
        self.cooldowns = {}
        self.generation = 0
        self.wakeup = asyncio.Event()
        self.clock = time.time

    @property
    def enabled(self):
        return bool(self.orch.cfg.settings.get('usage_limit_retry'))

    def cancel(self):
        self.generation += 1
        self.cooldowns.clear()
        self.wakeup.set()

    def interrupted(self):
        from ..adapters.base import TurnResult
        return TurnResult('', ok=False, interrupted=True, error='사용량 한도 자동 대기 중단')

    def stopped(self, generation):
        o = self.orch
        return (self.generation != generation or o.stop_requested or not self.enabled
                or (o.max_hours is not None and self.clock() - o.started >= o.max_hours * 3600)
                or (o.budget_usd is not None and o.cost_usd >= o.budget_usd))

    async def _tick(self):
        try:
            await asyncio.wait_for(self.wakeup.wait(), 1)
        except asyncio.TimeoutError:
            pass
        self.wakeup.clear()

    async def wait(self, ad, until, generation, attempt):
        label = ad.label
        self.waits[label] = {'role': label, 'cli': ad.role.cli, 'until': until, 'attempt': attempt}
        when = datetime.fromtimestamp(until).astimezone().strftime('%m/%d %H:%M:%S %Z')
        self.orch.set_activity(label, 'usage_wait', f'사용량 한도 · {when} 이후 재시도')
        self.orch.notice(f'{label}: 사용량 한도 — {when}까지 대기합니다. /stop 또는 /usage-retry off로 취소할 수 있습니다.', 'warn')
        self.orch.emit_status()
        try:
            while self.clock() < until or not self.orch.not_paused.is_set():
                if self.stopped(generation):
                    return False
                await self._tick()
            return not self.stopped(generation)
        finally:
            self.waits.pop(label, None)
            self.orch.set_activity(label, 'idle')
            self.orch.emit_status()

    async def run(self, ad, prompt, invoke=None):
        invoke = invoke or (lambda text: ad.run_turn(text))
        if not self.enabled:
            return await invoke(prompt)
        generation = self.generation
        cli = ad.role.cli
        retries = 0
        while True:
            cooldown = self.cooldowns.get(cli, 0) if self.enabled else 0
            if cooldown > self.clock():
                if not await self.wait(ad, cooldown, generation, retries):
                    return self.interrupted()
            if retries and self.stopped(generation):
                return self.interrupted()
            result = classify(await invoke(prompt))
            if not self.enabled or result.ok or not result.usage_limited or result.interrupted or result.context_overflow:
                return result
            if retries >= int(self.orch.cfg.settings.get('usage_limit_max_retries', 3)):
                self.orch.notice(f'{ad.label}: 사용량 한도 재시도 횟수를 초과해 멈춥니다.', 'warn')
                return result
            if self.stopped(generation):
                return self.interrupted()
            retries += 1
            now = self.clock()
            reset = result.usage_reset_at
            until = reset + 5 if reset and math.isfinite(reset) and reset > now else now + float(self.orch.cfg.settings.get('usage_limit_wait_sec', 18000))
            self.cooldowns[cli] = max(until, self.cooldowns.get(cli, 0))
            if not await self.wait(ad, self.cooldowns[cli], generation, retries):
                return self.interrupted()
            # Keep the session/thread ID; reconnect transports which may expire during a long wait.
            await ad.close()
            if self.stopped(generation):
                return self.interrupted()
            await ad.start()
            self.orch.set_activity(ad.label, 'thinking', '사용량 한도 대기 후 같은 작업 재개')
            prompt = ('[duet 사용량 한도 후 재개] 같은 작업의 이어서 진행입니다. 이미 실행한 변경·명령의 결과를 확인하고 '
                      '완료한 작업을 중복 실행하지 마세요. 미완료 부분만 이어가세요.\n\n' + prompt) if retries == 1 else prompt
