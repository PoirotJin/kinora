"""Import smoke tests: run against checkout files, never the live server tree."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ServerImportTests(unittest.TestCase):
    def run_python(self, *args):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), str(root / 'scripts'))))
        result = subprocess.run([sys.executable, *args], cwd=root, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_server_entrypoint(self):
        self.run_python('scripts/review_server.py', '--help')

    def test_routes_and_deferred_services(self):
        self.run_python('-c', 'import review_server, motion_operations, review_assessment, '
                        'review_calibration, review_harness, review_trash, harness_service, '
                        'harness_retarget, review_motion_edits, retarget_batch')


if __name__ == '__main__':
    unittest.main()
