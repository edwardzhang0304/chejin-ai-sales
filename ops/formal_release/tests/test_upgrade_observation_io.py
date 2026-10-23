"""Deterministic observer contention regression; no Worker or customer state."""
import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[3] / 'worker-client/scripts/run-windows-client-upgrade-test.py'
tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in ('observation_stream', 'read_json')]
namespace = dict(contextmanager=contextmanager, os=os, Path=Path, json=json)
exec(compile(ast.Module(body=functions, type_ignores=[]), str(SCRIPT), 'exec'), namespace)
observation_stream, read_json = namespace['observation_stream'], namespace['read_json']


class ObservationIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '更新状态.json'
        self.path.write_text('\ufeff{"state":"downloading"}', encoding='utf-8')

    def replace_from_other_process(self):
        code = ('import pathlib,os,sys; p=pathlib.Path(sys.argv[1]); '
                't=p.with_suffix(".tmp"); t.write_text(\'{"state":"succeeded"}\'); os.replace(t,p)')
        return subprocess.run([sys.executable, '-c', code, str(self.path)],
                              capture_output=True, text=True, timeout=15)

    def test_reads_bom_and_unicode_filename(self):
        self.assertEqual(read_json(self.path), {'state': 'downloading'})

    def test_missing_file_not_created(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            read_json(self.path)
        self.assertFalse(self.path.exists())

    def test_invalid_json_not_accepted_and_handle_closed(self):
        self.path.write_text('{')
        with self.assertRaises(json.JSONDecodeError):
            read_json(self.path)
        result = self.replace_from_other_process()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exception_closes_observer_handle(self):
        with self.assertRaisesRegex(RuntimeError, 'test abort'):
            with observation_stream(self.path) as stream:
                raise RuntimeError('test abort')
        self.assertTrue(stream.closed)
        result = self.replace_from_other_process()
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_original_python_reader_blocks_atomic_replace(self):
        with self.path.open('rb'):
            result = self.replace_from_other_process()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PermissionError', result.stderr)
        self.assertEqual(read_json(self.path), {'state': 'downloading'})

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
    def test_observer_allows_atomic_replace_and_keeps_consistent_old_snapshot(self):
        with observation_stream(self.path) as stream:
            with self.assertRaises((OSError, ValueError)):
                stream.write(b'forbidden')
            result = self.replace_from_other_process()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(stream.read().decode('utf-8-sig')), {'state': 'downloading'})
        self.assertEqual(read_json(self.path), {'state': 'succeeded'})


if __name__ == '__main__':
    unittest.main()
