import base64
import hashlib
import json
import winreg
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pywintypes

from app_info import APP_VERSION, EXE_NAME
from automation import Engine
from catalog import CatalogSource, VPNError, load_catalog_source
import updates


class Updates(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = Ed25519PrivateKey.generate()

    def package(self, path, extra=None):
        with ZipFile(path, "w") as bundle:
            for name in (EXE_NAME, "clash/verge-mihomo.exe", "7zip/7z.exe", "7zip/7z.dll"):
                bundle.writestr(f"VPN-X/{name}", b"test content")
            if extra:
                bundle.writestr(extra, b"test content")
        data = path.read_bytes()
        record = dict(version="99.1", url="https://example.test/update.zip",
                      size=len(data), sha256=hashlib.sha256(data).hexdigest())
        record["signature"] = base64.b64encode(self.key.sign(updates.signing_bytes(record))).decode()
        return record

    def test_legacy_sources_load_and_update_url_is_https(self):
        path = self.root / "catalog-source.json"
        path.write_text('{"url":"","key":""}')
        self.assertEqual(CatalogSource.load(path).update_url, "")
        with self.assertRaises(VPNError):
            CatalogSource(update_url="http://example.test/update.json")
        CatalogSource(update_url="https://example.test/update.json").save(path)
        self.assertEqual(CatalogSource.load(path).update_url, "https://example.test/update.json")

    def test_installer_defaults_fill_missing_fields_without_overwriting_user_source(self):
        profile = self.root / "user" / "catalog-source.json"
        package = self.root / "install" / "catalog-source.json"
        defaults = CatalogSource("https://installer.example/catalog.vxp", "installer-key",
                                 "https://installer.example/update.json")
        defaults.save(package)
        self.assertEqual(load_catalog_source(profile, package), defaults)
        local_subscription = CatalogSource("https://user.example/catalog.vxp", "user-key-0000")
        local_subscription.save(profile)
        expected = CatalogSource(local_subscription.url, local_subscription.key, defaults.update_url)
        self.assertEqual(load_catalog_source(profile, package), expected)
        self.assertEqual(CatalogSource.load(profile), expected)
        update_override = "https://user.example/update.json"
        CatalogSource(update_url=update_override).save(profile)
        self.assertEqual(load_catalog_source(profile, package), CatalogSource(defaults.url, defaults.key, update_override))
        custom = CatalogSource(local_subscription.url, local_subscription.key, update_override)
        custom.save(profile)
        self.assertEqual(load_catalog_source(profile, package), custom)

    def test_only_two_retries(self):
        engine = Engine(lambda *_: None, data_dir=self.root)
        operation = Mock(side_effect=VPNError("network"))
        with self.assertRaises(VPNError):
            engine.retry(operation, "test")
        self.assertEqual([c.args for c in operation.call_args_list], [(0,), (1,), (2,)])

    def test_manifest_rejects_tampering_and_zip_rejects_dangerous_entries(self):
        path = self.root / "test.zip"
        record = self.package(path)
        key = self.key.public_key().public_bytes_raw()
        parsed = updates.verify_manifest(json.dumps(record), key)
        self.assertTrue(updates.package_matches(path, parsed))
        with self.assertRaises(VPNError):
            updates.verify_manifest(json.dumps(dict(record, version="99.2")), key)
        for extra in ("VPN-X/../escape", "VPN-X/_internal/qt.dll:ads", "VPN-X/NUL.txt",
                      "VPN-X/_internal/evil.exe.", "VPN-X/vpn-x.exe", "VPN-X/settings.json"):
            with self.subTest(extra=extra), self.assertRaises(VPNError):
                self.package(path, extra)
                updates.archive_files(path)

    def test_download_verified_once_and_reused(self):
        test_zip = self.root / "incoming.zip"
        manifest = self.package(test_zip)
        manifest_url = "https://example.test/manifest.json"
        requested = []
        def download(address, out, maximum, progress, cancelled):
            requested.append(address)
            out.write(json.dumps(manifest).encode() if address == manifest_url else test_zip.read_bytes())
        with patch.object(updates, "_download", download), patch.object(updates, "trust_key", self.key.public_key().public_bytes_raw):
            for _ in range(2):
                self.assertEqual(updates.check(self.root, manifest_url, lambda *_: None).version, "99.1")
        self.assertEqual(requested, [manifest_url, manifest["url"], manifest_url])

    def test_windows_app_version_preserves_the_localized_name(self):
        (self.root / "current-version.txt").write_text("99.1\n", encoding="ascii")
        for before, after in (("VPN-X 版本 1.4", "VPN-X 版本 99.1"),
                              ("VPN-X version 1.4", "VPN-X version 99.1"),
                              ("VPN-X V1.4", "VPN-X V99.1"),
                              ("VPN-X", "VPN-X")):
            with self.subTest(before=before):
                values = {"InstallLocation": str(self.root) + "\\",
                          "DisplayName": before, "DisplayVersion": "1.4"}

                def set_value(key, name, reserved, value_type, value):
                    values[name] = value

                with (patch.object(winreg, "OpenKey") as open_key,
                      patch.object(winreg, "QueryValueEx", side_effect=lambda _, name: (values[name], winreg.REG_SZ)),
                      patch.object(winreg, "SetValueEx", side_effect=set_value) as write):
                    updates.sync_windows_app_version(self.root, "1.4")
                    open_key.assert_not_called()
                    updates.sync_windows_app_version(self.root, "99.1")
                    open_key.assert_called_once_with(winreg.HKEY_LOCAL_MACHINE, updates.UNINSTALL_KEY, 0,
                                                     winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY)
                    self.assertEqual((values["DisplayName"], values["DisplayVersion"]), (after, "99.1"))
                    write.reset_mock()
                    updates.sync_windows_app_version(self.root, "99.1")
                    write.assert_not_called()
                    values["InstallLocation"] = str(self.root / "another installation")
                    with self.assertRaises(VPNError):
                        updates.sync_windows_app_version(self.root, "99.1")
                    write.assert_not_called()

    def test_switch_commits_on_healthy_start_and_keeps_old_on_failure(self):
        for healthy, registry_error in ((False, False), (True, False), (True, True)):
            with self.subTest(healthy=healthy, registry_error=registry_error), TemporaryDirectory() as tmp:
                root = Path(tmp)
                cache = root / "updates"
                cache.mkdir()
                old = updates.version_exe(root, APP_VERSION)
                old.parent.mkdir(parents=True)
                old.write_bytes(b"old runtime")
                (root / "current-version.txt").write_text(f"{APP_VERSION}\n")
                archive = cache / "update.zip"
                manifest = self.package(archive)
                (cache / "manifest.json").write_text(json.dumps(manifest))
                starts = []
                def extract(path, tool, staging):
                    with ZipFile(path) as source:
                        source.extractall(staging)
                    return staging / "VPN-X"
                def launch(args, **kwargs):
                    starts.append(args)
                    self.assertEqual(updates.current_version(root), APP_VERSION)
                    child = Mock()
                    child.poll.return_value = None if healthy else 1
                    if healthy and args[1] == "--update-health":
                        (cache / f"health-{args[2]}").write_bytes(b"ready")
                    return child

                def sync_version(install_root, release):
                    self.assertEqual(updates.current_version(install_root), release)
                    if registry_error:
                        raise PermissionError("Registry is read-only")

                with (patch.object(updates, "trust_key", self.key.public_key().public_bytes_raw),
                      patch.object(updates, "extract_package", extract),
                      patch.object(updates.win32api, "OpenProcess", side_effect=pywintypes.error(87, "OpenProcess", "exited")),
                      patch.object(updates, "check_runtime"), patch.object(subprocess, "Popen", launch),
                      patch.object(updates, "sync_windows_app_version", side_effect=sync_version) as sync):
                    self.assertEqual(updates.apply_pending(root, 98765), 0 if healthy else 1)
                self.assertEqual(updates.current_version(root), "99.1" if healthy else APP_VERSION)
                self.assertEqual(old.read_bytes(), b"old runtime")
                if healthy:
                    sync.assert_called_once_with(root, "99.1")
                    self.assertEqual(len(starts), 1)
                else:
                    sync.assert_not_called()
                    self.assertEqual(starts[-1], [str(old), "--update-failed"])
