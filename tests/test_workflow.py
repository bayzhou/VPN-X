"""Windows integration test using local HTTP nodes and an isolated real Mihomo."""
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import Mock, patch

import automation
from catalog import (CatalogDownloadError, CatalogSource, Node, Profile, Vendor, decode_catalog, decrypt,
                     direct_hosts, encrypt, parse_yaml, VPNError, vendors)
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
    def test_background_refresh_distinguishes_catalog_membership_from_failed_fetches(self):
        engine = automation.Engine(lambda *_: None)
        a, b, c = [Vendor(name, f"https://{name}.example/subscribe") for name in "abc"]
        def profile(vendor):
            node = Node(vendor.name, "香港节点", {"server": f"{vendor.name}.example"})
            return Profile(vendor, {node.key: node}, {"Proxy": [node]}, None)
        old, recovered, new = map(profile, (a, b, c))
        settings = dict(automation.DEFAULTS, regions="香港")
        engine.own = Mock()
        engine.current = engine.list_candidates([old])[0]
        engine.settings = settings.copy()
        engine.live_profiles = engine.profiles = [old]
        engine.profile_catalog = {a.id, b.id}
        engine.complete_profiles = {a.id}

        def download(vendor, report, fetch, *, download):
            result = responses[vendor.id]
            if result is None:
                raise VPNError("network failure")
            return result

        with patch.object(engine, "sync"), patch.object(automation, "fetch_profile", side_effect=download):
            engine.catalog = [a, b]
            responses = {a.id: None, b.id: recovered}
            engine.refresh(settings, background=True)
            self.assertIsNone(engine.pending_profiles)
            self.assertEqual(engine.pending_revision, 0)

            # Confirmed catalog addition counts even when the new vendor is unreachable.
            engine.catalog = [a, b, c]
            responses = {a.id: None, b.id: recovered, c.id: None}
            engine.refresh(settings, background=True)
            self.assertEqual(engine.pending_catalog, {a.id, b.id, c.id})
            self.assertEqual(engine.pending_profiles, [old, recovered])
            self.assertEqual(engine.pending_revision, 1)
            responses[c.id] = new
            engine.refresh(settings, background=True)
            self.assertEqual(engine.pending_profiles, [old, recovered, new])
            self.assertEqual(engine.pending_revision, 1)

            # Removing the initially unreachable vendor also counts as a catalog change.
            engine.catalog = [a]
            responses = {a.id: None}
            engine.refresh(settings, background=True)
            self.assertEqual(engine.pending_catalog, {a.id})
            self.assertEqual(engine.pending_profiles, [old])
            self.assertEqual(engine.pending_revision, 2)
        engine.own.restore.assert_not_called()

    def test_background_refresh_only_notifies_for_new_profile_or_settings_changes(self):
        engine = automation.Engine(lambda *_: None)
        vendor = Vendor("Demo", "https://vendor.example/subscription")
        node = Node("test-node", "香港示例", {"server": "node.example"})
        profile = Profile(vendor, {node.key: node}, {"Proxy": [node]}, 100)
        settings = dict(automation.DEFAULTS, regions="香港")
        engine.current = engine.list_candidates([profile])[0]
        engine.live_profiles = engine.profiles = [profile]
        engine.settings = settings.copy()
        engine.catalog = [vendor]
        engine.own = Mock()
        with patch.object(engine, "sync"), patch.object(automation, "fetch_profile", return_value=replace(profile, quota=99)):
            engine.refresh(settings, background=True)
            self.assertIsNone(engine.pending_profiles)
            engine.imported_settings = {"mode": "global"}
            engine.refresh(settings, background=True)
            self.assertEqual(engine.pending_settings["mode"], "global")
            self.assertEqual(engine.pending_revision, 1)
            engine.refresh(settings, background=True)
            self.assertEqual(engine.pending_revision, 1)
        engine.own.restore.assert_not_called()
        engine.own.begin.assert_not_called()
        self.assertEqual(engine.settings["mode"], "rule")

    def test_confirmed_subscription_restart_uses_cached_profiles_and_prefers_current_node(self):
        engine = automation.Engine(lambda *_: None)
        vendor = Vendor("Demo", "https://vendor.example/subscription")
        first = Node("first", "香港目前", {})
        second = Node("second", "日本新增", {})
        profiles = [Profile(vendor, {n.key: n for n in (first, second)}, {"Proxy": [first, second]}, None)]
        engine.current = engine.list_candidates(profiles)[0]
        engine.pending_profiles = profiles
        engine.own = Mock()
        engine.own.journal.exists.return_value = False
        settings = dict(automation.DEFAULTS, regions="日本,香港")
        with (patch.object(automation, "find_verge", return_value=Path("clash")),
              patch.object(engine, "auto") as auto, patch.object(engine, "refresh") as refresh,
              patch.object(engine, "measure") as measure, patch.object(engine, "next_node") as connect):
            engine.restart(settings)
        auto.assert_not_called()
        refresh.assert_not_called()
        measure.assert_not_called()
        connect.assert_called_once()
        engine.own.begin.assert_called_once_with(Path("clash"), profiles, settings)
        self.assertEqual([c.node.key for c in engine.queue], ["first", "second"])

    def test_health_requires_two_failed_cycles_and_pretests_each_backup(self):
        engine = automation.Engine(lambda *_: None)
        vendor = Vendor("Demo", "https://vendor.example/subscription")
        current, first, second = [automation.Candidate(vendor, "Proxy", Node(str(i), name, {}), None)
                                  for i, name in enumerate(("香港当前", "香港失效", "香港可用"))]
        first.delay, second.delay = 10, 20
        engine.current = current
        engine.own = Mock()
        engine.own.probe_candidate.side_effect = [VPNError("unavailable"), 42]
        engine.settings = dict(automation.DEFAULTS, regions="香港")
        now = time.monotonic()
        engine.backups = {c.identity: (c, now) for c in (first, second)}
        with patch.object(automation, "request_data", side_effect=[VPNError("offline")] * 4) as request:
            engine.check_health()
            self.assertEqual(request.call_count, 2)
            self.assertEqual(engine.health_failures, 1)
            engine.own.switch_loaded.assert_not_called()
            engine.check_health()
            self.assertEqual(request.call_count, 4)
        self.assertEqual([call.args[0] for call in engine.own.probe_candidate.call_args_list], [first, second])
        engine.own.switch_loaded.assert_called_once_with(current, second)
        engine.own.restore.assert_not_called()
        self.assertIs(engine.current, second)
        self.assertEqual(engine.health_failures, 0)

    def test_backup_round_expands_by_region_and_limits_each_vendor_per_region(self):
        engine = automation.Engine(lambda *_: None)
        engine.settings = dict(automation.DEFAULTS, regions="香港,日本")
        def profile(name, priority, names):
            vendor = Vendor(name, f"https://{name}.example/subscription", priority)
            nodes = [Node(f"{name}-{index}", n, {}) for index, n in enumerate(names)]
            return Profile(vendor, {n.key:n for n in nodes}, {"Proxy":nodes}, None)
        a = profile("A", 1, ["香港当前", "香港失败", "日本一", "日本二", "日本三"])
        b = profile("B", 2, ["香港失败", "香港失败", "香港失败", "香港失败", "日本备用"])
        c = profile("C", 3, ["香港不测", "日本备用"])
        engine.live_profiles = [c, b, a]
        engine.current = engine.list_candidates([a])[0]
        engine.own = Mock()
        def probe(candidate, _):
            if "失败" in candidate.node.name:
                raise VPNError("offline")
            return 10
        engine.own.probe_candidate.side_effect = probe
        engine.refresh_backups()
        tried = [call.args[0] for call in engine.own.probe_candidate.call_args_list]
        self.assertEqual([node.vendor.name for node in tried[:4]], ["A", "B", "B", "B"])
        self.assertTrue(all(sum(n.vendor.name == name and n.node.name.startswith(region) for n in tried) <= 3
                            for name in "ABC" for region in ("香港", "日本")))
        self.assertNotIn("香港当前", [n.node.name for n in tried])
        self.assertNotIn("香港不测", [n.node.name for n in tried])
        self.assertGreaterEqual(len(engine.available_backups()), 2)
        engine.own.switch_loaded.assert_not_called()

    def test_startup_backups_use_first_region_results_for_all_vendors_and_stop_at_two(self):
        engine = automation.Engine(lambda *_: None)
        engine.settings = dict(automation.DEFAULTS, regions="香港,日本")
        for name in "ABC":
            vendor = Vendor(name, f"https://{name}.example/subscription")
            nodes = [Node(f"{name}-{i}", n, {}) for i, n in enumerate(("香港一", "香港二", "香港三", "日本一"))]
            engine.live_profiles.append(Profile(vendor, {n.key: n for n in nodes}, {"Proxy": nodes}, None))
        tested = engine.list_candidates(engine.live_profiles)
        engine.current = tested[0]
        for i, candidate in enumerate(tested):
            if candidate.node.name.startswith("香港"):
                candidate.delay = 50 + i
        engine.own = Mock()
        engine.refresh_backups(initial=tested, tested_through=0)
        backups = engine.available_backups()
        self.assertEqual(len(backups), 6)
        self.assertTrue(all(sum(c.vendor.name == name for c in backups) == 2 for name in "ABC"))
        self.assertTrue(all(c.node.name.startswith("香港") for c in backups))
        engine.own.probe_candidate.assert_not_called()

    def test_failover_prefers_vendor_priority_over_backup_region_and_latency(self):
        engine = automation.Engine(lambda *_: None)
        engine.settings = dict(automation.DEFAULTS, regions="香港,日本")
        a = Vendor("Preferred", "https://a.example/sub", 1)
        b = Vendor("Lower", "https://b.example/sub", 2)
        engine.current = automation.Candidate(b, "Proxy", Node("current", "香港当前", {}), 999)
        slow = automation.Candidate(a, "Proxy", Node("preferred", "日本备用", {}), 0, 200)
        fast = automation.Candidate(b, "Proxy", Node("lower", "香港备用", {}), 999, 1)
        now = time.monotonic()
        engine.backups = {fast.identity: (fast, now), slow.identity: (slow, now)}
        engine.own = Mock()
        engine.own.probe_candidate.return_value = 200
        current = engine.current
        engine.switch_backup()
        engine.own.probe_candidate.assert_called_once_with(slow, engine.settings["check"])
        engine.own.switch_loaded.assert_called_once_with(current, slow)

    def test_startup_backup_expansion_stops_at_configured_region_limit(self):
        engine = automation.Engine(lambda *_: None)
        vendor = Vendor("Demo", "https://vendor.example/sub")
        nodes = [Node(str(i), name, {}) for i, name in enumerate(
            ("香港当前", "香港备用", "日本一", "日本二", "日本三", "日本四", "新加坡节点"))]
        profile = Profile(vendor, {n.key: n for n in nodes}, {"Proxy": nodes}, None)
        engine.live_profiles = [profile]
        tested = engine.list_candidates([profile])[:2]
        engine.current = tested[0]
        tested[1].delay = 20
        engine.settings = dict(automation.DEFAULTS, regions="香港")
        engine.own = Mock()
        engine.own.probe_candidate.return_value = 40
        engine.refresh_backups(initial=tested, tested_through=0)
        self.assertEqual(len(engine.available_backups()), 1)
        engine.own.probe_candidate.assert_not_called()

        engine.settings["regions"] = "香港,日本,新加坡"
        engine.refresh_backups(initial=tested, tested_through=0)
        tried = [call.args[0].node.name for call in engine.own.probe_candidate.call_args_list]
        self.assertEqual(len(set(tried)), 3)
        self.assertTrue(all(name.startswith("日本") for name in tried))
        self.assertEqual(len(engine.available_backups()), 2)

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
  - {{name: 'Second', type: select, proxies: [日本第二组, 日本备选]}}
