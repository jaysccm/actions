"""deploy.sh telemetry against a scratch project: no real docker, host or production state (JAY-122).

Deploy success means the updater exited 0, a running container of the project is the tested image, and
none is unhealthy. Failed health, artifact mismatch, failed update and rollback are rehearsed with a fake
docker whose container table the fake updater rewrites.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_fixture import Collector  # noqa: E402

HOST = Path(__file__).resolve().parents[1] / "host"
SHA = "b" * 40
OLD, NEW = "sha256:" + "1" * 64, "sha256:" + "2" * 64

FAKE_DOCKER = r'''#!/bin/sh
table="$FAKE/containers"
case "$1" in
  image) exit 0 ;;
  ps) [ -s "$table" ] && cut -d' ' -f1 "$table"; exit 0 ;;
  inspect)
    fmt="$3"; shift 3
    for id in "$@"; do
      case "$fmt" in
        *Health*) awk -v id="$id" '$1==id {print $3}' "$table" ;;
        *) awk -v id="$id" '$1==id {print $2}' "$table" ;;
      esac
    done ;;
esac
'''

# The fake updater: SCENARIO decides what it does to the container table and its exit status.
FAKE_PYTHON = r'''#!/bin/sh
case "$1" in
  *pipeline_metrics.py) exec "$REAL_PY" "$@" ;;
esac
case "$2" in status) exit 0 ;; esac
t="$FAKE/containers"
case "$SCENARIO" in
  success) echo "c1 $NEW healthy" > "$t" ;;
  wrong_image) ;;                                  # exits 0 but still runs the old image
  unhealthy) echo "c1 $NEW unhealthy" > "$t" ;;     # exits 0 but the health check fails
  failed_rolled_back) exit 1 ;;                     # failed update, old version restored and healthy
  failed_broken) echo "c1 $NEW unhealthy" > "$t"; exit 1 ;;   # failed update, rollback did not hold
esac
exit 0
'''


class DeployTelemetry(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        services = self.home / "services"
        project = services / "sportsbook"
        (project / ".git").mkdir(parents=True)
        (project / "scripts").mkdir()
        (project / "scripts" / "deploy.py").touch()
        shutil.copy(HOST / "deploy.sh", services / "deploy.sh")
        self.have_script = True
        shutil.copy(HOST / "pipeline_metrics.py", services / "pipeline_metrics.py")
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("python3", FAKE_PYTHON)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.fake = self.home / "fake"
        self.fake.mkdir()
        (self.fake / "containers").write_text(f"c1 {OLD} healthy\n")
        self.collector = Collector()
        self.addCleanup(self.collector.close)

    def deploy(self, scenario: str, *, endpoint: str | None = None, enforce: bool = False,
               release: bool = True) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}",
               "FAKE": str(self.fake), "SCENARIO": scenario, "NEW": NEW, "REAL_PY": sys.executable,
               "OTEL_EXPORTER_OTLP_ENDPOINT": self.collector.url if endpoint is None else endpoint,
               "PIPELINE_METRICS_STATE": str(self.home / "state.json"), "PIPELINE_METRICS_TIMEOUT": "1"}
        env.pop("DEPLOY_IMAGE", None)
        if release:
            env.update(DEPLOY_IMAGE=f"sportsbook:{SHA}", EXPECTED_IMAGE_ID=NEW)
        if enforce:
            env["DEPLOY_VERIFY_ENFORCE"] = "1"
        if endpoint == "":
            env.pop("OTEL_EXPORTER_OTLP_ENDPOINT")
        return subprocess.run(["bash", str(self.home / "services" / "deploy.sh"), "sportsbook", SHA],
                              env=env, text=True, capture_output=True, timeout=60)

    def metric(self, name: str, **labels: str):
        return self.collector.value(name, project="sportsbook", **labels)

    def test_verified_deploy_is_a_success(self) -> None:
        result = self.deploy("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metric("delivery_jobs", stage="deploy", outcome="success"), 1)
        self.assertEqual(self.metric("delivery_jobs", stage="health", outcome="success"), 1)
        self.assertEqual(self.metric("delivery_artifact_verifications", point="target", outcome="ok"), 1)
        self.assertIsNotNone(self.metric("delivery_last_success_timestamp", stage="deploy"))
        self.assertIsNone(self.metric("delivery_rollbacks"))

    def test_failed_health_check_is_not_success_even_with_exit_zero(self) -> None:
        result = self.deploy("unhealthy")
        self.assertEqual(result.returncode, 0)  # default: record, do not change production behaviour
        self.assertIn("not verified", result.stderr)
        self.assertEqual(self.metric("delivery_jobs", stage="health", outcome="failure"), 1)
        self.assertEqual(self.metric("delivery_jobs", stage="deploy", outcome="failure"), 1)
        self.assertIsNone(self.metric("delivery_jobs", stage="deploy", outcome="success"))
        self.assertIsNone(self.metric("delivery_last_success_timestamp", stage="deploy"))
        self.assertEqual(self.deploy("unhealthy", enforce=True).returncode, 1)

    def test_artifact_mismatch_is_not_success(self) -> None:
        result = self.deploy("wrong_image")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.metric("delivery_artifact_verifications", point="target", outcome="mismatch"), 1)
        self.assertEqual(self.metric("delivery_jobs", stage="deploy", outcome="failure"), 1)
        self.assertEqual(self.metric("delivery_jobs", stage="health", outcome="success"), 1)
        self.assertEqual(self.deploy("wrong_image", enforce=True).returncode, 1)

    def test_failed_update_with_successful_rollback(self) -> None:
        result = self.deploy("failed_rolled_back")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.metric("delivery_jobs", stage="deploy", outcome="failure"), 1)
        self.assertEqual(self.metric("delivery_rollbacks", outcome="success"), 1)
        self.assertIsNone(self.metric("delivery_rollbacks", outcome="failure"))

    def test_failed_update_where_rollback_does_not_hold(self) -> None:
        result = self.deploy("failed_broken")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.metric("delivery_rollbacks", outcome="failure"), 1)
        self.assertIsNone(self.metric("delivery_rollbacks", outcome="success"))

    def test_first_deploy_failure_has_no_rollback_to_record(self) -> None:
        (self.fake / "containers").write_text("")
        self.assertEqual(self.deploy("failed_broken").returncode, 1)
        self.assertEqual(self.metric("delivery_jobs", stage="deploy", outcome="failure"), 1)
        self.assertIsNone(self.metric("delivery_rollbacks"))

    def test_telemetry_outage_or_absence_never_changes_the_deploy(self) -> None:
        self.assertEqual(self.deploy("success", endpoint="http://127.0.0.1:9").returncode, 0)
        self.assertEqual(self.deploy("failed_rolled_back", endpoint="http://127.0.0.1:9").returncode, 1)
        self.assertEqual(self.deploy("success", endpoint="").returncode, 0)
        os.remove(self.home / "services" / "pipeline_metrics.py")  # script not installed
        self.assertEqual(self.deploy("success").returncode, 0)
        self.assertEqual(self.collector.payloads, [])

    def test_no_identifiers_in_any_label(self) -> None:
        self.deploy("success")
        self.deploy("failed_broken")
        for payload in self.collector.payloads:
            text = repr(payload)
            self.assertNotIn(SHA, text)
            self.assertNotIn(NEW, text)


class TransferAndBuild(unittest.TestCase):
    """Events the release workflow reports for a failed build and a failed transfer (via the same emitter)."""

    def test_failed_build_and_failed_transfer(self) -> None:
        collector = Collector()
        self.addCleanup(collector.close)
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": collector.url,
                   "PIPELINE_METRICS_STATE": str(Path(temp) / "s.json")}
            record = [sys.executable, str(HOST / "pipeline_metrics.py"), "record", "--project", "scratch"]
            # build-test failed at the build step; the receive job never ran.
            subprocess.run([*record, "job", "stage=build", "outcome=failure", "duration=61", "cache=clean"], env=env, check=True)
            # a later release: build and test passed, the archive failed its checksum on the target.
            subprocess.run([*record, "job", "stage=build", "outcome=success", "duration=80", "cache=unknown", "+",
                            "job", "stage=test", "outcome=success", "duration=20", "+", "queue", "stage=ship", "wait=9", "+",
                            "job", "stage=ship", "outcome=failure", "duration=4", "+",
                            "verify", "point=archive", "outcome=mismatch"], env=env, check=True)
        v = lambda n, **kw: collector.value(n, project="scratch", **kw)  # noqa: E731
        self.assertEqual(v("delivery_jobs", stage="build", outcome="failure"), 1)
        self.assertEqual(v("delivery_jobs", stage="build", outcome="success"), 1)
        self.assertEqual(v("delivery_jobs", stage="ship", outcome="failure"), 1)
        self.assertEqual(v("delivery_artifact_verifications", point="archive", outcome="mismatch"), 1)
        self.assertEqual(v("delivery_job_duration", stage="build", cache_state="clean"), 1)
        self.assertIsNone(v("delivery_jobs", stage="deploy"))  # a failed transfer never reaches deploy


if __name__ == "__main__":
    unittest.main()
