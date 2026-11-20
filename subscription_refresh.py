"""Bounded, staggered HTTP attempts for an individual vendor subscription."""
import asyncio
from contextlib import AsyncExitStack
from urllib.parse import urljoin, urlsplit

import httpx

from catalog import DOWNLOAD_HEADERS, MAX_DOWNLOAD, VPNError, parse_yaml, profile_sections, url

ATTEMPT_SECONDS = 5
ROUND_SECONDS = 15
ATTEMPT_INTERVAL = 1
MAX_IN_FLIGHT = 5
MAX_ATTEMPTS = 11


class RateLimited(VPNError):
    pass


def validated_document(data):
    document = parse_yaml(data)
    nodes, providers, _ = profile_sections(document)
    if not nodes and not providers:
        raise VPNError("订阅未返回节点或节点源")
    return document


async def fetch_attempt(client, address, deadline):
    try:
        async with asyncio.timeout_at(deadline):
            for _ in range(6):
                async with client.stream("GET", address) as response:
                    location = response.headers.get("location")
                    if location and response.status_code in (301, 302, 303, 307, 308):
                        address = url(urljoin(address, location), https=urlsplit(address).scheme == "https")
                        continue
                    if response.status_code == 429:
                        raise RateLimited("HTTP 429，厂商限流，已停止本轮请求")
                    if not 200 <= response.status_code < 300:
                        raise VPNError(f"HTTP {response.status_code}")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(16384):
                        data.extend(chunk)
                        if len(data) > MAX_DOWNLOAD:
                            raise VPNError("下载超过大小上限")
                    document = await asyncio.to_thread(validated_document, bytes(data))
                    return document, response.headers
            raise VPNError("重定向次数过多")
    except (TimeoutError, httpx.TimeoutException):
        raise VPNError("订阅请求超时") from None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
        # Client error details may include a private subscription URL.
        raise VPNError(f"网络请求失败（{type(error).__name__}）") from None


async def race(address, proxy, cancelled, *, interval=ATTEMPT_INTERVAL,
               attempt_seconds=ATTEMPT_SECONDS, round_seconds=ROUND_SECONDS):
    address = url(address)
    loop = asyncio.get_running_loop()
    pending = set()
    attempts = 0
    last_error = VPNError("订阅请求超时")
    async with AsyncExitStack() as resources:
        options = dict(headers=DOWNLOAD_HEADERS, trust_env=False, timeout=None)
        direct = await resources.enter_async_context(httpx.AsyncClient(**options))
        fallback = await resources.enter_async_context(httpx.AsyncClient(proxy=proxy, **options)) if proxy else direct
        next_attempt = loop.time()
        deadline = next_attempt + round_seconds
        try:
            while pending or attempts < MAX_ATTEMPTS:
                cancelled()
                now = loop.time()
                if now >= deadline:
                    break
                if attempts < MAX_ATTEMPTS and len(pending) < MAX_IN_FLIGHT and now >= next_attempt:
                    client = fallback if attempts else direct
                    pending.add(asyncio.create_task(fetch_attempt(client, address, min(deadline, now + attempt_seconds))))
                    attempts += 1
                    next_attempt = now + interval

                wait = min(0.2, deadline - now)
                if attempts < MAX_ATTEMPTS and len(pending) < MAX_IN_FLIGHT:
                    wait = min(wait, next_attempt - now)
                if not pending:
                    await asyncio.sleep(wait)
                    continue
                done, _ = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                cancelled()
                for task in done:
                    pending.remove(task)
                    try:
                        return task.result()
                    except RateLimited:
                        raise
                    except VPNError as error:
                        last_error = error
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    raise VPNError(f"已尝试 {attempts} 次，未取得可用订阅：{last_error}")


def download_subscription(address, *, proxy, cancelled):
    # Do not join a timed-out system DNS lookup when returning the round result.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(race(address, proxy, cancelled))
    finally:
        loop.close()
