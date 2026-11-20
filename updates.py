"""Signed Windows updates. Keep the previous version until the new one is healthy."""
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import winreg
from zipfile import BadZipFile, ZipFile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pywintypes
import requests
import win32api
import win32event

from app_info import APP_VERSION, EXE_NAME
from catalog import VPNError, atomic, url

NO_WINDOW = 0x08000000
MAX_PACKAGE = 768 * 1024**2
# The 64-bit Inno Setup uninstall entry for the AppId in installer.iss.
UNINSTALL_KEY = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{FA63F85D-822B-4A4F-B2D1-5817445DFC1C}_is1")


def version(text):
    if not isinstance(text, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})", text):
        raise VPNError("更新版本号格式无效")
    return tuple(map(int, text.split(".")))


def installed_root(exe=None):
    if exe is None and not getattr(sys, "frozen", False):
        return None
    folder = Path(exe or sys.executable).resolve().parent
    if folder.parent.name == "versions" and (folder.parents[1] / "vpn-x-installed.txt").is_file():
        root = folder.parents[1]
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VPN-X", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                registered, _ = winreg.QueryValueEx(key, "InstallDir")
        except OSError:
            return None
        if root == Path(registered).resolve():
            version(folder.name)
            return root
    return None


def current_version(root):
    value = (root / "current-version.txt").read_text(encoding="ascii").strip()
    version(value)
    return value


def version_exe(root, value):
    version(value)
    return root / "versions" / value / EXE_NAME


def sync_windows_app_version(root, release):
    if current_version(root) != release:
        return
    access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0, access) as key:
        location, _ = winreg.QueryValueEx(key, "InstallLocation")
        if not isinstance(location, str) or Path(location) != root:
            raise VPNError("Windows 已安装的应用记录与当前安装目录不一致")
        name, _ = winreg.QueryValueEx(key, "DisplayName")
        registered_version, _ = winreg.QueryValueEx(key, "DisplayVersion")
        if isinstance(name, str):
            updated_name = re.sub(r"(?<=\s)([vV]?)[0-9]{1,4}\.[0-9]{1,4}(?=\s*\Z)", r"\g<1>" + release, name)
            if updated_name != name:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, updated_name)
        if registered_version != release:
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, release)


def signing_bytes(data):
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True)
class Manifest:
    version: str
    size: int
    sha256: str
    url: str


def verify_manifest(raw, public_key):
    try:
        record = json.loads(raw)
        if not isinstance(record, dict) or set(record) != {"version", "size", "sha256", "url", "signature"}:
            raise ValueError
        signature = base64.b64decode(record.pop("signature"), validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signing_bytes(record))
        item = Manifest(**record)
        version(item.version)
        url(item.url, https=True)
        if type(item.size) is not int or not 1 <= item.size <= MAX_PACKAGE:
            raise ValueError
        if not re.fullmatch(r"[a-f0-9]{64}", item.sha256):
            raise ValueError
    except (InvalidSignature, KeyError, TypeError, ValueError, RecursionError) as err:
        raise VPNError("更新清单格式无效或签名校验失败") from err
    return item


def trust_key():
    try:
        key = (Path(sys._MEIPASS) / "update-public-key.bin").read_bytes()
        if len(key) == 32:
            return key
    except (OSError, AttributeError):
        pass
    raise VPNError("此安装包未内置更新公钥，无法验证在线更新")


def package_matches(path, manifest):
    try:
        if path.stat().st_size != manifest.size:
            return False
        with path.open("rb") as src:
            return hashlib.file_digest(src, "sha256").hexdigest() == manifest.sha256
    except OSError:
        return False