""".encode()

            def fetch(url, **kwargs):
                if url == address:
                    download_states.append((windows[0], engine.current))
                    return package, {}, 0
                return real_request(url, **kwargs)

            def download(url, **kwargs):
                self.assertEqual(url, provider)
                return parse_yaml(subscription), {"subscription-userinfo": "upload=1; download=2; total=10"}

            stack.enter_context(patch.object(automation, "request_data", fetch))
            stack.enter_context(patch.object(automation, "download_subscription", download))
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
            engine.refresh(dict(settings, proxy_settings_locked=True), local)
            self.assertEqual(engine.imported_settings, {})
            self.assertEqual(len(engine.profiles), 1)
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
            results = {c.node.name: c for c in engine.results}
            self.assertEqual(set(results), {"香港优先", "日本备选", "日本高延迟", "新加坡备选"})
            self.assertEqual(results["日本备选"].quota, 7)
            self.assertLess(results["日本备选"].delay, results["日本高延迟"].delay)
            self.assertIsNone(results["香港优先"].delay)
            self.assertEqual(engine.current.node.name, "日本备选")
            self.assertTrue(any("香港优先：测试失败" in line for line in logs))
            self.assertFalse(other_region_hits)
            self.assertFalse(later_group_hits)
            self.assertEqual(windows[0], 3)
            self.assertIn("*.legacy.example", windows[2].split(";"))
            self.assertIn("*.office.example", windows[2].split(";"))
            process = engine.own.headless.process
            current = engine.current
            first_group = engine.own.group_keys[(current.vendor.id, current.group)]
            previous_proxy = windows[:]
            subscription = subscription.replace(b"proxies:\n", b"proxies:\n  - {name: New, type: http, server: 127.0.0.1, port: 12345}\n", 1)
            engine.refresh(settings, background=True)
            self.assertEqual(windows, previous_proxy)
            self.assertIs(engine.own.headless.process, process)
            self.assertIs(engine.current, current)
            self.assertEqual(engine.own.core.proxy_info(first_group)["now"], current.node.key)
            self.assertNotIn("New", engine.profiles[0].nodes)
            self.assertIn("New", engine.pending_profiles[0].nodes)
            with self.assertRaises(VPNError):
                engine.test_and_use(settings, results["香港优先"])
            self.assertEqual(engine.own.core.proxy_info(first_group)["now"], current.node.key)
            self.assertIs(engine.current, current)
            with patch.object(engine, "refresh", side_effect=AssertionError("must not fetch")):
                engine.restart(dict(settings, **exported_settings))
            self.assertEqual(engine.current.node.name, "日本备选")
            self.assertIn("New", engine.profiles[0].nodes)
            engine.restart(settings)
            self.assertEqual(download_states[-1], (1, None))
            self.assertIsNotNone(process.poll())
            self.assertEqual(engine.current.node.name, "日本备选")
            self.assertIn(("正在关闭代理", 0, 0), progress)
            self.assertIn(("更新厂商订阅", 1, 1), progress)
            self.assertIn(("测试节点延迟", 1, 1), progress)
            self.assertIn(("测试节点延迟", 2, 2), progress)
            self.assertIn("New", engine.profiles[0].nodes)
            self.assertTrue(engine.available_backups())
            engine.switch_backup()
            self.assertIn(engine.current.node.name, ("日本高延迟", "日本第二组"))
            settings.update(exported_settings)
            later_choice = (engine.profiles[0].vendor.id, "Second")
            with self.assertRaisesRegex(VPNError, "第一个代理组"):
                engine.measure(settings, later_choice)
            engine.activate(settings, later_choice)
            self.assertEqual(engine.current.node.name, "日本第二组")
            self.assertTrue(engine.current.verified)
            self.assertFalse(other_region_hits)
            self.assertTrue(later_group_hits)
            engine.close()
            self.assertEqual(windows, before)
            self.assertIsNone(engine.own.headless)
            self.assertFalse(engine.own.journal.exists())
