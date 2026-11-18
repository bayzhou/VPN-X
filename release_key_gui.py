"""Local-only password prompt for creating and using the release key."""
from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sign_update import signed_manifest
from catalog import atomic


def prompt(label):
    value, accepted = QInputDialog.getText(None, "VPN-X 更新签名", label, QLineEdit.EchoMode.Password)
    return value.encode() if accepted else None


def main():
    app = QApplication(sys.argv)
    mode, folder, *extra = sys.argv[1:]
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    private = root / "vpn-x-signing.pem"
    public = root / "update-public-key.bin"
    if mode == "sign":
        archive, address, output, release = extra
    while True:
        password = prompt("为签名私钥设置密码（至少 12 个字符）" if mode == "keygen" else f"输入签名私钥密码，签署 V{release} 更新清单")
        if password is None:
            return 1
        if mode == "keygen":
            if len(password.decode()) < 12:
                QMessageBox.warning(None, "密码过短", "请输入至少 12 个字符。")
                continue
            repeated = prompt("再输一次密码以确认")
            if repeated is None:
                return 1
            if repeated != password:
                QMessageBox.warning(None, "密码不一致", "请重新设置密码。")
                continue
            key = Ed25519PrivateKey.generate()
            with private.open("xb") as stream:
                stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.BestAvailableEncryption(password)))
            atomic(public, key.public_key().public_bytes_raw())
            return 0
        try:
            key = serialization.load_pem_private_key(private.read_bytes(), password=password)
        except (ValueError, TypeError):
            QMessageBox.warning(None, "密码不正确", "请重试或取消。")
            continue
        if not isinstance(key, Ed25519PrivateKey) or key.public_key().public_bytes_raw() != public.read_bytes():
            QMessageBox.critical(None, "无法签名", "私钥和公钥不匹配。")
            return 1
        atomic(Path(output), signed_manifest(key, Path(archive), address, release))
        return 0


if __name__ == "__main__":
    sys.exit(main())
