"""Offline release tool. Keep the signing key outside the source tree and installers."""
import argparse
import base64
from getpass import getpass
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app_info import APP_VERSION
from catalog import atomic, url
from updates import MAX_PACKAGE, archive_files, signing_bytes, version


def signed_manifest(key, archive, address, release):
    archive_files(archive)
    size = archive.stat().st_size
    if not 1 <= size <= MAX_PACKAGE:
        raise ValueError("Update archive exceeds the download limit")
    version(release)
    with archive.open("rb") as stream:
        record = dict(version=release, url=url(address, https=True), size=size,
                      sha256=hashlib.file_digest(stream, "sha256").hexdigest())
    record["signature"] = base64.b64encode(key.sign(signing_bytes(record))).decode("ascii")
    return (json.dumps(record, indent=2) + "\n").encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("keygen")
    generate.add_argument("private_key", type=Path)
    generate.add_argument("public_key", type=Path)
    sign = commands.add_parser("sign")
    sign.add_argument("private_key", type=Path)
    sign.add_argument("archive", type=Path)
    sign.add_argument("url")
    sign.add_argument("output", type=Path)
    sign.add_argument("--version", default=APP_VERSION)
    args = parser.parse_args()
    password = getpass("Signing-key password: ").encode()
    if args.command == "keygen":
        if len(password) < 12 or password != getpass("Repeat password: ").encode():
            parser.error("Passwords must match and contain at least 12 characters")
        if args.private_key.exists() or args.public_key.exists() or args.private_key.resolve() == args.public_key.resolve():
            parser.error("Choose two new, distinct output paths")
        key = Ed25519PrivateKey.generate()
        with args.private_key.open("xb") as stream:
            stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.BestAvailableEncryption(password)))
        with args.public_key.open("xb") as stream:
            stream.write(key.public_key().public_bytes_raw())
    else:
        key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=password)
        if not isinstance(key, Ed25519PrivateKey):
            parser.error("Expected an Ed25519 key")
        atomic(args.output, signed_manifest(key, args.archive, args.url, args.version))


if __name__ == "__main__":
    main()