def _download(address, out, maximum, progress, cancelled):
    from urllib.parse import urljoin
    started = time.monotonic()
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(6):
            cancelled()
            url(address, https=True)
            with session.get(address, stream=True, allow_redirects=False, timeout=(8, 15),
                             headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"}) as response:
                if response.is_redirect:
                    address = urljoin(address, response.headers["Location"])
                    continue
                if response.status_code != 200:
                    raise VPNError(f"更新服务器返回 HTTP {response.status_code}")
                count = 0
                for chunk in response.iter_content(256 * 1024):
                    cancelled()
                    count += len(chunk)
                    if count > maximum or time.monotonic() - started > 1800:
                        raise VPNError("更新下载超出大小或时间限制")
                    out.write(chunk)
                    progress(count)
                return
    raise VPNError("更新下载重定向次数过多")


def check(root, address, progress, cancelled=lambda: None):
    import io
    key = trust_key()
    # Protected by Program Files permissions on a normal installation. Never execute from a user temp folder.
    cache = root / "updates"
    try:
        contents = io.BytesIO()
        _download(address, contents, 16384, lambda _: None, cancelled)
        raw = contents.getvalue()
        item = verify_manifest(raw, key)
        if version(item.version) <= version(APP_VERSION):
            return None
        with install_lock(root):
            cache.mkdir(exist_ok=True)
            archive = cache / "update.zip"
            if not package_matches(archive, item):
                part = cache / f"{secrets.token_hex(12)}.part"
                try:
                    with part.open("xb") as stream:
                        _download(item.url, stream, item.size, lambda done: progress("下载更新包", done, item.size),
                                  cancelled)
                    if not package_matches(part, item):
                        raise VPNError("更新包 SHA-256 校验失败")
                    archive_files(part)
                    (cache / "manifest.json").unlink(missing_ok=True)
                    os.replace(part, archive)
                finally:
                    part.unlink(missing_ok=True)
            else:
                archive_files(archive)
            atomic(cache / "manifest.json", raw)
        return item
    except (OSError, requests.RequestException, BadZipFile) as err:
        raise VPNError("更新下载失败，请检查网络或安装目录权限") from err


def archive_files(archive):
    files, paths, names = set(), {}, set()
    total = 0
    with ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 5000:
            raise VPNError("更新包文件数量过多")
        for info in bundle.infolist():
            name = info.orig_filename.rstrip("/")
            parts = name.split("/")
            if (len(parts) < 2 or parts[0] != "VPN-X" or len(name) > 185 or
                    any(not re.fullmatch(r"[a-zA-Z0-9_.+ -]{1,100}", p) or p.endswith((" ", "."))
                        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", p)
                        for p in parts) or info.flag_bits & 1 or
                    stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG)
                    or info.external_attr & 0x400):
                raise VPNError("更新包包含不安全的文件路径或类型")
            for idx in range(1, len(parts) + 1):
                path = "/".join(parts[:idx])
                if paths.setdefault(path.lower(), path) != path:
                    raise VPNError("更新包含大小写冲突的文件路径")
            if name in names:
                raise VPNError("更新包含重复路径")
            names.add(name)
            if not info.is_dir():
                files.add(name)
                total += info.file_size
                if total > 1536 * 1024**2:
                    raise VPNError("更新解压大小超过限制")
    required = {f"VPN-X/{p}" for p in (EXE_NAME, "clash/verge-mihomo.exe", "7zip/7z.exe", "7zip/7z.dll")}
    if not required <= files or any(f"VPN-X/{p}" in files for p in ("catalog-source.json", "settings.json")):
        raise VPNError("更新包缺少运行文件或包含用户配置")
    if any("/".join(name.split("/")[:i]) in files for name in paths.values()
           for i in range(1, len(name.split("/")))):
        raise VPNError("更新包含冲突的文件和目录")
    return files


def extract_package(archive, seven_zip, staging):
    wanted = archive_files(archive)
    subprocess.run([str(seven_zip), "x", "-tzip", "-y", f"-o{staging}", "--", str(archive)],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=180, check=True,
                   creationflags=NO_WINDOW, cwd=seven_zip.parent)
    actual = set()
    for file in staging.rglob("*"):
        if file.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise VPNError("更新解压目录包含链接")
        if file.is_file():
            actual.add(file.relative_to(staging).as_posix())
    if actual != wanted:
        raise VPNError("更新包解压内容与清单不一致")
    return staging / "VPN-X"


def check_runtime(content, item, key):
    exe = content / EXE_NAME
    major, minor = version(item.version)
    info = win32api.GetFileVersionInfo(str(exe), "\\")
    if (info["FileVersionMS"] != (major << 16 | minor) or info["FileVersionLS"] != 0 or
            (content / "_internal" / "update-public-key.bin").read_bytes() != key):
        raise VPNError("更新包中的程序版本或更新公钥与清单不一致")
    for flag in ("--check-startup", "--check-clash"):
        subprocess.run([str(exe), flag], check=True, timeout=30, cwd=content)


