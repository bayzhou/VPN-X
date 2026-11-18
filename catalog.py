"""Encrypted catalogs, bounded downloads, and Clash subscription normalization."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"VPN-X\x00\x01"
MAX_DOWNLOAD = 8 * 1024 * 1024
DEFAULT_CHECK = "https://chatgpt.com/robots.txt"
CLOUDFLARE_CHECK = "https://cp.cloudflare.com/generate_204"
SOURCE_FILENAME = "catalog-source.json"
PUBLIC_SETTINGS = dict(check=DEFAULT_CHECK, hours=24, regions="", network="tun", mode="rule")


class VPNError(Exception):
    pass


class CatalogDownloadError(VPNError):
    pass


def catalog_settings(value):
    if not isinstance(value, dict) or not value.keys() <= PUBLIC_SETTINGS.keys():
        raise VPNError("目录中的软件配置格式无效")
    for key, setting in value.items():
        if type(setting) is not type(PUBLIC_SETTINGS[key]):
            raise VPNError("目录中的软件配置格式无效")
        if key == "check":
            url(setting)
        elif ((key == "hours" and not 1 <= setting <= 720)
              or (key == "network" and setting not in ("tun", "system"))
              or (key == "mode" and setting not in ("rule", "global"))
              or (key == "regions" and len(setting) > 2000)):
            raise VPNError("目录中的软件配置超出允许范围")
    return value.copy()


def label(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()[:150]


def url(value, *, https=False):
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value)):
        raise VPNError("请输入有效的 HTTP(S) 链接")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in (("https",) if https else ("http", "https"))
                and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.fragment):
            _ = parsed.port
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError:
                if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", parsed.hostname.encode("idna").decode()):
                    raise ValueError
            return value
    except (ValueError, UnicodeError):
        pass
    raise VPNError("链接格式无效；订阅和目录需使用 HTTPS" if https else "链接格式无效")


@dataclass(frozen=True)
class CatalogSource:
    url: str = ""
    key: str = ""

    def __post_init__(self):
        if self.url == self.key == "":
            return
        url(self.url, https=True)
        if not isinstance(self.key, str) or not 12 <= len(self.key) <= 1024 or not self.key.strip():
            raise VPNError("目录密钥须包含 12 至 1024 个字符")
        try:
            self.key.encode("utf-8")
        except UnicodeError:
            raise VPNError("目录密钥包含无效字符") from None

    @property
    def configured(self):
        return bool(self.url and self.key)

    @classmethod
    def load(cls, path):
        try:
            with Path(path).open("rb") as stream:
                data = stream.read(32769)
            if len(data) > 32768:
                raise VPNError("目录来源文件超过大小限制")
            value = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            raise VPNError("目录来源文件须为 UTF-8 JSON") from None
        if (not isinstance(value, dict) or set(value) != {"url", "key"}
                or not isinstance(value.get("url"), str)):
            raise VPNError("目录来源文件须包含 url 和 key")
        return cls(value["url"], value["key"])

    def save(self, path):
        atomic(path, (json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def settings(self):
        return {"catalog": self.url, "password": self.key}

    def identity(self):
        return hashlib.sha256(self.url.encode() + b"\x00" + self.key.encode()).hexdigest()[:24]


def request_data(address, *, limit=MAX_DOWNLOAD, seconds=18, proxy=None, revalidate=False):
    url(address)
    started = time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env = False
            if proxy:
                session.proxies.update(http=proxy, https=proxy)
            headers = {"User-Agent": "clash-verge/v2.5 VPN-X/1", "Accept-Encoding": "identity"}
            if revalidate:
                headers["Cache-Control"] = "no-cache"
            # Enforce HTTPS on every redirect when the original URL uses TLS.
            for _ in range(6):
                with session.get(address, stream=True, timeout=(5, 5), allow_redirects=False,
                                 headers=headers) as r:
                    if r.is_redirect:
                        from urllib.parse import urljoin
                        address = url(urljoin(address, r.headers["Location"]),
                                      https=urlsplit(address).scheme == "https")
                        continue
                    if not 200 <= r.status_code < 300:
                        raise VPNError(f"HTTP {r.status_code}")
                    data = bytearray()
                    if not limit:
                        return b"", r.headers, time.monotonic() - started
                    for chunk in r.iter_content(min(16384, limit)):
                        if time.monotonic() - started > seconds:
                            raise VPNError("下载超时")
                        data.extend(chunk)
                        if len(data) > limit:
                            raise VPNError("下载超过大小上限")
                    return bytes(data), r.headers, time.monotonic() - started
            raise VPNError("重定向次数过多")
    except requests.exceptions.RequestException as e:
        # Requests' exception text can contain the full subscription token.
        raise VPNError(f"网络请求失败（{type(e).__name__}）") from None


@dataclass(frozen=True)
class Vendor:
    name: str
    url: str

    @property
    def id(self):
        return hashlib.sha256(self.name.encode()).hexdigest()[:16]


def vendors(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise VPNError("目录需包含 1 至 64 家厂商")
    result, names = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise VPNError("每条记录需包含厂商和订阅链接")
        name = label(row["name"])
        if not name or name.casefold() in names:
            raise VPNError("厂商名称不能为空或重复")
        names.add(name.casefold())
        result.append(Vendor(name, url(row.get("url"), https=True)))
    return result


def _key(password, salt):
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise VPNError("请输入目录密钥")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode("utf-8"))


def encrypt(rows, password, settings=None):
    if len(password) < 12:
        raise VPNError("制作文件时请使用至少 12 个字符的密钥")
    values = [asdict(v) for v in vendors(rows)]
    if settings is not None:
        values = {"vendors": values, "settings": catalog_settings(settings)}
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
    return MAGIC + salt + nonce + AESGCM(_key(password, salt)).encrypt(nonce, payload, MAGIC)


def decode_catalog(data, password):
    start = len(MAGIC)
    if not data.startswith(MAGIC) or not start + 44 <= len(data) <= 1024 * 1024:
        raise VPNError("不是有效的 VPN-X v1 加密目录")
    salt, nonce = data[start:start+16], data[start+16:start+28]
    try:
        decoded = AESGCM(_key(password, salt)).decrypt(nonce, data[start+28:], MAGIC)
        value = json.loads(decoded)
    except (InvalidTag, ValueError, UnicodeError):
        raise VPNError("解密失败：密钥不正确或文件已损坏") from None
    if isinstance(value, list):
        return vendors(value), {}
    if isinstance(value, dict) and value.keys() == {"vendors", "settings"}:
        return vendors(value["vendors"]), catalog_settings(value["settings"])
    raise VPNError("加密目录的内容格式无效")


def decrypt(data, password):
    return decode_catalog(data, password)[0]


def page_password_hash(password, salt=None):
    salt = secrets.token_bytes(16) if salt is None else salt
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def check_page_password(password, stored):
    if len(password) > 256:
        return False
    if not stored:
        return secrets.compare_digest(password.encode(), b"admin")
    try:
        salt = bytes.fromhex(stored.split(":", 1)[0])
        return secrets.compare_digest(page_password_hash(password, salt), stored)
    except (ValueError, TypeError):
        return False


def protect(value, *, decode=False):
    import win32crypt
    if decode:
        return win32crypt.CryptUnprotectData(base64.b64decode(value), None, None, None, 1)[1]
    return base64.b64encode(win32crypt.CryptProtectData(value, "VPN-X", None, None, None, 1)).decode()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class SubscriptionLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0
        self.events = 0

    def compose_node(self, parent, index):
        self.depth += 1
        self.events += 1
        if self.check_event(yaml.AliasEvent) or self.depth > 40 or self.events > 180000:
            raise VPNError("订阅包含 YAML 别名或结构过于复杂")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def parse_yaml(data):
    try:
        doc = yaml.load(data, Loader=SubscriptionLoader)
    except (ValueError, yaml.YAMLError):
        raise VPNError("订阅不是有效的 Clash YAML") from None
    if not isinstance(doc, dict):
        raise VPNError("订阅不是 Clash YAML 配置")
    return doc


def remaining(header):
    items = dict(re.findall(r"(upload|download|total|expire)\s*=\s*(\d{1,19})", header.lower()))
    if items.get("expire") and 0 < int(items["expire"]) <= time.time():
        return 0
    if any(k not in items for k in ("upload", "download", "total")) or not int(items["total"]):
        return None
    return max(0, int(items["total"]) - int(items["upload"]) - int(items["download"]))


TYPES = set("ss ssr vmess vless trojan hysteria hysteria2 tuic wireguard snell socks5 http anytls ssh mieru".split())
# Only outbound definitions enter the elevated core. No subscription ports, scripts,
# external controller, local provider paths, inbound services or custom rules.
DENIED = {"interface-name", "routing-mark", "dialer-proxy", "client-certificate",
          "client-private-key", "ca", "ca-str", "ech-key"}


def scrub(obj, kind):
    if isinstance(obj, list):
        return [scrub(v, kind) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            key = k.lower()
            if key in DENIED or key.endswith(("-file", "-path")) or key.startswith("path-to-"):
                continue
            if key in ("certificate", "private-key"):
                pem = isinstance(v, str) and v.startswith("-----BEGIN ")
                wireguard = (key == "private-key" and kind == "wireguard" and
                             isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9+/]{43}=", v))
                if not pem and not wireguard:
                    continue
            out[k] = scrub(v, kind)
        return out
    return obj if isinstance(obj, (str, int, float, bool)) or obj is None else str(obj)


@dataclass
class Node:
    key: str
    name: str
    config: dict


@dataclass
class Profile:
    vendor: Vendor
    nodes: dict
    groups: dict
    quota: int | None


def parse_profile(vendor, doc, quota, log, fetch):
    nodes, providers, groups = {}, {}, {}

    def add(raw):
        if not isinstance(raw, dict) or raw.get("type") not in TYPES:
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name or name in ("DIRECT", "REJECT", "GLOBAL"):
            return None
        if len(nodes) >= 3000 and name not in nodes:
            raise VPNError("单个订阅超过 3000 个节点")
        if not isinstance(raw.get("server"), str) or not raw["server"]:
            return None
        key = "vx-n-" + hashlib.sha256((vendor.id+"\x00"+name).encode()).hexdigest()[:24]
        config = scrub(raw, raw["type"])
        # Preserve application header casing and keys such as Sec-WebSocket-Protocol.
        nodes.setdefault(name, Node(key, label(name), dict(config, name=key)))
        return name

    raw_nodes = doc.get("proxies") or []
    if not isinstance(raw_nodes, list):
        raise VPNError("订阅的 proxies 字段无效")
    inline = [n for raw in raw_nodes if (n := add(raw))]
    raw_providers = doc.get("proxy-providers") or {}
    if not isinstance(raw_providers, dict) or len(raw_providers) > 32:
        raise VPNError("节点提供者格式无效或数量过多")
    for k, spec in raw_providers.items():
        try:
            if not isinstance(spec, dict):
                raise VPNError("提供者格式无效")
            if spec.get("type") == "inline":
                items = spec.get("payload")
            elif spec.get("type") == "http":
                items = parse_yaml(fetch(url(spec.get("url"), https=True))[0]).get("proxies")
            else:
                raise VPNError("不支持本地文件型提供者")
            if not isinstance(items, list):
                raise VPNError("提供者未返回节点列表")
            providers[k] = [n for raw in items if (n := add(raw))]
        except VPNError as e:
            log(f"{vendor.name} / {label(k)}：节点提供者更新失败：{e}")

    raw_groups = doc.get("proxy-groups") or []
    if not isinstance(raw_groups, list) or len(raw_groups) > 256:
        raise VPNError("代理组格式无效或数量过多")
    specs = {g["name"]: g for g in raw_groups if isinstance(g, dict)
             and isinstance(g.get("name"), str) and g["name"] not in nodes}

    def values(spec, key):
        seq = spec.get(key, [])
        return [v for v in seq if isinstance(v, str)] if isinstance(seq, list) else []

    work = 0

    def flatten(name, stack=()):
        nonlocal work
        if name in nodes:
            return [name]
        if name in stack or len(stack) >= 20 or name not in specs:
            return []
        if name in memo:
            return memo[name]
        stack += (name,)
        if len(values(specs[name], "proxies")) > 6000:
            raise VPNError("单个代理组包含过多项目")
        spec = specs[name]
        result = []
        for item in dict.fromkeys(values(spec, "proxies")):
            child = flatten(item, stack)
            work += 1 + len(child)
            if work > 3_000_000:
                raise VPNError("订阅的代理组引用过于复杂")
            result.extend(child)
        for source in dict.fromkeys(values(spec, "use")):
            result.extend(providers.get(source, []))
        if spec.get("include-all") or spec.get("include-all-proxies"):
            result.extend(inline)
        if spec.get("include-all") or spec.get("include-all-providers"):
            result.extend(n for ns in providers.values() for n in ns)
        result = list(dict.fromkeys(result))
        # Vendor group filtering is left to Mihomo-compatible re2 semantics for
        # ordinary patterns. Limit input and use a regex engine with a deadline.
        import regex
        for field, include in (("filter", True), ("exclude-filter", False)):
            pattern = spec.get(field)
            if isinstance(pattern, str) and pattern:
                try:
                    matcher = regex.compile(pattern[:1024], regex.VERSION1)
                    deadline = time.monotonic() + 0.15
                    result = [n for n in result if bool(matcher.search(
                        n[:512], timeout=max(0.00001, deadline-time.monotonic()))) == include]
                except (regex.error, TimeoutError):
                    log(f"{vendor.name} / {label(name)}：代理组筛选表达式无效或超时")
                    return []
        memo[name] = result
        return result

    for name in specs:
        memo = {}
        groups[name] = [nodes[n] for n in flatten(name)]
    if not nodes:
        raise VPNError("未找到可用的代理节点")
    if not groups:
        groups["全部节点"] = list(nodes.values())
    return Profile(vendor, nodes, groups, quota)


def fetch_profile(vendor, log, fetch=request_data):
    data, headers, _ = fetch(vendor.url)
    return parse_profile(vendor, parse_yaml(data), remaining(headers.get("subscription-userinfo", "")),
                         log, fetch)


REGIONS = {
    "香港": ("香港", "hong kong", "hongkong", "hk", "🇭🇰"),
    "台湾": ("台湾", "臺灣", "台灣", "taiwan", "tw", "🇹🇼"),
    "日本": ("日本", "东京", "東京", "大阪", "japan", "tokyo", "jp", "🇯🇵"),
    "新加坡": ("新加坡", "狮城", "singapore", "sg", "🇸🇬"),
    "美国": ("美国", "美國", "洛杉矶", "西雅图", "纽约", "united states", "usa", "us", "🇺🇸"),
    "韩国": ("韩国", "韓國", "首尔", "korea", "kr", "🇰🇷"),
    "英国": ("英国", "英國", "伦敦", "uk", "gb", "🇬🇧"),
    "德国": ("德国", "德國", "法兰克福", "de", "🇩🇪"),
}


def region_rank(name, priorities):
    folded = name.casefold()
    for i, region in enumerate(priorities):
        match = next((aliases for aliases in REGIONS.values() if region.casefold() in aliases), None)
        for term in match or (region.casefold(),):
            if term.isascii() and len(term) <= 3:
                found = re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", folded)
            else:
                found = term in folded
            if found:
                return i
    return len(priorities)
