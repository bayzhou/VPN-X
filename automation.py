"""Refresh, benchmark, rank and try nodes without blocking the GUI."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Event
import time

from catalog import (
    CatalogSource, PUBLIC_SETTINGS, CatalogDownloadError, VPNError, atomic,
    decode_catalog, fetch_profile,
    region_rank, request_data, url,
)
from verge import Bench, DATA, Ownership, find_verge

DEFAULTS = dict(PUBLIC_SETTINGS, **CatalogSource().settings())


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

    def sort_key(self, priorities):
        return (region_rank(self.node.name, priorities),
                -(self.quota if self.quota is not None else -1),
                self.delay if self.delay is not None else float("inf"),
                self.vendor.name, self.node.name)


class Engine:
    def __init__(self, log, progress=None):
        self.log = log
        self.progress = progress or (lambda *_: None)
        self.own = Ownership(log)
        self.catalog = None
        self.imported_settings = {}
        self.profiles = []
        self.results = []
        self.queue = []
        self.current = None
        self.failures = []
        self.last_sync = None
        self.settings = None
        self.cancel = Event()

    def cancelled(self):
        if self.cancel.is_set():
            raise VPNError("操作已取消")

    def sync(self, settings, path=None, *, stale=False):
        self.imported_settings = {}
        source = CatalogSource(settings["catalog"], settings["password"])
        if not source.configured:
            raise VPNError("请在「高级模式」中设置 VXP 链接和密钥")
        cache = DATA / f"subscriptions-{source.identity()}.vxp"
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
                    payload, _, _ = request_data(source.url,
                                                 limit=1024 * 1024, revalidate=True)
                except VPNError as e:
                    raise CatalogDownloadError(str(e)) from None
            self.progress("校验并解密目录", 0, 0)
            self.log("校验完整性并解密订阅目录")
            result, imported = decode_catalog(payload, source.key)
        except (OSError, VPNError) as e:
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

    def refresh(self, settings, path=None, *, stale=False):
        self.failures = []
        self.sync(settings, path, stale=stale)
        profiles = {}
        proxy = self.own.probe if self.current and self.own.probe else None

        def fetch(address, **kwargs):
            try:
                return request_data(address, **kwargs)
            except VPNError:
                # A system proxy is not inherited by the downloader. If direct
                # access failed, retry the active VPN-X outbound when available.
                if not proxy:
                    raise
                return request_data(address, proxy=proxy, **kwargs)

        def update(vendor):
            self.cancelled()
            self.log(f"{vendor.name}：更新订阅")
            issues = []
            def report(message):
                issues.append(vendor.name)
                self.log(message)
            return fetch_profile(vendor, report, fetch), issues

        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = {pool.submit(update, v): v for v in self.catalog}
            self.progress("更新厂商订阅", 0, len(jobs))
            for completed, job in enumerate(as_completed(jobs), 1):
                v = jobs[job]
                try:
                    p, issues = job.result()
                    if issues:
                        self.failures.append(f"{v.name}（部分节点源失败）")
                    if len(p.nodes) > 0:
                        profiles[v.id] = p
                    quota = f"{p.quota / 2**30:.2f} GiB" if p.quota is not None else "未知"
                    self.log(f"{v.name}：{len(p.nodes)} 节点 / {len(p.groups)} 组，剩余 {quota}")
                except Exception as e:
                    reason = str(e) if isinstance(e, VPNError) else type(e).__name__
                    self.log(f"{v.name}：订阅失败：{reason}")
                    self.failures.append(v.name)
                self.progress("更新厂商订阅", completed, len(jobs))
        self.cancelled()
        self.profiles = [profiles[v.id] for v in self.catalog if v.id in profiles]
        self.results = []
        if not self.profiles:
            raise VPNError("所有订阅更新失败，请查看日志")

    def test_candidates(self, settings, choice):
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
            matches = [n for n in nodes if region_rank(n.name, priorities) < len(priorities)]
            if nodes and not matches:
                self.log(f"{p.vendor.name} / {group}：首组没有命中优先地区的节点")
            result.extend(Candidate(p.vendor, group, n, p.quota) for n in matches)
        if not result:
            raise VPNError("首组没有命中优先地区的节点")
        return result

    def measure(self, settings, choice=None):
        check = url(settings["check"])
        self.results = []
        self.results = self.test_candidates(settings, choice)
        exe = find_verge()
        all_nodes = {n.key: n for p in self.profiles for n in p.nodes.values()}
        if len(all_nodes) > 6000:
            raise VPNError("所有订阅合计超过 6000 个节点，请缩减订阅范围")
        self.log(f"开始连接测试：{len(self.results)} 个首组节点命中优先地区")
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
                if not self.results:
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

            with ThreadPoolExecutor(max_workers=4) as pool:
                tasks = [pool.submit(test, c) for c in self.results]
                self.progress("测试节点延迟", 0, len(tasks))
                for completed, task in enumerate(as_completed(tasks), 1):
                    task.result()
                    self.progress("测试节点延迟", completed, len(tasks))
        self.progress("排序候选节点", 0, 0)
        self.results.sort(key=lambda c: c.sort_key(preferred_regions(settings)))
        self.log(f"已排序 {len(self.results)} 个节点：地区优先级 → 订阅剩余流量 → 连接延迟")

    def auto(self, settings):
        self.refresh(settings, stale=True)
        settings = dict(settings, **self.imported_settings)
        self.measure(settings)
        self.activate(settings)

    def restart(self, settings):
        self.progress("正在关闭代理", 0, 0)
        self.log("重启：先关闭代理，再更新订阅")
        self.close()
        self.own.stop_headless()
        self.cancelled()
        self.auto(settings)

    def activate(self, settings, choice=None, node=None):
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
                     and region_rank(c.node.name, priorities) < len(priorities)]
            picks.sort(key=lambda c: c.sort_key(priorities))
        if not picks:
            raise VPNError("没有可启动的候选节点")
        if self.own.journal.exists():
            self.close()
        self.settings = settings.copy()
        try:
            self.progress("启动代理内核并载入配置", 0, 0)
            self.own.begin(find_verge(), self.profiles, settings)
            if verify:
                self.queue = picks
                self.next_node()
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
        raise VPNError("所有候选节点都未通过测试，已恢复原网络设置")

    def close(self):
        self.own.restore()
        self.current = None
        self.queue = []
