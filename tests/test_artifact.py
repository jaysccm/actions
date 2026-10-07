"""Exercise the actual workflow receive shell against corrupt and mismatched artifacts."""
import gzip
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/release.yml'

def receive_script():
    lines = WORKFLOW.read_text().split('      - name: Verify archive and load identical image\n', 1)[1]
    block = lines.split('        run: |\n', 1)[1].split('      - name:', 1)[0]
    return '\n'.join(line[10:] for line in block.splitlines())

class ArtifactValidation(unittest.TestCase):
    def run_receive(self, corrupt=False, wrong_id=False, broken_gzip=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'artifact').mkdir()
            payload = b'not a gzip stream' if broken_gzip else gzip.compress(b'synthetic image archive')
            (root / 'artifact/image.tar.gz').write_bytes(payload)
            docker = root / 'docker'
            docker.write_text('''#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$RUNNER_TEMP/docker-calls"
if [ "$1" = load ]; then cat >/dev/null; else printf '%s\n' "$MOCK_IMAGE_ID"; fi
''')
            docker.chmod(0o755)
            expected = 'sha256:' + 'a' * 64
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], RUNNER_TEMP=str(root),
                       ARCHIVE_NAME='artifact', DEPLOY_IMAGE='fixture:abc', EXPECTED_IMAGE_ID=expected,
                       EXPECTED_ARCHIVE_SHA='0' * 64 if corrupt else hashlib.sha256(payload).hexdigest(),
                       MOCK_IMAGE_ID='sha256:' + 'b' * 64 if wrong_id else expected)
            result = subprocess.run(['bash', '-c', receive_script()], env=env, capture_output=True, text=True)
            calls = (root / 'docker-calls').read_text() if (root / 'docker-calls').exists() else ''
            return result, calls

    def test_valid_archive_and_image(self):
        result, calls = self.run_receive()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('load', calls)
        self.assertIn('Verified fixture:abc', result.stdout)

    def test_corruption_rejected_before_docker_load(self):
        result, calls = self.run_receive(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, '')

    def test_wrong_image_blocks_deployment(self):
        result, _ = self.run_receive(wrong_id=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Image ID mismatch', result.stderr)

    def test_bad_gzip_cannot_be_hidden_by_successful_docker_exit(self):
        result, _ = self.run_receive(broken_gzip=True)
        self.assertNotEqual(result.returncode, 0)

if __name__ == '__main__':
    unittest.main()
