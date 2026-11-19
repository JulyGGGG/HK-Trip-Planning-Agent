"""Install and activate a checkout on a Linux systemd controller."""

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from trip.deploy import release_files, release_id
from trip.settings import DEFAULTS, file_settings


UNITS = ("hk-trip-api.service", "hk-trip-ui.service")
UNIT_DIR = Path("/etc/systemd/system")
DEFAULT_CONFIG = Path("/etc/hk-trip-planner.env")
HEALTH_PATHS = (("API", "/api/health"), ("UI", "/_stcore/health"))


def settings(path):
    values = dict(DEFAULTS)
    if path:
        overrides = file_settings(path)
        values.update({key: value for key, value in overrides.items() if key in DEFAULTS})
    for key in ("HK_TRIP_INSTALL_ROOT", "TACC_COURSE_ROOT", "TACC_GATEWAY_USERDIR",
                "STORM_WHEELHOUSE", "STORM_EMBEDDING_SOURCE"):
        value = values[key]
        if not value and key in {"TACC_GATEWAY_USERDIR", "STORM_WHEELHOUSE", "STORM_EMBEDDING_SOURCE"}:
            continue
        if not value.startswith("/") or ".." in value.split("/") or value.strip("/") == "":
            raise ValueError(f"{key} must be an absolute directory path")
        values[key] = value.rstrip("/")
    for key in ("HK_TRIP_API_PORT", "HK_TRIP_UI_PORT", "TACC_GATEWAY_PORT"):
        if not re.fullmatch(r"[0-9]+", values[key]) or not 1 <= int(values[key]) <= 65535:
            raise ValueError(f"{key} must be a TCP port between 1 and 65535")
        values[key] = str(int(values[key]))
    if values["HK_TRIP_API_PORT"] == values["HK_TRIP_UI_PORT"]:
        raise ValueError("The API and UI must use different ports")
    for key in ("HK_TRIP_API_BIND", "HK_TRIP_UI_BIND"):
        if values[key] not in {"127.0.0.1", "0.0.0.0"}:
            raise ValueError(f"{key} must be 127.0.0.1 or 0.0.0.0")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", values["TACC_GATEWAY_HOST"]):
        raise ValueError("TACC_GATEWAY_HOST must be a hostname or an IPv4 address")
    if values["TACC_GPU_EXCLUDE"] and not re.fullmatch(
            r"[A-Za-z0-9-]+(?:,[A-Za-z0-9-]+)*", values["TACC_GPU_EXCLUDE"]):
        raise ValueError("TACC_GPU_EXCLUDE must be a comma-separated Slurm node list")
    binary = shutil.which(values["TCLOUD_BIN"])
    if not binary:
        raise ValueError("Install tcloud on PATH or set TCLOUD_BIN to its absolute executable path")
    values["TCLOUD_BIN"] = str(Path(binary).absolute())
    return values


