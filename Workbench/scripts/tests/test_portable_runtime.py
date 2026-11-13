"""Regression checks for moving the application and changing external paths."""
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import portable_runtime as runtime


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "moved application"
        self.root.mkdir()
        self.cfg = {
            "qgis_root": str(self.root / "GIS installation"),
            "plugin_root": str(self.root / "plugins/QSWATPlus"),
            "support_root": str(self.root / "support"),
            "editor_root": str(self.root / "separate editor"),
            "mpi_dir": str(self.root / "MPI/Bin"),
        }
        cfg = runtime.complete_config(self.cfg)
        paths = [Path(cfg["qgis_root"]) / "bin/python-qgis-ltr.bat",
                 Path(cfg["plugin_root"]) / "QSWATPlus/QSWATPlusMain.py",
                 Path(cfg["mpi_dir"]) / "mpiexec.exe"]
        paths.extend(Path(cfg["support_root"]) / name for name in runtime.REQUIRED_SUPPORT_FILES)
        paths.extend(Path(cfg[key]) for key in ("editor_executable", "editor_api", "model_executable"))
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        self.metadata = Path(cfg["plugin_root"]) / "metadata.txt"
        self.metadata.write_text("[general]\nversion=4.0.4\n", encoding="utf-8")

    def test_custom_installation_and_separate_editor(self):
        self.assertEqual(runtime.validate(self.cfg, root=self.root), [])
        cfg = runtime.complete_config(self.cfg)
        self.assertTrue(Path(cfg["editor_api"]).is_relative_to(Path(self.cfg["editor_root"])))
        self.assertEqual(cfg["support_entry"], cfg["support_root"])

    def test_saved_valid_choices_are_preferred(self):
        with patch.object(runtime, "installed_locations", return_value=[]):
            cfg = runtime.discover(self.cfg, self.root)
        for key in self.cfg:
            self.assertEqual(Path(cfg[key]), Path(self.cfg[key]))

    def test_wrong_plugin_version_is_reported(self):
        self.metadata.write_text("[general]\nversion=3.0\n", encoding="utf-8")
        self.assertTrue(any("4.0.4" in e for e in runtime.validate(self.cfg)))

    def test_missing_database_is_reported(self):
        (Path(self.cfg["support_root"]) / runtime.REQUIRED_SUPPORT_FILES[0]).unlink()
        self.assertTrue(any("QSWATPlusProj.sqlite" in e for e in runtime.validate(self.cfg)))

    def test_save_and_reload(self):
        target = self.root / "config/qgis344_runtime.json"
        saved = runtime.save(self.cfg, target)
        self.assertEqual(runtime.read_config(target), saved)
        self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_missing_configuration_can_start_setup(self):
        self.assertEqual(runtime.read_config(self.root / "missing.json"), {})
        self.assertEqual(len(runtime.validate({})), 4)

    def test_probe_checks_actual_qgis_version(self):
        with patch.object(runtime.subprocess, "run") as run:
            run.return_value.returncode = 2
            run.return_value.stdout = b"3.40.0"
            errors = runtime.validate(self.cfg, probe=True)
        self.assertTrue(any("3.40.0" in e for e in errors))
        self.assertNotIn("PYTHONHOME", run.call_args.kwargs["env"])

    def test_non_ascii_installation_uses_ascii_qgis_scratch(self):
        with patch.object(runtime.tempfile, "gettempdir", return_value=self.directory.name):
            actual = runtime.qgis_runtime_directory(self.root / "中文目录")
        self.assertTrue(str(actual).isascii())
        self.assertTrue(actual.is_dir())


if __name__ == "__main__":
    unittest.main()
