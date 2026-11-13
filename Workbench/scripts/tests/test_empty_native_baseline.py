"""A native baseline with zero parameter edits still completes its output checks."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_calibration
import swatplus_trial


class EmptyNativeBaselineTests(unittest.TestCase):
    def test_empty_parameter_list_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'inputs').mkdir()
            executable = root / 'model.exe'
            executable.write_bytes(b'fixture')
            spec = {'root': str(root), 'input_dir': 'inputs', 'executable': str(executable),
                    'work_dir': 'trial', 'expected_version': '62.0.1', 'frequency': 'monthly',
                    'start_year': 1990, 'end_year': 1991, 'warmup_years': 1,
                    'outlet': {'unit': 1, 'gis_id': 1}, 'parameter_backend': 'native_calibration',
                    'parameters': []}
            def completed_model(*args, **kwargs):
                (Path(kwargs['cwd']) / 'success.fin').write_text('success')
                return swatplus_trial.subprocess.CompletedProcess(args[0], 0)
            with patch.object(swatplus_trial, 'check_clean_source', return_value={}), \
                 patch.object(swatplus_trial, 'configure_period_and_print'), \
                 patch.object(swatplus_trial.subprocess, 'run', side_effect=completed_model), \
                 patch.object(native_calibration, 'check_initialization', return_value=[]) as check, \
                 patch.object(swatplus_trial, 'extract_outlet', return_value={}), \
                 patch.object(swatplus_trial, 'check_basin_outputs', return_value={}):
                result = swatplus_trial.run_trial(spec)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['parameter_changes'], [])
            check.assert_called_once()


if __name__ == '__main__':
    unittest.main()