def systemd_value(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + escaped + '"'


def unit_contents(values, root, python):
    common = (
        "[Service]\nType=simple\nUMask=0077\n"
        f"User={values['HK_TRIP_SERVICE_USER']}\n"
        f"WorkingDirectory={systemd_value(root / 'current')}\n"
        "Restart=on-failure\nRestartSec=3\n"
    )
    shared_env = {"HK_TRIP_API_PORT": values["HK_TRIP_API_PORT"], "PYTHONUNBUFFERED": "1"}
    api_env = dict(shared_env, **{
        key: value for key, value in values.items()
        if key.startswith(("TACC_", "STORM_")) or key in {"TCLOUD_BIN", "HK_TRIP_API_BIND"}
    }, TACC_PROJECT_SESSION="hk-trip-planner", TACC_PROJECT_RUNS="hk-trip-planner/runs")
    ui_env = dict(shared_env, PYTHONPATH=str(root / "current"),
                  STREAMLIT_SERVER_ADDRESS=values["HK_TRIP_UI_BIND"],
                  STREAMLIT_SERVER_PORT=values["HK_TRIP_UI_PORT"])
    install = "\n[Install]\nWantedBy=multi-user.target\n"

    def environment(entries):
        return "".join(f"Environment={systemd_value(key + '=' + value)}\n"
                       for key, value in entries.items())

    return {
        UNITS[0]: ("[Unit]\nDescription=Hong Kong trip planner API and TACC controller\n"
                   "After=network-online.target\nWants=network-online.target\n\n"
                   + common + environment(api_env)
                   + f"ExecStart={systemd_value(python)} -B -m trip.controller "
                     f"--data-dir {systemd_value(root / 'data')}\n" + install),
        UNITS[1]: ("[Unit]\nDescription=Hong Kong trip planner web interface\n"
                   "After=hk-trip-api.service\nWants=hk-trip-api.service\n\n"
                   + common + environment(ui_env)
                   + f"ExecStart={systemd_value(python)} -B -m streamlit run trip/ui.py "
                     "--server.headless=true --browser.gatherUsageStats=false "
                     "--client.toolbarMode=minimal --theme.primaryColor=#15706d\n" + install),
    }


def write_bytes(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            temporary.chmod(0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_release(directory, files):
    if directory.exists():
        if any(not (directory / name).is_file() or (directory / name).read_bytes() != contents
               for name, contents in files):
            raise ValueError(f"Existing release content differs at {directory}")
        return
    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        source = Path(temporary) / "release"
        source.mkdir(mode=0o755)
        for name, contents in files:
            file = source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(contents)
        (source / "install.sh").chmod(0o755)
        source.rename(directory)


def prepare_venv(root, release, index_url):
    requirements = release / "trip" / "requirements-control.txt"
    version = hashlib.sha256(requirements.read_bytes()).hexdigest()[:12]
    runtime = root / "venvs" / f"py{sys.version_info.major}.{sys.version_info.minor}-{version}"
    python = runtime / "bin" / "python"
    ready = runtime / ".ready"
    if not ready.is_file() or not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "--index-url", index_url, "--timeout", "20", "--retries", "2",
                        "-r", str(requirements)], check=True)
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        ready.touch()
    return python


def preflight(values, release, python, runtime_dirs):
    account = pwd.getpwnam(values["HK_TRIP_SERVICE_USER"])
    for directory in runtime_dirs:
        if not directory.exists():
            directory.mkdir(mode=0o700)
            os.chown(directory, account.pw_uid, account.pw_gid)
    script = """
import os
from pathlib import Path
import sys
from paramiko import HostKeys
from trip.controller import TripWorker
from trip.settings import gateway_host_key_name, tcloud_identity
from trip.ui import form_fields

if not os.access(os.environ["TCLOUD_BIN"], os.X_OK):
    sys.exit("The service user cannot execute TCLOUD_BIN")
if not os.access(tcloud_identity().key, os.R_OK):
    sys.exit("The service user cannot read its configured tcloud key")
known_hosts = Path.home() / ".ssh" / "known_hosts"
if not known_hosts.is_file() or not HostKeys(str(known_hosts)).lookup(gateway_host_key_name()):
    sys.exit("Add the verified TACC gateway key to the service user's known_hosts")
for path in sys.argv[1:]:
    if not Path(path).is_dir() or not os.access(path, os.W_OK | os.X_OK):
        sys.exit("The service user cannot write to " + path)
"""
    env = [f"{key}={values[key]}" for key in ("TCLOUD_BIN", "TACC_GATEWAY_HOST", "TACC_GATEWAY_PORT")]
    subprocess.run(["runuser", "-u", account.pw_name, "--", "env", *env,
                    str(python), "-B", "-c", script, *map(str, runtime_dirs)],
                   cwd=release, check=True)


def service_call(*args, check=True, quiet=False):
    return subprocess.run(["systemctl", *args], check=check, timeout=35,
                          stdout=subprocess.DEVNULL if quiet else None,
                          stderr=subprocess.DEVNULL if quiet else None)


def wait_for_health(values):
    http = build_opener(ProxyHandler({}))
    for _ in range(15):
        try:
            for name, path in HEALTH_PATHS:
                url = "http://127.0.0.1:" + values[f"HK_TRIP_{name}_PORT"] + path
                with http.open(url, timeout=2) as response:
                    if response.status != 200:
                        raise OSError(f"HTTP {response.status} from {path}")
            for name in UNITS:
                service_call("is-active", "--quiet", name)
            return
        except (OSError, URLError, subprocess.CalledProcessError):
            time.sleep(1)
    raise RuntimeError("API and UI did not pass their health checks")


def activate(root, release, units, values):
    current = root / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError(f"{current} must be a symbolic link")
    previous = os.readlink(current) if current.is_symlink() else None
    saved = {name: (UNIT_DIR / name).read_bytes() if (UNIT_DIR / name).exists() else None
             for name in UNITS}
    active = [name for name in UNITS if service_call("is-active", "--quiet", name, check=False).returncode == 0]
    enabled = [name for name in UNITS if service_call("is-enabled", "--quiet", name,
                                                      check=False, quiet=True).returncode == 0]

    def switch(target):
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            link = Path(temporary) / "current"
            link.symlink_to(target)
            link.replace(current)

    try:
        for name, body in units.items():
            write_bytes(UNIT_DIR / name, body.encode("utf-8"))
        service_call("daemon-reload")
        switch(release)
        service_call("restart", *UNITS)
        wait_for_health(values)
        service_call("enable", *UNITS)
    except (Exception, KeyboardInterrupt):
        service_call("stop", *UNITS, check=False)
        added = [name for name in UNITS if name not in enabled]
        if added:
            service_call("disable", *added, check=False)
        for name, body in saved.items():
            path = UNIT_DIR / name
            if body is None:
                path.unlink(missing_ok=True)
            else:
                write_bytes(path, body)
        if previous is None:
            current.unlink(missing_ok=True)
        else:
            switch(previous)
        service_call("daemon-reload")
        if active:
            service_call("start", *active)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Deployment settings file")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run the installer with sudo")
    local = Path(".env.local")
    path = args.config
    if path is None:
        path = local if local.is_file() else (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None)
    try:
        values = settings(path)
        files = list(release_files())
        version = release_id(files)
        root = Path(values["HK_TRIP_INSTALL_ROOT"]).resolve()
        os.umask(0o022)
        for directory in (root / "releases", root / "venvs"):
            directory.mkdir(parents=True, exist_ok=True)
        with (root / "release.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = root / "releases" / version
            write_release(release, files)
            python = prepare_venv(root, release, values["HK_TRIP_PIP_INDEX_URL"])
            preflight(values, release, python, (root / "data", root / "staging"))
            activate(root, release, unit_contents(values, root, python), values)
        print(f"Active release {version} in {root}")
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Installation failed: {exc}\n")


if __name__ == "__main__":
    main()
