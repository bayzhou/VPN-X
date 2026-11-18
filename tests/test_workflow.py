"""Windows integration test using local HTTP nodes and an isolated real Mihomo."""
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import patch

import automation
from catalog import (CatalogDownloadError, CatalogSource, Node, Vendor, decode_catalog, decrypt,
                     direct_hosts, encrypt, VPNError, vendors)
import verge


@contextmanager
def outbound(code, hits=None, latency=0):
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def response(self):
            if hits is not None:
                hits.append(True)
            if latency:
                time.sleep(latency)
            self.wfile.write(f"HTTP/1.1 {code} Test\r\nContent-Length: 0\r\n"
                             "Connection: close\r\n\r\n".encode())

        def do_CONNECT(self):
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b""):
                pass
            self.response()

        def do_GET(self):
            self.response()

        def do_HEAD(self):
            self.response()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    server.timeout = 2
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class Workflow(unittest.TestCase):
    def test_vendor_priority_precedes_quota_within_a_region(self):
        rows = [
            {"name": "Preferred", "url": "https://test.example/1", "priority": 1},
            {"name": "Same priority", "url": "https://test.example/2", "priority": 1},
            {"name": "Older catalog", "url": "https://test.example/3"},
            {"name": "Lower priority", "url": "https://test.example/4", "priority": 2},
        ]
        preferred, tied, unset, lower = vendors(rows)
        self.assertIsNone(unset.priority)
        for invalid in (0, -1, 1000, True, "1"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(VPNError, "优先级"):
                vendors([dict(rows[0], priority=invalid)])

        def candidate(vendor, name, quota, delay):
            return automation.Candidate(vendor, "Proxy", Node("node", name, {}), quota, delay)
        candidates = [candidate(unset, "香港未设置", 500, 1),
                      candidate(lower, "香港低优先", 400, 2),
                      candidate(tied, "香港流量多", 300, 100),
                      candidate(preferred, "香港低延迟", 100, 50),
                      candidate(preferred, "香港高延迟", 100, 200),
                      candidate(preferred, "日本", 1000, 1)]
        self.assertEqual([c.node.name for c in sorted(candidates, key=lambda c: c.sort_key(["香港", "日本"]))],
                         ["香港流量多", "香港低延迟", "香港高延迟", "香港低优先", "香港未设置", "日本"])

    def test_windows_proxy_bypass_retains_existing_entries(self):
        exceptions = direct_hosts("*.OFFICE.example, 10.9.8.7，fd00::2")
        self.assertEqual(exceptions, (["office.example"], ["10.9.8.7", "fd00::2"]))
        exclusions = verge.proxy_bypass("*.legacy.example; <local>", exceptions).split(";")
        for expected in ("<local>", "10.*", "172.16.*", "172.31.*", "192.168.*",
                         "*.legacy.example", "office.example", "*.office.example", "10.9.8.7", "[fd00::2]"):
            self.assertIn(expected, exclusions)
        self.assertEqual(exclusions.count("<local>"), 1)
        with self.assertRaises(VPNError):
            direct_hosts("https://office.example/path")

    def test_empty_catalog_source_does_not_contact_server(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "catalog-source.json"
            CatalogSource().save(path)
            source = CatalogSource.load(path)
            self.assertFalse(source.configured)
            with patch.object(automation, "request_data") as request:
                with self.assertRaises(VPNError):
                    automation.Engine(lambda *_: None).refresh(dict(automation.DEFAULTS, **source.settings()))
                request.assert_not_called()

    def test_requires_clash_directory_even_when_another_copy_exists(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            exe = root / "existing install" / "clash-verge.exe"
            exe.parent.mkdir()
            exe.touch()
            exe.with_name("verge-mihomo.exe").touch()
            package = root / "Clash.Verge_2.5.10_x64-setup.exe"
            with (patch.object(verge, "DATA", root / "VPN-X"),
                  patch.object(verge, "bundled_installer", return_value=package),
                  patch.object(verge, "file_version", return_value="2.5.10")):
                with self.assertRaises(VPNError):
                    verge.find_verge()
                with self.assertRaises(VPNError):
                    verge.Ownership(lambda *_: None).prepare(exe)
                local = verge.DATA / "clash" / "clash-verge.exe"
                local.parent.mkdir(parents=True)
                local.touch()
                local.with_name("verge-mihomo.exe").touch()
                self.assertEqual(verge.find_verge(), local)
                self.assertEqual(verge.ensure_verge(lambda *_: None), local)

    def test_catalog_rank_failover_and_restore(self):
        provider = "https://vendor.example/subscription"
        address = "https://catalog.example/catalog.vxp"
        source = CatalogSource(address, "test-source-key")
        password = source.key
        exported_settings = {"regions": "香港，日本，新加坡", "network": "system"}
        package = encrypt([{"name": "Demo", "url": provider, "priority": 2}], password, exported_settings)
        self.assertEqual(decrypt(package, password)[0], Vendor("Demo", provider, 2))
        self.assertEqual(decode_catalog(package, password)[1], exported_settings)
        with self.assertRaises(VPNError):
            decrypt(package[:-1] + bytes([package[-1] ^ 1]), password)
        try:
            exe = verge.find_verge()
        except VPNError:
            self.skipTest("Build or unpack the local clash folder")
        with ExitStack() as stack:
            tmp = Path(stack.enter_context(TemporaryDirectory()))
            stack.enter_context(patch.object(automation, "DATA", tmp))
            stack.enter_context(patch.object(verge, "DATA", tmp))
            local = tmp / "clash"
            local.mkdir()
            shutil.copy2(exe, local / exe.name)
            shutil.copy2(exe.with_name("verge-mihomo.exe"), local / "verge-mihomo.exe")
            (local / "resources").mkdir()
            geo_assets = [name for name in verge.GEO_ASSETS if (exe.parent / "resources" / name).is_file()]
            for name in geo_assets:
                shutil.copyfile(exe.parent / "resources" / name, local / "resources" / name)
            early = stack.enter_context(outbound(503))
            first = stack.enter_context(outbound(200))
            second = stack.enter_context(outbound(200))
            slow = stack.enter_context(outbound(200, latency=0.15))
            other_region_hits, later_group_hits = [], []
            other_region = stack.enter_context(outbound(200, other_region_hits))
            later_group = stack.enter_context(outbound(200, later_group_hits))
            logs = []
            progress = []
            engine = automation.Engine(logs.append, lambda *values: progress.append(values))
            stack.callback(engine.own.stop_headless)
            before = [1, "previous", "*.legacy.example", ""]
            windows = before[:]
            download_states = []

            def win_proxy(values=None):
                if values is None:
                    return windows[:]
                windows[:] = values

            stack.enter_context(patch.object(verge, "win_proxy", win_proxy))
            real_request = automation.request_data
            subscription = f"""
proxies:
  - {{name: 香港优先, type: http, server: 127.0.0.1, port: {early}}}
  - {{name: 日本备选, type: http, server: 127.0.0.1, port: {first}}}
  - {{name: 新加坡备选, type: http, server: 127.0.0.1, port: {second}}}
  - {{name: 日本高延迟, type: http, server: 127.0.0.1, port: {slow}}}
  - {{name: 美国, type: http, server: 127.0.0.1, port: {other_region}}}
  - {{name: 日本第二组, type: http, server: 127.0.0.1, port: {later_group}}}
proxy-groups:
  - {{name: 'Proxy', type: select, proxies: [DIRECT, 日本高延迟, 香港优先, 日本备选, 新加坡备选, 美国]}}
  - {{name: 'Second', type: select, proxies: [日本第二组]}}
""".encode()

            def fetch(url, **kwargs):
                if url == address:
                    download_states.append((windows[0], engine.current))
                    return package, {}, 0
                if url == provider:
                    return subscription, {"subscription-userinfo": "upload=1; download=2; total=10"}, 0
                return real_request(url, **kwargs)

            stack.enter_context(patch.object(automation, "request_data", fetch))
            source_file = tmp / "catalog-source.json"
            source.save(source_file)
            settings = dict(automation.DEFAULTS, **CatalogSource.load(source_file).settings())
            settings["check"] = "http://example.invalid/check"
            settings["direct_hosts"] = "*.office.example"
            with patch.object(automation, "request_data", side_effect=VPNError("network failure")):
                with self.assertRaises(CatalogDownloadError):
                    engine.refresh(settings)
            local = tmp / "local.vxp"
            local.write_bytes(package)
            engine.refresh(settings, local)
            self.assertEqual(engine.imported_settings, exported_settings)
            engine.auto(settings)
            self.assertIsNotNone(engine.own.headless)
            live_rules = {(rule["type"].lower(), rule["payload"].lower(), rule["proxy"])
                          for rule in engine.own.core.call("GET", "/rules")["rules"]}
            self.assertIn(("domainsuffix", "office.example", "DIRECT"), live_rules)
            self.assertIn(("domainsuffix", "cn", "DIRECT"), live_rules)
            if "geosite.dat" in geo_assets:
                self.assertIn(("geosite", "cn", "DIRECT"), live_rules)
            if "Country.mmdb" in geo_assets:
                self.assertIn(("geoip", "cn", "DIRECT"), live_rules)
            direct_hits = []
            origin = stack.enter_context(outbound(200, direct_hits))
            real_request(f"http://127.0.0.1:{origin}/local", proxy=f"http://127.0.0.1:{engine.own.mixed}", limit=0)
            self.assertEqual(direct_hits, [True])
            self.assertEqual([c.node.name for c in engine.results],
                             ["香港优先", "日本备选", "日本高延迟", "新加坡备选"])
            self.assertEqual(engine.results[1].quota, 7)
            self.assertLess(engine.results[1].delay, engine.results[2].delay)
            self.assertEqual(engine.current.node.name, "日本备选")
            self.assertTrue(any("香港优先：验证失败" in line for line in logs))
            self.assertEqual(windows[0], 3)
            self.assertIn("*.legacy.example", windows[2].split(";"))
            self.assertIn("*.office.example", windows[2].split(";"))
            process = engine.own.headless.process
            engine.restart(settings)
            self.assertEqual(download_states[-1], (1, None))
            self.assertIsNotNone(process.poll())
            self.assertEqual(engine.current.node.name, "日本备选")
            self.assertIn(("正在关闭代理", 0, 0), progress)
            self.assertIn(("更新厂商订阅", 1, 1), progress)
            self.assertIn(("测试节点延迟", 4, 4), progress)
            engine.next_node()
            self.assertEqual(engine.current.node.name, "日本高延迟")
            settings.update(exported_settings)
            later_choice = (engine.profiles[0].vendor.id, "Second")
            with self.assertRaisesRegex(VPNError, "第一个代理组"):
                engine.measure(settings, later_choice)
            engine.activate(settings, later_choice)
            self.assertEqual(engine.current.node.name, "日本第二组")
            self.assertFalse(engine.current.verified)
            self.assertFalse(other_region_hits)
            self.assertFalse(later_group_hits)
            engine.close()
            self.assertEqual(windows, before)
            self.assertIsNone(engine.own.headless)
            self.assertFalse(engine.own.journal.exists())
