"""Optional SSH transport for a portable release built on Windows or Linux."""

import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))

from trip.settings import file_settings


TOP_FILES = ("install.sh", "app.py", "orchestrator.py", "coallocated.py", "gateway_accounting.py")
DIRECTORIES = ("combined", "service", "workflow", "trip")
SUFFIXES = {"", ".py", ".md", ".sh", ".conf", ".txt", ".json"}
SSH_RECEIVER = """
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import sys

with tempfile.TemporaryDirectory(prefix='hk-trip-upload-') as temporary:
    root = Path(temporary)
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode='r:gz') as archive:
        for member in archive:
            destination = (root / member.name).resolve()
            destination.relative_to(root)
            if not member.isfile() or not member.name:
                raise ValueError('Invalid release archive')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.extractfile(member).read())
    subprocess.run(['bash', 'install.sh', *sys.argv[1:]], cwd=root, check=True)
"""


def release_files(root=ROOT):
    paths = [root / name for name in TOP_FILES]
    for directory in DIRECTORIES:
        paths.extend((root / directory).rglob("*"))
    for path in sorted(paths, key=lambda entry: entry.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part.startswith(".") or part in {"__pycache__", "wheelhouse", "embedding-model"}
               for part in relative.parts):
            continue
        if (path.is_file() and not path.is_symlink()
                and not path.name.startswith("test_") and path.suffix in SUFFIXES):
            yield relative.as_posix(), path.read_bytes().replace(b"\r\n", b"\n")


def release_id(files):
    version = hashlib.sha256()
    for name, contents in files:
        version.update(name.encode("utf-8") + b"\0" + hashlib.sha256(contents).digest())
    return version.hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="SSH target, or HK_TRIP_SSH_HOST in .env.local")
    parser.add_argument("--config", help="Absolute configuration path on the Linux host")
    args = parser.parse_args()
    settings = ROOT / ".env.local"
    local = file_settings(settings) if settings.exists() else {}
    host = args.host or os.getenv("HK_TRIP_SSH_HOST") or local.get("HK_TRIP_SSH_HOST", "")
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*", host):
        parser.error("Set --host or HK_TRIP_SSH_HOST to an SSH target")
    if args.config and not args.config.startswith("/"):
        parser.error("The Linux configuration path must be absolute")
    files = list(release_files())
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for name, contents in files:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            tar.addfile(member, io.BytesIO(contents))
    install_args = ["--config", args.config] if args.config else []
    remote = " ".join(shlex.quote(value) for value in
                      ("sudo", "-n", "python3", "-c", SSH_RECEIVER, *install_args))
    print(f"Publishing release {release_id(files)}", flush=True)
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=yes",
         "-o", "UpdateHostKeys=no", host, remote], input=archive.getvalue(), check=True)


if __name__ == "__main__":
    main()
