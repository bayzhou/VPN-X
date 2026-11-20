import asyncio
from contextlib import suppress
import unittest

from catalog import VPNError
from subscription_refresh import RateLimited, race


async def respond(writer, status, body=b""):
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()


class SubscriptionRaceTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, handle):
        async def connection(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                await handle(reader, writer)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

        server = await asyncio.start_server(connection, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/subscription"

    async def test_first_usable_document_cancels_pending_requests(self):
        received = 0
        slow_closed = asyncio.Event()

        async def handle(reader, writer):
            nonlocal received
            received += 1
            if received == 1:
                await reader.read()
                slow_closed.set()
            elif received == 2:
                await respond(writer, "200 OK", b"error: temporarily unavailable")
            else:
                await respond(writer, "200 OK", b"proxies: [{name: Demo, type: http, server: node.test}]")

        address = await self.serve(handle)
        document, _ = await race(address, None, lambda: None, interval=0.07, attempt_seconds=1, round_seconds=2)
        self.assertEqual(document["proxies"][0]["name"], "Demo")
        self.assertEqual(received, 3)
        await asyncio.wait_for(slow_closed.wait(), timeout=0.5)

    async def test_rate_limit_stops_new_attempts(self):
        received = 0

        async def handle(reader, writer):
            nonlocal received
            received += 1
            await respond(writer, "429 Too Many Requests")

        address = await self.serve(handle)
        with self.assertRaises(RateLimited):
            await race(address, None, lambda: None, interval=0.15, attempt_seconds=0.5, round_seconds=1)
        self.assertEqual(received, 1)

    async def test_stalled_body_respects_attempt_and_round_deadlines(self):
        received = 0
        closed = 0

        async def handle(reader, writer):
            nonlocal received, closed
            received += 1
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 50000\r\n\r\nproxies: []")
            await writer.drain()
            await reader.read()
            closed += 1

        address = await self.serve(handle)
        with self.assertRaisesRegex(VPNError, "订阅请求超时"):
            await asyncio.wait_for(race(address, None, lambda: None, interval=0.04,
                                        attempt_seconds=0.22, round_seconds=0.8), timeout=1.4)
        await asyncio.sleep(0.05)
        self.assertGreater(received, 5)
        self.assertLessEqual(received, 11)
        self.assertEqual(received, closed)
