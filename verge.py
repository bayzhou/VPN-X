"""Control the running Clash Verge core and own a reversible Windows proxy session."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode, urlsplit

import pywintypes
import requests
import win32api
import win32event
import win32file
import win32job
import win32pipe
import yaml

from catalog import VPNError, atomic, direct_hosts, protect, url

VERGE_APP_ID = "io.github.clash-verge-rev.clash-verge-rev"
VERGE_HOME = Path(os.environ["APPDATA"]) / VERGE_APP_ID
LEGACY_DATA = Path(os.environ["LOCALAPPDATA"]) / "VPN-X"
DATA = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
ACTIVE = "VPN-X 出口"
HIDDEN = "vx-managed-session"
CREATE_NO_WINDOW = 0x08000000
INSTALLER_NAME = re.compile(r"Clash[. ]Verge_(\d+\.\d+\.\d+)_x64-setup\.exe", re.IGNORECASE)
LOCAL_DOMAINS = ("localhost", "local", "lan", "home.arpa")
LOCAL_NETWORKS = ("127.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16",
                  "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10")
GEO_ASSETS = ("geosite.dat", "Country.mmdb")


class PipeIO(io.RawIOBase):
    def __init__(self, path, timeout):
        if not re.fullmatch(r"\\\\\.\\pipe\\verge-mihomo[-a-zA-Z0-9_]*", path):
            raise VPNError("Clash Verge 的控制管道格式无效")
        self.timeout = timeout
        win32pipe.WaitNamedPipe(path, min(1500, int(timeout * 1000)))
        self.handle = win32file.CreateFile(
            path, 0xC0000000, 0, None, 3, 0x40000000, None)

    def readable(self):
        return True

    def _run(self, op, buffer):
        ov = pywintypes.OVERLAPPED()
        ov.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            status, _ = op(self.handle, buffer, ov)
            if status == 997 and win32event.WaitForSingleObject(
                    ov.hEvent, int(self.timeout*1000)) != win32event.WAIT_OBJECT_0:
                win32file.CancelIoEx(self.handle, ov)
                try:
                    win32file.GetOverlappedResult(self.handle, ov, True)
                except pywintypes.error:
                    pass
                raise TimeoutError()
            return win32file.GetOverlappedResult(self.handle, ov, True)
        finally:
            ov.hEvent.Close()

    def readinto(self, out):
        buf = win32file.AllocateReadBuffer(len(out))
        try:
            n = self._run(win32file.ReadFile, buf)
        except pywintypes.error as e:
            if e.winerror in (109, 232, 233):
                return 0
            raise
        out[:n] = memoryview(buf)[:n]
        return n

    def send(self, data):
        while data:
            n = self._run(win32file.WriteFile, data)
            if not n:
                raise OSError("pipe write failed")
            data = data[n:]

    def makefile(self, mode):
        return io.BufferedReader(self)

    def close(self):
        if not self.closed and hasattr(self, "handle"):
            self.handle.Close()
        super().close()


def pipe_server_executable(handle):
    kernel = C.WinDLL("kernel32", use_last_error=True)
    get_pid = kernel.GetNamedPipeServerProcessId
    get_pid.argtypes = (wintypes.HANDLE, C.POINTER(wintypes.ULONG))
    pid = wintypes.ULONG()
    if not get_pid(int(handle), C.byref(pid)):
        raise C.WinError(C.get_last_error())
    process = win32api.OpenProcess(0x1000, False, pid.value)
    try:
        query = kernel.QueryFullProcessImageNameW
        query.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                          C.POINTER(wintypes.DWORD))
        path = C.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        if not query(int(process), 0, path, C.byref(size)):
            raise C.WinError(C.get_last_error())
        return Path(path.value).resolve()
    finally:
        process.Close()


class Core:
    def __init__(self, *, pipe="", endpoint="", secret="", local_images=None):
        self.pipe, self.endpoint, self.secret = pipe, endpoint, secret or ""
        self.local_images = local_images

    def call(self, method, route, payload=None, timeout=8):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        headers = {"Authorization": "Bearer " + self.secret, "Content-Type": "application/json",
                   "Connection": "close"}
        try:
            if self.pipe:
                pipe = PipeIO(self.pipe, timeout)
                with pipe:
                    if self.local_images is not None and pipe_server_executable(pipe.handle) not in self.local_images:
                        raise VPNError("此控制接口属于其他目录的 Clash Verge")
                    headers.update(Host="localhost", **{"Content-Length": str(len(body))})
                    lines = [f"{method} {route} HTTP/1.1", *(f"{k}: {v}" for k, v in headers.items())]
                    pipe.send("\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body)
                    with http.client.HTTPResponse(pipe, method=method) as response:
                        response.begin()
                        status = response.status
                        data = response.read(32 * 1024 * 1024)
            else:
                with requests.Session() as session:
                    session.trust_env = False
                    r = session.request(method, self.endpoint + route, data=body, headers=headers,
                                        timeout=timeout, allow_redirects=False)
                    status, data = r.status_code, r.content
        except (OSError, requests.RequestException, http.client.HTTPException, pywintypes.error):
            raise VPNError("无法连接 Clash Verge / Mihomo 本机接口") from None
        if not 200 <= status < 300:
            raise VPNError(f"Mihomo {method} {route.split('?')[0].split('/')[1]}：HTTP {status}")
        try:
            return json.loads(data) if data else None
        except ValueError:
            raise VPNError("Mihomo 本机接口返回了无法解析的数据") from None

    def reload(self, config):
        # JSON is valid YAML. Payload avoids granting the core access to extra paths.
        payload = config if isinstance(config, str) else json.dumps(config, ensure_ascii=False)
        self.call("PUT", "/configs?force=true", {"payload": payload}, timeout=35)

    def proxies(self):
        return self.call("GET", "/proxies")["proxies"]

    def select(self, group, node):
        self.call("PUT", "/proxies/" + quote(group, safe=""), {"name": node})

    def delay(self, node, address):
        params = urlencode({"url": address, "timeout": 4500, "expected": "200-299"})
        return self.call("GET", f"/proxies/{quote(node, safe='')}/delay?{params}", timeout=6)["delay"]


def managed_exe():
    return DATA / "clash" / "clash-verge.exe"


def find_verge():
    exe = managed_exe()
    core = exe.with_name("verge-mihomo.exe")
    folder = DATA.resolve() / "clash"
    if (not exe.is_file() or not core.is_file()
            or any(p.resolve().parent != folder for p in (exe, core))):
        raise VPNError("程序目录缺少完整的 clash 文件夹，请将发布包完整解压")
    expected = installer_version(bundled_installer())
    if file_version(exe) != expected:
        raise VPNError(f"clash 目录中的 Clash Verge 版本应为 {expected}，请重新解压发布包")
    return exe


def file_version(exe):
    try:
        version = win32api.GetFileVersionInfo(str(exe), "\\")
    except pywintypes.error:
        return ""
    ms, ls = version["FileVersionMS"], version["FileVersionLS"]
    return f"{ms >> 16}.{ms & 65535}.{ls >> 16}"


def installer_version(package):
    match = INSTALLER_NAME.fullmatch(package.name)
    if not match:
        raise VPNError("内置安装包的名称应包含正式版本号和 x64")
    return match[1]


def ensure_verge(log):
    exe = find_verge()
    log(f"使用程序目录 clash 中的 Clash Verge {file_version(exe)}")
    return exe


def start_verge(exe):
    subprocess.Popen([str(exe)], cwd=exe.parent, creationflags=CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect_verge(log, exe=None, *, home=None, restore_legacy=False):
    home = home or VERGE_HOME
    deadline = time.monotonic() + (26 if exe else 3)
    launched = False
    local_images = None if restore_legacy else {
        DATA.resolve() / "clash" / name for name in ("verge-mihomo.exe", "verge-mihomo-alpha.exe")
    }
    while True:
        try:
            runtime = (home / "clash-verge.yaml").read_text(encoding="utf-8-sig")
            doc = yaml.safe_load(runtime)
            address = doc.get("external-controller") or ""
            secret = doc.get("secret") or ""
            pipe = doc.get("external-controller-pipe")
            clients = [Core(pipe=p, secret=secret, local_images=local_images) for p in dict.fromkeys(
                [pipe, pipe.replace("-sidecar-release-", "-production-"),
                 pipe.replace("-production-", "-sidecar-release-")])] if isinstance(pipe, str) and pipe else []
            if restore_legacy and re.fullmatch(r"(?:127\.0\.0\.1|localhost):\d{1,5}", address) and secret:
                clients.append(Core(endpoint="http://" + address, secret=secret))
            for client in clients:
                try:
                    if client.call("GET", "/version", timeout=2).get("meta"):
                        log("已连接 Clash Verge 的 Mihomo 内核")
                        return client, runtime, doc
                except VPNError:
                    continue
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            pass
        if exe and not launched:
            log("启动 Clash Verge，等待内核就绪")
            start_verge(exe)
            launched = True
        if time.monotonic() >= deadline:
            raise VPNError("无法连接程序目录中的 Clash Verge。若其他位置的 Verge 正在运行，请先退出后重试")
        time.sleep(0.4)


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            s = socket.socket()
            sockets.append(s)
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


def _listener(name, port, group, credential):
    return {"name": name, "port": port, "listen": "127.0.0.1", "type": "mixed",
            "proxy": group, "users": [{"username": "vpnx", "password": credential}]}


class Bench:
    """Temporary loopback core; no TUN or system proxy changes."""
    def __init__(self, verge_exe, log, *, purpose="连接测试", with_geo=False):
        self.exe = verge_exe.with_name("verge-mihomo.exe")
        self.log = log
        self.root = None
        self.process = None
        self.job = None
        self.purpose = purpose
        self.with_geo = with_geo
        if (not self.exe.is_file()
                or self.exe.resolve().parent != DATA.resolve() / "clash"):
            raise VPNError("找不到程序目录 clash 中的 verge-mihomo.exe")

    def __enter__(self):
        self.root = tempfile.TemporaryDirectory(prefix="bench-", dir=DATA)
        try:
            if self.with_geo:
                for name in GEO_ASSETS:
                    asset = self.exe.parent / "resources" / name
                    if asset.is_file():
                        shutil.copyfile(asset, Path(self.root.name) / name)
            control, = free_ports(1)
            secret = secrets.token_urlsafe(24)
            address = f"127.0.0.1:{control}"
            self.core = Core(endpoint="http://"+address, secret=secret)
            self.base = {"external-controller": address, "secret": secret,
                         "mode": "rule", "allow-lan": False, "log-level": "silent",
                         "tun": {"enable": False}, "rules": ["MATCH,DIRECT"]}
            path = Path(self.root.name) / "bench.json"
            path.write_text(json.dumps(self.base), encoding="utf-8")
            self.process = subprocess.Popen([str(self.exe), "-d", self.root.name, "-f", str(path)],
                                            creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            process = win32api.OpenProcess(0x0101, False, self.process.pid)
            try:
                win32job.AssignProcessToJobObject(self.job, process)
            finally:
                process.Close()
            deadline = time.monotonic() + 9
            while self.process.poll() is None and time.monotonic() < deadline:
                try:
                    self.core.call("GET", "/version", timeout=1)
                    self.log(f"{self.purpose}内核已在后台启动")
                    return self
                except VPNError:
                    time.sleep(0.2)
            raise VPNError(f"{self.purpose}内核启动失败")
        except Exception:
            self.__exit__(None, None, None)
            raise

    def configure(self, nodes):
        self.core.reload(dict(self.base, proxies=[n.config for n in nodes]))

    def __exit__(self, *_):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.process = None
        if self.job:
            self.job.Close()
            self.job = None
        if self.root:
            self.root.cleanup()
            self.root = None


class Value(C.Union):
    _fields_ = [("number", C.c_uint32), ("text", C.c_void_p), ("time", C.c_uint64)]


class Option(C.Structure):
    _fields_ = [("id", C.c_uint32), ("value", Value)]


class Options(C.Structure):
    _fields_ = [("size", C.c_uint32), ("connection", C.c_wchar_p), ("count", C.c_uint32),
                ("error", C.c_uint32), ("options", C.POINTER(Option))]


def win_proxy(values=None):
    """Read/set the LAN WinINet mode, server, bypass and PAC using native APIs."""
    lib = C.WinDLL("wininet", use_last_error=True)
    call = lib.InternetSetOptionW if values is not None else lib.InternetQueryOptionW
    options = (Option * 4)(*(Option(i) for i in range(1, 5)))
    prefs = Options(C.sizeof(Options), None, 4, 0, options)
    if values is not None:
        options[0].value.number = values[0]
        buffers = [C.create_unicode_buffer(s or "") for s in values[1:]]
        for opt, buf in zip(options[1:], buffers):
            opt.value.text = C.cast(buf, C.c_void_p)
        if not call(None, 75, C.byref(prefs), C.sizeof(prefs)):
            raise VPNError("无法设置 Windows 系统代理")
        lib.InternetSetOptionW(None, 39, None, 0)
        lib.InternetSetOptionW(None, 37, None, 0)
        return
    size = C.c_uint32(C.sizeof(prefs))
    if not call(None, 75, C.byref(prefs), C.byref(size)):
        raise VPNError("无法读取 Windows 系统代理")
    free = C.WinDLL("kernel32").GlobalFree
    free.argtypes = (C.c_void_p,)
    try:
        return [options[0].value.number] + [
            C.wstring_at(opt.value.text) if opt.value.text else "" for opt in options[1:]]
    finally:
        for opt in options[1:]:
            if opt.value.text:
                free(opt.value.text)


def proxy_bypass(previous, direct):
    domains, addresses = direct
    entries = ["<local>", "127.*", "10.*", "169.254.*", "192.168.*",
               *(f"172.{second}.*" for second in range(16, 32))]
    for name in (*LOCAL_DOMAINS, *domains):
        entries.extend((name, f"*.{name}"))
    entries.extend(f"[{ip}]" if ":" in ip else ip for ip in ("::1", *addresses))
    entries.extend(x.strip() for x in previous.split(";") if x.strip())
    return ";".join(dict((entry.casefold(), entry) for entry in entries).values())


def active_config(profiles, doc, settings, mixed, probe, token, *, geo, direct):
    routing = []
    proxies, groups = {}, []
    for p in profiles:
        for n in p.nodes.values():
            proxies[n.key] = n.config
        for idx, (name, nodes) in enumerate(p.groups.items()):
            if nodes:
                key = f"vx-g-{p.vendor.id}-{idx}"
                groups.append({"name": key, "type": "select", "proxies": [n.key for n in nodes]})
                routing.append((p.vendor.id, name, key))
    if not routing:
        raise VPNError("没有包含节点的代理组")
    group_keys = [key for _, _, key in routing]
    groups += [{"name": ACTIVE, "type": "select", "proxies": group_keys},
               {"name": HIDDEN, "type": "select", "proxies": [ACTIVE], "hidden": True}]
    check_host = urlsplit(url(settings["check"])).hostname
    try:
        ip = ipaddress.ip_address(check_host)
        check_rule = f"IP-CIDR{ '6' if ip.version == 6 else ''},{ip}/{ip.max_prefixlen},{ACTIVE},no-resolve"
    except ValueError:
        check_rule = f"DOMAIN,{check_host.encode('idna').decode()},{ACTIVE}"
    tun = settings["network"] == "tun"
    domains, addresses = direct
    domains = list(dict.fromkeys((*LOCAL_DOMAINS, *domains)))
    networks = list(dict.fromkeys((*LOCAL_NETWORKS,
                                  *(str(ipaddress.ip_network(ip)) for ip in addresses))))
    direct_rules = [f"DOMAIN-SUFFIX,{host},DIRECT" for host in domains]
    direct_rules += [f"IP-CIDR{'6' if ':' in cidr else ''},{cidr},DIRECT,no-resolve" for cidr in networks]
    direct_rules.append("DOMAIN-SUFFIX,cn,DIRECT")
    if "geosite.dat" in geo:
        direct_rules.append("GEOSITE,cn,DIRECT")
    if "Country.mmdb" in geo:
        # Known destination IPs can use GEOIP without waiting for a DNS lookup on every proxy request.
        direct_rules.append("GEOIP,CN,DIRECT,no-resolve")
    dns_domains = [f"+.{host}" for host in domains]
    config = {
        "mode": settings["mode"], "allow-lan": False, "bind-address": "127.0.0.1",
        "mixed-port": mixed, "log-level": "warning", "profile": {"store-selected": False},
        "geodata-mode": False, "geo-auto-update": False,
        "tun": {"enable": False, "stack": "mixed", "auto-route": True, "strict-route": True,
                "dns-mode": "native", "route-exclude-address": networks,
                "auto-detect-interface": True, "dns-hijack": ["any:53", "tcp://any:53"]},
        "dns": {"enable": tun, "enhanced-mode": "fake-ip", "fake-ip-range": "198.18.0.1/16",
                "fake-ip-filter": dns_domains,
                "nameserver-policy": dict.fromkeys(dns_domains, "system"),
                "direct-nameserver": ["system"],
                "nameserver": ["223.5.5.5", "1.1.1.1"]},
        "listeners": [_listener("vx-probe", probe, ACTIVE, token)],
        "proxies": list(proxies.values()), "proxy-groups": groups,
        "rules": [check_rule, *direct_rules, f"MATCH,{ACTIVE}"],
    }
    for k in ("external-controller", "external-controller-pipe", "secret", "external-controller-cors"):
        if k in doc:
            config[k] = doc[k]
    return config, {(v, n): k for v, n, k in routing}


class Ownership:
    def __init__(self, log, *, data_dir=None):
        self.log = log
        self.journal = (data_dir if data_dir is not None else DATA) / "restore.json.dpapi"
        self.core = None
        self.probe = None
        self.group_keys = {}
        self.session_id = None
        self.headless = None

    def stop_headless(self):
        if self.headless:
            self.headless.__exit__(None, None, None)
            self.headless = None

    def prepare(self, exe):
        if exe.resolve() != managed_exe().resolve():
            raise VPNError("只能使用程序目录 clash 中的 Clash Verge")
        if self.headless and self.headless.process.poll() is None:
            return
        self.stop_headless()
        try:
            self.headless = Bench(exe, self.log, purpose="Clash Verge", with_geo=True).__enter__()
            return
        except (OSError, pywintypes.error, VPNError):
            self.log("后台内核启动失败，改用 Clash Verge 窗口")
        connect_verge(self.log, exe)

    def begin(self, exe, profiles, settings):
        if self.journal.exists():
            self.restore()
        self.prepare(exe)
        if self.headless:
            self.core = self.headless.core
            doc = self.headless.base
            runtime = json.dumps(doc)
            geo_home = Path(self.headless.root.name)
        else:
            self.core, runtime, doc = connect_verge(self.log, exe)
            geo_home = VERGE_HOME
        live = self.core.call("GET", "/configs")
        existing = self.core.proxies()
        if HIDDEN in existing:
            raise VPNError("当前内核已经被其他 VPN-X 会话接管，请先恢复 Clash Verge")
        selected = {name: p["now"] for name, p in existing.items()
                    if p.get("type") == "Selector" and p.get("now") in p.get("all", [])}
        mixed, probe = free_ports(2)
        token = secrets.token_urlsafe(20)
        geo = {name for name in GEO_ASSETS if (geo_home / name).is_file()}
        if len(geo) != len(GEO_ASSETS) and settings["mode"] == "rule":
            self.log("未找到完整国内分流数据；请检查发布包的 clash/resources 目录")
        direct = direct_hosts(settings.get("direct_hosts", ""))
        config, self.group_keys = active_config(profiles, doc, settings, mixed, probe, token,
                                              geo=geo, direct=direct)
        self.session_id = "vx-session-" + secrets.token_hex(12)
        config["proxy-groups"].append({"name": self.session_id, "type": "select",
                                       "hidden": True, "proxies": [ACTIVE]})
        before = win_proxy()
        after = ([3, f"127.0.0.1:{mixed}", proxy_bypass(before[2], direct), ""]
                 if settings["network"] == "system" else [1, "", "", ""])
        entry = {"runtime": runtime, "mode": live["mode"], "tun": live.get("tun", {}).get("enable", False),
                 "selected": selected, "before": before, "after": after, "marker": self.session_id,
                 "headless": bool(self.headless), "local_clash": True}
        atomic(self.journal, protect(json.dumps(entry).encode()).encode())
        self.log("已保存 Clash Verge 运行配置、已选节点和 Windows 代理设置")
        self.core.reload(config)
        if settings["mode"] == "rule":
            self.log("规则分流：本机、内网、国内域名/IP 和自定义例外直连")
        if settings["network"] == "system":
            self.log("系统代理将保留原有例外，并补充本机、常见内网和自定义地址")
        self.probe = f"http://vpnx:{token}@127.0.0.1:{probe}"
        self.mixed = mixed
        self.after = after
        self.network = settings["network"]
        self.mode = settings["mode"]
        self.log("已载入 VPN-X 配置，等待选定节点后验证网络")

    def choose(self, candidate):
        if not self.core or not self.session_id:
            raise VPNError("VPN-X 会话尚未启动")
        core = self.core
        if self.session_id not in core.proxies():
            raise VPNError("Clash Verge 已在外部切换配置，请重新启动 VPN-X 会话")
        group = self.group_keys[(candidate.vendor.id, candidate.group)]
        core.select(group, candidate.node.key)
        core.select(ACTIVE, group)
        if self.mode == "global":
            core.select("GLOBAL", ACTIVE)

    def enable(self):
        want_tun = self.network == "tun"
        if self.core.call("GET", "/configs").get("tun", {}).get("enable") != want_tun:
            self.core.call("PATCH", "/configs", {"tun": {"enable": want_tun}})
        actual = self.core.call("GET", "/configs")
        if (actual.get("mixed-port") != self.mixed or
                actual.get("tun", {}).get("enable") != want_tun or actual.get("mode") != self.mode):
            raise VPNError("Mihomo 未成功启用所选的代理模式或虚拟网卡")
        if win_proxy() != self.after:
            win_proxy(self.after)

    def restore(self):
        if not self.journal.exists():
            return
        entry = json.loads(protect(self.journal.read_bytes(), decode=True))
        # Restore our LAN setting even when the Verge process is no longer present.
        if win_proxy() == entry["after"]:
            win_proxy(entry["before"])
        if entry.get("headless"):
            # Our Windows job also terminates this process if VPN-X exits unexpectedly.
            self.stop_headless()
            client = None
        else:
            client, _, _ = connect_verge(self.log, home=Path(entry.get("home", VERGE_HOME)),
                                         restore_legacy=not entry.get("local_clash"))
        if client and entry["marker"] in client.proxies():
            baseline = yaml.safe_load(entry["runtime"])
            baseline["mode"] = entry["mode"]
            baseline.setdefault("tun", {})["enable"] = entry["tun"]
            client.reload(yaml.safe_dump(baseline, allow_unicode=True, sort_keys=False))
            available = client.proxies()
            for group, node in entry["selected"].items():
                if node in available.get(group, {}).get("all", []):
                    client.select(group, node)
            self.log("已恢复 Clash Verge 原配置和节点选择")
        elif client:
            self.log("Clash Verge 已载入其他配置；保留当前配置")
        self.journal.unlink()
        self.core = self.probe = self.session_id = None
        self.group_keys = {}
        self.log("已关闭 VPN-X 代理并恢复 Windows 代理设置")


def bundled_installer():
    roots = [Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "installers",
             Path(sys.executable).parent / "installers"] if getattr(sys, "frozen", False) else [
                 Path(__file__).parent / "installers"]
    for root in roots:
        found = [(tuple(map(int, match[1].split("."))), path) for path in root.glob("*.exe")
                 if (match := INSTALLER_NAME.fullmatch(path.name))]
        if found:
            return max(found)[1]
    raise VPNError("未找到内置安装包。请将安装包放入 installers 目录")
