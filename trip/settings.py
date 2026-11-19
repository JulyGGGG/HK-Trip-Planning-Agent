"""Deployment defaults and the identity shared with the course SDK."""

import json
import os
from pathlib import Path
import re
from typing import NamedTuple


DEFAULTS = {
    "HK_TRIP_INSTALL_ROOT": "/opt/hk-trip-planner",
    "HK_TRIP_SERVICE_USER": "root",
    "HK_TRIP_API_BIND": "0.0.0.0",
    "HK_TRIP_API_PORT": "8766",
    "HK_TRIP_UI_BIND": "0.0.0.0",
    "HK_TRIP_UI_PORT": "8501",
    "TCLOUD_BIN": "tcloud",
    "HK_TRIP_PIP_INDEX_URL": "https://pypi.org/simple",
    "TACC_GATEWAY_HOST": "gw.turing.ust.hk",
    "TACC_GATEWAY_PORT": "30041",
    "TACC_COURSE_ROOT": "/mnt/home/public/course-envs/CSIT6000V",
    "TACC_GATEWAY_USERDIR": "",
    "TACC_GPU_EXCLUDE": "",
    "STORM_WHEELHOUSE": "",
    "STORM_EMBEDDING_SOURCE": "",
}


class TcloudIdentity(NamedTuple):
    username: str
    key: Path


def tcloud_identity():
    home = Path.home()
    try:
        config = json.loads((home / ".tcloud" / ".userconfig").read_text(encoding="utf-8"))
        values = {key.lower(): value for key, value in config.items()}
        username = values.get("username", "")
        key = values.get("authfile") or str(home / ".ssh" / "id_rsa")
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", username):
            raise ValueError
        if not Path(key).is_absolute():
            raise ValueError
        return TcloudIdentity(username, Path(key))
    except (AttributeError, OSError, TypeError, ValueError):
        raise ValueError("Configure the service account's tcloud username and absolute key path") from None


def gateway_address():
    return (os.getenv("TACC_GATEWAY_HOST", DEFAULTS["TACC_GATEWAY_HOST"]),
            int(os.getenv("TACC_GATEWAY_PORT", DEFAULTS["TACC_GATEWAY_PORT"])))


def gateway_host_key_name():
    host, port = gateway_address()
    return host if port == 22 else "[{}]:{}".format(host, port)


def file_settings(path):
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in set(DEFAULTS) | {"HK_TRIP_SSH_HOST"}:
            raise ValueError("Unknown deployment setting on line {}".format(number))
        if key in values:
            raise ValueError("Duplicate deployment setting {}".format(key))
        values[key] = value.strip()
    return values
