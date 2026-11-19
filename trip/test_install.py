import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from trip.deploy import release_files, release_id
from trip.settings import DEFAULTS

if os.name == "posix":
    from trip import install


class ReleaseCase(unittest.TestCase):
    def test_package_excludes_local_files_and_normalizes_line_endings(self):
        parent = Path(__file__).resolve().parents[1] / "evidence" / "test-tmp"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            root = Path(temporary)
            (root / "trip").mkdir()
            (root / "trip" / ".env.local").write_text("local config")
            (root / "trip" / "test_settings.py").write_text("test code")
            (root / "trip" / "a.py").write_bytes(b"print(1)\r\n")
            (root / "trip" / "Z.py").write_bytes(b"print(2)\n")
            files = list(release_files(root))
            self.assertEqual(files, [("trip/Z.py", b"print(2)\n"), ("trip/a.py", b"print(1)\n")])
            (root / "trip" / "a.py").write_bytes(b"print(1)\n")
            self.assertEqual(release_id(files), release_id(release_files(root)))


@unittest.skipUnless(os.name == "posix", "Linux systemd installer")
class InstallerCase(unittest.TestCase):
    def test_custom_ports_and_install_directory_are_shared_by_services(self):
        values = dict(DEFAULTS, HK_TRIP_API_PORT="18766", HK_TRIP_UI_PORT="18501")
        root = Path("/srv/trip demo")
        api, ui = install.unit_contents(values, root, root / "venvs/bin/python").values()
        self.assertIn("WorkingDirectory=/srv/trip demo/current\n", api)
        self.assertIn('--data-dir "/srv/trip demo/data"', api)
        for unit in (api, ui):
            self.assertIn('Environment="HK_TRIP_API_PORT=18766"', unit)
        self.assertIn('Environment="STREAMLIT_SERVER_PORT=18501"', ui)

    def test_failed_health_check_restores_prior_release_and_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit_dir = root / "units"
            unit_dir.mkdir()
            current = root / "current"
            current.symlink_to("old-release")
            for name in install.UNITS:
                (unit_dir / name).write_text("old " + name)
            with patch.object(install, "UNIT_DIR", unit_dir), \
                    patch.object(install, "service_call", return_value=subprocess.CompletedProcess([], 0)) as systemctl, \
                    patch.object(install, "wait_for_health", side_effect=RuntimeError("failed health")):
                with self.assertRaisesRegex(RuntimeError, "failed health"):
                    install.activate(root, root / "new-release", dict.fromkeys(install.UNITS, "new unit"), DEFAULTS)
            self.assertEqual(os.readlink(current), "old-release")
            for name in install.UNITS:
                self.assertEqual((unit_dir / name).read_text(), "old " + name)
            systemctl.assert_called_with("start", *install.UNITS)