@contextmanager
def install_lock(root):
    with (root / "update.lock").open("a+b") as stream:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def remove_old_versions(root, release, log):
    """Remove older runtime slots while the installed startup holds the update lock."""
    active = version(release)
    versions = root / "versions"
    if (versions.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or versions.resolve(strict=True) != versions or not version_exe(root, release).is_file()):
        raise VPNError("无法确认安装版本目录")
    complete = True
    for folder in versions.iterdir():
        try:
            if version(folder.name) >= active:
                continue
        except VPNError:
            continue
        try:
            info = folder.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or folder.resolve(strict=True) != folder):
                continue
            shutil.rmtree(folder)
        except FileNotFoundError:
            complete = complete and not folder.exists()
        except OSError:
            complete = False
        else:
            log(f"已清理旧版本 V{folder.name}")
    return complete


def remove_installed_package(root, release, log):
    cache = root / "updates"
    manifest = cache / "manifest.json"
    archive = cache / "update.zip"
    try:
        if cache.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return
    except FileNotFoundError:
        return
    try:
        raw = manifest.read_bytes()
    except FileNotFoundError:
        try:
            archive.unlink()
        except FileNotFoundError:
            pass
        else:
            log("已清理未完成的更新包")
        return
    try:
        item = verify_manifest(raw, trust_key())
    except VPNError:
        return
    if version(item.version) > version(release):
        return
    archive.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    log(f"已清理 V{item.version} 更新包")


def maintain_installation(root, release, log):
    """Wait for the update commit, then maintain the installed version in the background."""
    deadline = time.monotonic() + 30
    synced = versions_cleaned = package_cleaned = False
    while True:
        try:
            with install_lock(root):
                if current_version(root) != release:
                    return
                if not synced:
                    try:
                        sync_windows_app_version(root, release)
                    except (OSError, VPNError):
                        pass
                    else:
                        synced = True
                if not versions_cleaned:
                    versions_cleaned = remove_old_versions(root, release, log)
                if not package_cleaned:
                    remove_installed_package(root, release, log)
                    package_cleaned = True
        except (OSError, VPNError):
            pass
        if synced and versions_cleaned and package_cleaned:
            return
        if time.monotonic() >= deadline:
            if not synced:
                log("未能同步 Windows 已安装的应用版本信息")
            if not versions_cleaned or not package_cleaned:
                log("旧版本或更新包清理未完成，下次启动时重试")
            return
        time.sleep(0.5)


def apply_pending(root, parent):
    old = version_exe(root, APP_VERSION)
    try:
        with install_lock(root):
            # Open the previous GUI before modifying any files; its normal exit released the proxy and lock.
            try:
                process = win32api.OpenProcess(0x00100000, False, parent)
            except pywintypes.error as err:
                if (getattr(err, "winerror", None) or err.args[0]) != 87:  # Process already exited.
                    raise
                process = None
            if process is not None:
                with process:
                    if win32event.WaitForSingleObject(process, 30000) != win32event.WAIT_OBJECT_0:
                        raise VPNError("VPN-X 尚未退出，更新已取消")
            if current_version(root) != APP_VERSION:
                raise VPNError("当前版本已经切换")
            cache = root / "updates"
            key = trust_key()
            item = verify_manifest((cache / "manifest.json").read_bytes(), key)
            archive = cache / "update.zip"
            if version(item.version) <= version(APP_VERSION) or not package_matches(archive, item):
                raise VPNError("更新包版本或完整性检查失败")
            new = version_exe(root, item.version)
            nonce = secrets.token_hex(12)
            staging = cache / ("stage-" + nonce)
            staging.mkdir()
            health = cache / ("health-" + nonce)
            child = None
            try:
                content = extract_package(archive, old.parent / "7zip" / "7z.exe", staging)
                check_runtime(content, item, key)
                # If an earlier attempt got this far, leave its files untouched and install into an empty slot.
                if new.parent.exists():
                    new.parent.rename(cache / ("previous-" + nonce))
                content.rename(new.parent)
                try:
                    child = subprocess.Popen([str(new), "--update-health", nonce], cwd=new.parent)
                    deadline = time.monotonic() + 30
                    while not health.is_file() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.2)
                    if not health.is_file() or child.poll() is not None:
                        raise VPNError("新版本启动检查失败")
                    atomic(root / "current-version.txt", (item.version + "\n").encode("ascii"))
                except Exception:
                    if child and child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)
                    raise
                try:
                    sync_windows_app_version(root, item.version)
                except (OSError, VPNError):
                    # The runtime is committed. The new process can retry the display metadata.
                    pass
            finally:
                try:
                    health.unlink(missing_ok=True)
                except OSError:
                    pass
                shutil.rmtree(staging, ignore_errors=True)
    except Exception:
        # The old runtime is retained and its data was never moved or overwritten.
        subprocess.Popen([str(old), "--update-failed"], cwd=old.parent)
        return 1
    return 0
