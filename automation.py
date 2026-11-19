"""Refresh, benchmark, rank and try nodes without blocking the GUI."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import random
from threading import Event
import time

from catalog import (
    CatalogSource, PUBLIC_SETTINGS, CatalogDownloadError, VPNError, atomic,
    decode_catalog, fetch_profile,
    region_rank, request_data, url,
)
from verge import Bench, DATA, Ownership, ensure_verge, find_verge

DEFAULTS = dict(PUBLIC_SETTINGS, direct_hosts="", **CatalogSource().settings())
HEALTH_INTERVAL = 5 * 60
BACKUP_INTERVAL = 10 * 60
BACKUP_LIFETIME = 2 * BACKUP_INTERVAL + HEALTH_INTERVAL
STARTUP_PROBE_WORKERS = 12
BACKUP_PROBE_WORKERS = 4


class NoReachableNode(VPNError):
    pass


def preferred_regions(settings):
    return [region.strip() for region in settings["regions"].replace("，", ",").split(",")
            if region.strip()]


@dataclass
class Candidate:
    vendor: object
    group: str
    node: object
    quota: int | None
    delay: int | None = None
    verified: bool = False

    @property
    def identity(self):
        return self.vendor.id, self.group, self.node.key

    def sort_key(self, priorities):
        return (region_rank(self.node.name, priorities),
                self.vendor.priority if self.vendor.priority is not None else float("inf"),
                -(self.quota if self.quota is not None else -1),
                self.delay if self.delay is not None else float("inf"),
                self.vendor.name, self.node.name)

    def backup_sort_key(self, priorities):
        region, vendor, quota, delay, vendor_name, node_name = self.sort_key(priorities)
        return vendor, region, quota, delay, vendor_name, node_name


class Engine:
    def __init__(self, log, progress=None, *, data_dir=None):
        self.log = log
        self.progress = progress or (lambda *_: None)
        self.data_dir = data_dir if data_dir is not None else DATA
        self.own = Ownership(log, data_dir=data_dir)
        self.catalog = None
        self.imported_settings = {}
        self.profiles = []
        self.profile_catalog = None
        self.complete_profiles = None
        self.results = []
        self.queue = []
        self.current = None
        self.failures = []
        self.last_sync = None
        self.settings = None
        self.live_profiles = []
        self.pending_profiles = None
        self.pending_catalog = None
        self.pending_complete = None
        self.pending_settings = None
        self.pending_revision = 0
        self.backups = {}
        self.health_failures = 0
        self.cancel = Event()

    def cancelled(self):
        if self.cancel.is_set():
            raise VPNError("操作已取消")

    def retry(self, operation, label):
        for attempt in range(3):
            self.cancelled()
            try:
                return operation(attempt)
            except VPNError:
                if attempt == 2:
                    raise
                self.log(f"{label}失败，正在第 {attempt + 1}/2 次重试")

    def sync(self, settings, path=None, *, stale=False):
        self.imported_settings = {}
        source = CatalogSource(settings["catalog"], settings["password"])
        if not source.configured:
            raise VPNError("请在「高级模式」中设置 VXP 链接和密钥")
        cache = self.data_dir / f"subscriptions-{source.identity()}.vxp"
        try:
            if path:
                self.progress("读取目录文件", 0, 0)
                self.log("读取本机加密目录")
                from pathlib import Path
                with Path(path).open("rb") as f:
                    payload = f.read(1024 * 1024 + 1)
            else:
                self.progress("下载加密目录", 0, 0)
                self.log("下载加密订阅目录")
                try:
                    payload, _, _ = self.retry(lambda attempt: request_data(
                        source.url, limit=1024 * 1024, revalidate=True,
                        **({"proxy": self.own.probe} if attempt and self.current and self.own.probe else {})),
                        "下载加密目录")
                except VPNError as e:
                    raise CatalogDownloadError(str(e)) from None
            self.progress("校验并解密目录", 0, 0)
            self.log("校验完整性并解密订阅目录")
            result, imported = decode_catalog(payload, source.key)
        except (OSError, VPNError) as e:
            self.cancelled()
            reason = str(e) if isinstance(e, VPNError) else "无法读取目录文件"
            if not stale or not cache.exists():
                if isinstance(e, CatalogDownloadError):
                    raise
                raise VPNError(reason) from None
            self.log(f"远程目录更新失败：{reason}；尝试使用上次缓存")
            self.failures.append("目录更新失败，使用上次缓存")
            self.progress("读取上次缓存", 0, 0)
            result, _ = decode_catalog(cache.read_bytes(), source.key)
        else:
            atomic(cache, payload)
            self.imported_settings = imported
            self.last_sync = time.time()
        self.catalog = result
        self.log(f"目录已就绪：{len(result)} 家厂商")

    def refresh(self, settings, path=None, *, stale=False, background=False):
        self.failures = []
        self.sync(settings, path, stale=stale)
        profiles = {}
        complete = set()
        proxy = self.own.probe if self.current and self.own.probe else None

        def update(vendor):
            self.cancelled()
            self.log(f"{vendor.name}：更新订阅")
            def attempt_profile(attempt):
                issues = []
                first = True
                def report(message):
                    issues.append(vendor.name)
                    self.log(message)
                def fetch(address, **kwargs):
                    nonlocal first
                    def request(index):
                        return request_data(address, **kwargs, **({"proxy": proxy} if index and proxy else {}))
                    if first:
                        first = False
                        return request(attempt)
                    return self.retry(request, f"{vendor.name}节点源请求")
                return fetch_profile(vendor, report, fetch), issues
            return self.retry(attempt_profile, f"{vendor.name}订阅更新")

        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = {pool.submit(update, v): v for v in self.catalog}
            self.progress("更新厂商订阅", 0, len(jobs))
            for completed, job in enumerate(as_completed(jobs), 1):
                v = jobs[job]
                try:
                    p, issues = job.result()
                    if issues:
                        self.failures.append(f"{v.name}（部分节点源失败）")
                    if p.nodes and (not issues or not background):
                        profiles[v.id] = p
                        if not issues:
                            complete.add(v.id)
                    quota = f"{p.quota / 2**30:.2f} GiB" if p.quota is not None else "未知"
                    self.log(f"{v.name}：{len(p.nodes)} 节点 / {len(p.groups)} 组，剩余 {quota}")
                except Exception as e:
                    reason = str(e) if isinstance(e, VPNError) else type(e).__name__
                    self.log(f"{v.name}：订阅失败：{reason}")
                    self.failures.append(v.name)
                self.progress("更新厂商订阅", completed, len(jobs))
        self.cancelled()
        updated = [profiles[v.id] for v in self.catalog if v.id in profiles]
        catalog_ids = {v.id for v in self.catalog}
        if background and self.current:
            # Keep every active core and its node definitions untouched. Failed vendors retain
            # their previous profiles until a later refresh succeeds.
            baseline = self.profile_catalog if self.profile_catalog is not None else {p.vendor.id for p in self.live_profiles}
            baseline_complete = self.complete_profiles if self.complete_profiles is not None else baseline
            previous = {p.vendor.id: p for p in (self.pending_profiles if self.pending_profiles is not None else self.live_profiles)}
            previous_complete = self.pending_complete if self.pending_complete is not None else baseline_complete
            pending = [profiles.get(v.id, previous.get(v.id)) for v in self.catalog if v.id in profiles or v.id in previous]
            if not pending:
                raise VPNError("没有可用的订阅数据，已保留当前代理")
            pending_complete = complete | ((previous_complete - profiles.keys()) & catalog_ids)
            desired = dict(settings, **self.imported_settings)
            changed = (catalog_ids != baseline or self.profiles_differ(
                pending, self.live_profiles, pending_complete & baseline_complete)
                       or any(desired.get(k) != (self.settings or {}).get(k) for k in (*PUBLIC_SETTINGS, "direct_hosts")))
            same_pending = (self.pending_profiles is not None
                            and catalog_ids == self.pending_catalog
                            and not self.profiles_differ(pending, self.pending_profiles, pending_complete & previous_complete)
                            and desired == self.pending_settings)
            if changed and not same_pending:
                self.pending_revision += 1
            self.pending_profiles = pending if changed else None
            self.pending_catalog = catalog_ids if changed else None
            self.pending_complete = pending_complete if changed else None
            self.pending_settings = desired if changed else None
            self.log("已缓存订阅变更，等待用户确认重启" if changed else "订阅和配置没有变化，当前代理保持不变")
        else:
            if not updated:
                raise VPNError("所有订阅更新失败，请查看日志")
            self.profiles = updated
            self.profile_catalog = catalog_ids
            self.complete_profiles = complete
            self.pending_profiles = None
            self.pending_catalog = None
            self.pending_complete = None
            self.pending_settings = None
            self.results = []

    @staticmethod
    def profiles_differ(left, right, comparable):
        def layout(profiles):
            return {p.vendor.id: (p.vendor, p.nodes, tuple(p.groups.items()))
                    for p in profiles if p.vendor.id in comparable}
        before, after = layout(left), layout(right)
        return any(before[key] != after[key] for key in before.keys() & after.keys())

    def test_candidates(self, settings, choice, *, region=None):
        priorities = preferred_regions(settings)
        if not priorities:
            raise VPNError("请先在设置中填写至少一个优先地区")
        result = []
        for p in self.profiles:
            if choice and p.vendor.id != choice[0]:
                continue
            group, nodes = next(iter(p.groups.items()))
            if choice and group != choice[1]:
                raise VPNError("连接测试仅适用于厂商的第一个代理组")
            if not nodes:
                self.log(f"{p.vendor.name} / {group}：首组无节点")
                if not choice:
                    self.failures.append(f"{p.vendor.name}（首组无节点）")
            matches = [n for n in nodes if (region_rank(n.name, priorities) < len(priorities) if region is None
                                              else region_rank(n.name, priorities) == region)]
            if nodes and not matches:
                self.log(f"{p.vendor.name} / {group}：首组没有命中优先地区的节点")
            result.extend(Candidate(p.vendor, group, n, p.quota) for n in matches)
        if not result and region is None:
            raise VPNError("首组没有命中优先地区的节点")
        return result

    def measure(self, settings, choice=None, *, region=None):
        check = url(settings["check"])
        self.results = self.test_candidates(settings, choice, region=region)
        if region is not None and region > 0:
            by_vendor = {}
            for candidate in self.results:
                by_vendor.setdefault(candidate.vendor.id, []).append(candidate)
            self.results = [candidate for group in by_vendor.values()
                            for candidate in random.sample(group, min(3, len(group)))]
        if not self.results:
            return
        if self.current:
            # Under an active TUN, another process could use the current outbound.
            # Probe the named outbound in the active core without selecting it.
            for index, c in enumerate(self.results, 1):
                self.cancelled()
                try:
                    c.delay = self.own.probe_candidate(c, check)
                except VPNError as err:
                    self.log(f"{c.node.name}：测试失败：{err}")
                self.progress("测试节点延迟", index, len(self.results))
            self.results.sort(key=lambda c: c.sort_key(preferred_regions(settings)))
            return
        exe = find_verge()
        all_nodes = {n.key: n for p in self.profiles for n in p.nodes.values()}
        if len(all_nodes) > 6000:
            raise VPNError("所有订阅合计超过 6000 个节点，请缩减订阅范围")
        self.log(f"开始连接测试：{len(self.results)} 条节点记录")
        self.progress("启动测试内核并载入节点", 0, 0)
        with Bench(exe, self.log) as bench:
            invalid = set()

            def valid(nodes):
                self.cancelled()
                try:
                    bench.configure(nodes)
                    return nodes
                except VPNError as e:
                    if "HTTP 400" not in str(e):
                        raise
                    if len(nodes) == 1:
                        invalid.add(nodes[0].key)
                        self.log(f"{nodes[0].name}：Mihomo 不支持此节点配置，跳过")
                        return []
                    middle = len(nodes) // 2
                    return valid(nodes[:middle]) + valid(nodes[middle:])

            nodes = valid(list(all_nodes.values()))
            if not nodes:
                raise VPNError("Mihomo 未能载入任何节点")
            if invalid:
                bench.configure(nodes)
                for p in self.profiles:
                    p.nodes = {k: n for k, n in p.nodes.items() if n.key not in invalid}
                    p.groups = {k: [n for n in ns if n.key not in invalid] for k, ns in p.groups.items()}
                self.results = [c for c in self.results if c.node.key not in invalid]
                if not self.results and region is None:
                    raise VPNError("所选代理组的节点无法载入")

            def test(c):
                self.cancelled()
                prefix = f"{c.vendor.name} / {c.node.name}"
                self.log(f"{prefix}：测试连接")
                try:
                    c.delay = bench.core.delay(c.node.key, check)
                except VPNError as e:
                    self.log(f"{prefix}：测试失败：{e}")
                else:
                    self.log(f"{prefix}：{c.delay} ms")

            unique = {}
            for c in self.results:
                unique.setdefault(c.node.key, c)
            self.probe_many(unique.values(), test, workers=STARTUP_PROBE_WORKERS)
            for c in self.results:
                c.delay = unique[c.node.key].delay
        self.progress("排序候选节点", 0, 0)
        self.results.sort(key=lambda c: c.sort_key(preferred_regions(settings)))
        self.log(f"已排序 {len(self.results)} 个节点：地区优先级 → 厂商优先级 → 订阅剩余流量 → 连接延迟")

    def auto(self, settings, path=None):
        self.progress("检查内置 Clash 内核", 0, 0)
        exe = ensure_verge(self.log)
        self.own.prepare(exe)
        self.refresh(settings, path, stale=path is None)
        settings = dict(settings, **self.imported_settings)
        priorities = preferred_regions(settings)
        if not priorities:
            raise VPNError("请先在设置中填写至少一个优先地区")
        tested = []
        for level in range(len(priorities)):
            self.measure(settings, region=level)
            tested.extend(self.results)
            if any(c.delay is not None for c in self.results):
                self.results = tested
                try:
                    self.activate(settings, startup_tier=level)
                except NoReachableNode:
                    self.cancelled()
                else:
                    return
        raise VPNError("设置的优先地区没有通过连接测试的节点")

    def restart(self, settings):
        if self.pending_profiles is not None:
            self.apply_pending(settings)
            return
        self.progress("正在关闭代理", 0, 0)
        self.log("重启：先关闭代理，再更新订阅")
        self.close()
        self.own.stop_headless()
        self.cancelled()
        self.auto(settings)

    def apply_pending(self, settings):
        if self.pending_profiles is None:
            raise VPNError("没有待应用的订阅更新")
        self.cancelled()
        profiles = self.pending_profiles
        candidates = self.list_candidates(profiles)
        if not candidates:
            raise VPNError("已下载的订阅没有可用节点，已保留当前代理")
        # Prefer the current node if its endpoint is unchanged, then known good backups.
        current = self.current
        known = {c.identity: c for c in self.available_backups()}
        known.update({c.identity: c for c in self.results if c.delay is not None and c.identity not in known})
        if self.settings and settings["check"] != self.settings["check"]:
            known.clear()
        priorities = preferred_regions(settings)
        for c in candidates:
            previous = known.get(c.identity)
            if previous and previous.node == c.node:
                c.delay = previous.delay
        candidates.sort(key=lambda c: (
            0 if current and c.identity == current.identity and c.node == current.node else
            1 if c.delay is not None else 2, c.sort_key(priorities)))
        picks = []
        seen = set()
        for candidate in candidates:
            if candidate.node.key not in seen:
                picks.append(candidate)
                seen.add(candidate.node.key)
        self.progress("正在重启代理并应用已下载配置", 0, 0)
        self.log("使用已下载的订阅重启代理，不重新下载或全量测试")
        self.close()
        self.cancelled()
        self.settings = settings.copy()
        try:
            self.own.begin(find_verge(), profiles, settings)
            self.live_profiles = profiles[:]
            self.queue = picks
            self.next_node()
            self.results = candidates
            self.refresh_backups(initial=self.results)
        except Exception:
            if self.own.journal.exists():
                self.close()
            raise

    def activate(self, settings, choice=None, node=None, *, startup_tier=None):
        self.cancelled()
        priorities = preferred_regions(settings)
        verify = True
        if choice:
            p = next((p for p in self.profiles if p.vendor.id == choice[0]), None)
            nodes = p.groups.get(choice[1], []) if p else []
            if node:
                nodes = [n for n in nodes if n.key == node]
            if not nodes:
                raise VPNError("所选代理组没有对应的节点")
            eligible = [n for n in nodes if region_rank(n.name, priorities) < len(priorities)]
            verify = choice[1] == next(iter(p.groups)) and bool(eligible)
            if verify and not node:
                self.measure(settings, choice)
                picks = self.results[:]
            else:
                picks = [Candidate(p.vendor, choice[1], nodes[0], p.quota)]
        else:
            first_groups = {(p.vendor.id, next(iter(p.groups))) for p in self.profiles}
            picks = [c for c in self.results if (c.vendor.id, c.group) in first_groups
                     and region_rank(c.node.name, priorities) < len(priorities)
                     and (startup_tier is None or c.delay is not None)]
            picks.sort(key=lambda c: c.sort_key(priorities))
        if not picks:
            raise VPNError("没有可启动的候选节点")
        if self.current:
            for candidate in picks:
                try:
                    self.switch_live(candidate)
                except VPNError:
                    self.cancelled()
                    continue
                return
            raise VPNError("没有通过连接测试的节点，已保留当前代理")
        if self.own.journal.exists():
            self.close()
        self.settings = settings.copy()
        try:
            self.progress("启动代理内核并载入配置", 0, 0)
            self.own.begin(find_verge(), self.profiles, settings)
            self.live_profiles = self.profiles[:]
            if verify:
                self.queue = picks
                self.next_node()
                self.refresh_backups(initial=self.results, tested_through=startup_tier)
            else:
                candidate = picks[0]
                self.own.choose(candidate)
                self.own.enable()
                self.current = candidate
                self.log(f"已手动启用 {candidate.vendor.name} / {candidate.node.name}（未测试）")
        except Exception:
            if self.own.journal.exists():
                self.close()
            raise

    def next_node(self):
        self.current = None
        total = len(self.queue)
        while self.queue:
            if self.cancel.is_set():
                self.close()
                self.cancelled()
            c = self.queue.pop(0)
            attempted = total - len(self.queue)
            self.progress(f"验证第 {attempted} 个候选节点", attempted - 1, total)
            self.log(f"尝试 {c.vendor.name} / {c.group} / {c.node.name}（后续 {len(self.queue)} 个）")
            try:
                self.own.choose(c)
                self.log("经选定节点请求测试链接")
                request_data(self.settings["check"], proxy=self.own.probe, limit=0)
                self.own.enable()
                if self.settings["network"] == "tun":
                    self.log("复测虚拟网卡的出站连接")
                    request_data(self.settings["check"], limit=0)
                else:
                    self.log("复测系统代理监听端口的出站连接")
                    request_data(self.settings["check"], proxy=f"http://127.0.0.1:{self.own.mixed}", limit=0)
            except VPNError as e:
                self.log(f"{c.node.name}：验证失败：{e}")
                continue
            c.verified = True
            self.current = c
            self.log(f"代理已启动：{c.vendor.name} / {c.node.name}")
            return
        self.close()
        raise NoReachableNode("所有候选节点都未通过测试，已恢复原网络设置")

    def close(self):
        self.own.restore()
        self.current = None
        self.queue = []
        self.live_profiles = []
        self.backups.clear()
        self.health_failures = 0
        if self.pending_profiles is not None:
            self.profiles, self.pending_profiles = self.pending_profiles, None
            self.profile_catalog, self.pending_catalog = self.pending_catalog, None
            self.complete_profiles, self.pending_complete = self.pending_complete, None
            self.pending_settings = None
            self.results = []

    def shutdown(self):
        self.close()
        self.own.stop_headless()

    def probe_many(self, candidates, test, *, workers, stage="测试节点延迟"):
        candidates = list(candidates)
        if not candidates:
            return
        with ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as pool:
            tasks = [pool.submit(test, candidate) for candidate in candidates]
            self.progress(stage, 0, len(tasks))
            try:
                for completed, task in enumerate(as_completed(tasks), 1):
                    self.cancelled()
                    task.result()
                    self.progress(stage, completed, len(tasks))
            finally:
                if self.cancel.is_set():
                    for task in tasks:
                        task.cancel()

    def available_backups(self, now=None):
        now = time.monotonic() if now is None else now
        candidates = [candidate for candidate, checked_at in self.backups.values()
                      if now - checked_at <= BACKUP_LIFETIME]
        return sorted(candidates, key=lambda c: c.backup_sort_key(preferred_regions(self.settings or DEFAULTS)))

    @staticmethod
    def list_candidates(profiles):
        return [Candidate(p.vendor, group, node, p.quota) for p in profiles
                for group, nodes in p.groups.items() for node in nodes]

    def refresh_backups(self, *, initial=None, tested_through=None):
        if not self.current:
            return
        priorities = preferred_regions(self.settings)
        if not priorities:
            return
        current = self.current
        profiles = sorted(self.live_profiles, key=lambda p: (
            p.vendor.priority if p.vendor.priority is not None else float("inf"),
            -(p.quota if p.quota is not None else -1), p.vendor.name))
        vendors = [p.vendor.id for p in profiles]
        # The next vendor is the best other vendor when the current one is already last.
        ix = vendors.index(current.vendor.id) if current.vendor.id in vendors else -1
        next_vendor = vendors[ix + 1] if ix + 1 < len(vendors) else (vendors[0] if len(vendors) > 1 else None)
        preferred = (current.vendor.id, next_vendor)
        prior = {c.identity: c for c in initial or [] if c.delay is not None}
        counts = {}
        found = {}
        tiers = {}
        for profile in profiles:
            if not profile.groups:
                continue
            vendor = profile.vendor.id
            group, nodes = next(iter(profile.groups.items()))
            for node in nodes:
                if node.key == current.node.key:
                    continue
                level = region_rank(node.name, priorities)
                if level >= len(priorities):
                    continue
                c = Candidate(profile.vendor, group, node, profile.quota)
                tiers.setdefault((level, vendor), []).append(c)

        def probe(level, order):
            reuse = initial is not None and (tested_through is None or level <= tested_through)
            shortlist = {}
            for vendor in dict.fromkeys(order):
                remaining = 2 - counts.get(vendor, 0)
                picks = tiers.get((level, vendor), [])
                if not picks or remaining <= 0:
                    continue
                if reuse:
                    picks = sorted((c for c in picks if c.identity in prior),
                                   key=lambda c: prior[c.identity].delay)
                else:
                    picks = random.sample(picks, min(3, len(picks)))
                shortlist[vendor] = picks

            def test(c):
                self.cancelled()
                try:
                    c.delay = self.own.probe_candidate(c, self.settings["check"])
                except VPNError:
                    pass

            if not reuse:
                workers = BACKUP_PROBE_WORKERS if tested_through is not None else 1
                self.probe_many((c for picks in shortlist.values() for c in picks), test,
                                workers=workers, stage="测试备用节点")
            for vendor, picks in shortlist.items():
                passed = []
                for c in picks:
                    if reuse:
                        c.delay = prior[c.identity].delay
                    if c.delay is None:
                        self.backups.pop(c.identity, None)
                        continue
                    passed.append(c)
                    if not reuse and tested_through is not None:
                        self.results.append(c)
                for c in sorted(passed, key=lambda c: c.delay)[:2 - counts.get(vendor, 0)]:
                    found[c.identity] = (c, time.monotonic())
                    counts[vendor] = counts.get(vendor, 0) + 1

        probe(0, vendors if tested_through is not None else preferred)
        for level in range(1, len(priorities)):
            if len(found) >= 2:
                break
            probe(level, (*preferred, *vendors))
        # Keep previous unexpired results when a round cannot replenish the pool.
        fresh = {c.identity: self.backups[c.identity] for c in self.available_backups()
                 if c.node.key != self.current.node.key}
        if len(found) < 2:
            for identity, value in fresh.items():
                vendor = value[0].vendor.id
                if (identity not in found and counts.get(vendor, 0) < 2
                        and region_rank(value[0].node.name, priorities) < len(priorities)):
                    found[identity] = value
                    counts[vendor] = counts.get(vendor, 0) + 1
        self.backups = found
        self.log(f"备用节点检查完成：{len(self.backups)} 个可用候选（未切换当前节点）")

    def switch_backup(self):
        if not self.current:
            raise VPNError("当前没有运行中的代理")
        for candidate in self.available_backups():
            self.cancelled()
            try:
                self.switch_live(candidate)
            except VPNError as err:
                self.backups.pop(candidate.identity, None)
                self.log(f"{candidate.node.name}：备用节点复测或切换失败：{err}")
                self.cancelled()
            else:
                return
        raise VPNError("暂时没有通过复测的备用节点，已保留当前代理设置")

    def switch_live(self, candidate):
        current = self.current
        if not current or candidate.identity == current.identity:
            return
        self.cancelled()
        try:
            candidate.delay = self.own.probe_candidate(candidate, self.settings["check"])
        except VPNError:
            self.backups.pop(candidate.identity, None)
            raise
        self.cancelled()
        self.own.switch_loaded(current, candidate)
        candidate.verified = True
        self.current = candidate
        self.health_failures = 0
        self.backups.pop(candidate.identity, None)
        self.log(f"已切换到 {candidate.vendor.name} / {candidate.node.name}")

    def test_and_use(self, settings, candidate):
        if self.current:
            self.switch_live(candidate)
            return
        self.cancelled()
        # An explicit user selection may be outside the automatic region/group filters.
        with Bench(find_verge(), self.log) as bench:
            bench.configure([candidate.node])
            candidate.delay = bench.core.delay(candidate.node.key, url(settings["check"]))
        self.cancelled()
        if self.own.journal.exists():
            self.close()
        self.settings = settings.copy()
        try:
            self.own.begin(find_verge(), self.profiles, settings)
            self.live_profiles = self.profiles[:]
            self.queue = [candidate]
            self.next_node()
            self.refresh_backups(initial=self.results)
        except Exception:
            if self.own.journal.exists():
                self.close()
            raise

    def check_health(self):
        if not self.current:
            return
        if not self.own.probe:
            raise VPNError("当前代理没有可用的本地检测端口")
        for _ in range(2):
            self.cancelled()
            try:
                request_data(self.settings["check"], proxy=self.own.probe, limit=0)
            except VPNError:
                continue
            self.health_failures = 0
            return
        self.cancelled()
        self.health_failures += 1
        self.log(f"当前节点连续 {self.health_failures} 个周期未通过连接测试")
        if self.health_failures >= 2:
            try:
                if self.available_backups():
                    self.switch_backup()
                    return
            except VPNError:
                self.cancelled()
            self.refresh_backups()
            self.switch_backup()
