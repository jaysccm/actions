"""Exercise the workflow receiver against verified, corrupt and mismatched images."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/release.yml'


def receive_script():
    lines = WORKFLOW.read_text().split('      - name: Verify archive and load identical image\n', 1)[1]
    block = lines.split('        run: |\n', 1)[1].split('      - name:', 1)[0]
    return '\n'.join(line[10:] for line in block.splitlines())


def image_archive(tag='fixture:abc', bad_manifest=False):
    config = b'{"architecture":"amd64","os":"linux"}'
    config_hash = hashlib.sha256(config).hexdigest()
    config_id = 'sha256:' + config_hash
    manifest = json.dumps({'config': {'digest': config_id}}).encode()
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    manifest_id = 'sha256:' + manifest_hash
    if bad_manifest:
        manifest += b' '
    index = json.dumps({'manifests': [{'digest': manifest_id}]}).encode()
    legacy = json.dumps([{'Config': 'blobs/sha256/' + config_hash, 'RepoTags': [tag]}]).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, data in (
            ('manifest.json', legacy),
            ('index.json', index),
            ('blobs/sha256/' + config_hash, config),
            ('blobs/sha256/' + manifest_hash, manifest),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), config_id, manifest_id


class ArtifactValidation(unittest.TestCase):
    def run_receive(self, corrupt=False, loaded_id='config', broken_gzip=False,
                    wrong_tag=False, bad_manifest=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'artifact').mkdir()
            payload, config_id, manifest_id = image_archive(
                tag='fixture:other' if wrong_tag else 'fixture:abc',
                bad_manifest=bad_manifest,
            )
            if broken_gzip:
                payload = b'not a gzip stream'
            (root / 'artifact/image.tar.gz').write_bytes(payload)
            docker = root / 'docker'
            docker.write_text('''#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$RUNNER_TEMP/docker-calls"
if [ "$1" = load ]; then cat >/dev/null; else printf '%s\n' "$MOCK_IMAGE_ID"; fi
''')
            docker.chmod(0o755)
            ids = {'config': config_id, 'manifest': manifest_id, 'wrong': 'sha256:' + 'f' * 64}
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], RUNNER_TEMP=str(root),
                       ARCHIVE_NAME='artifact', DEPLOY_IMAGE='fixture:abc', EXPECTED_IMAGE_ID=config_id,
                       EXPECTED_ARCHIVE_SHA='0' * 64 if corrupt else hashlib.sha256(payload).hexdigest(),
                       MOCK_IMAGE_ID=ids[loaded_id], GITHUB_ENV=str(root / 'github-env'),
                       GITHUB_OUTPUT=str(root / 'github-output'))
            result = subprocess.run(['bash', '-c', receive_script()], env=env, capture_output=True, text=True)
            calls = (root / 'docker-calls').read_text() if (root / 'docker-calls').exists() else ''
            updated_env = (root / 'github-env').read_text() if (root / 'github-env').exists() else ''
            out = (root / 'github-output').read_text().split() if (root / 'github-output').exists() else []
            self.verify_result = out[-1] if out else None  # last write wins in a step's outputs
            return result, calls, updated_env, ids[loaded_id]

    def test_config_id_succeeds(self):
        result, calls, updated_env, image_id = self.run_receive()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('load', calls)
        self.assertIn('Verified fixture:abc', result.stdout)
        self.assertEqual(self.verify_result, 'result=ok')
        self.assertIn('EXPECTED_IMAGE_ID=' + image_id, updated_env)

    def test_manifest_id_succeeds_and_reaches_updater(self):
        result, calls, updated_env, image_id = self.run_receive(loaded_id='manifest')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('load', calls)
        self.assertIn('EXPECTED_IMAGE_ID=' + image_id, updated_env)

    def test_corruption_rejected_before_docker_load(self):
        result, calls, _, _ = self.run_receive(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, '')
        self.assertEqual(self.verify_result, 'result=mismatch')

    def test_wrong_image_blocks_deployment(self):
        result, _, _, _ = self.run_receive(loaded_id='wrong')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Image ID mismatch', result.stderr)
        self.assertEqual(self.verify_result, 'result=mismatch')

    def test_bad_gzip_cannot_be_hidden_by_successful_docker_exit(self):
        result, _, _, _ = self.run_receive(broken_gzip=True)
        self.assertNotEqual(result.returncode, 0)

    def test_wrong_tag_rejected_before_docker_load(self):
        result, calls, _, _ = self.run_receive(wrong_tag=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, '')

    def test_inner_manifest_digest_rejected_before_docker_load(self):
        result, calls, _, _ = self.run_receive(bad_manifest=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, '')


if __name__ == '__main__':
    unittest.main()
