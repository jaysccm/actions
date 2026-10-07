"""Pipeline metrics emitter: optional, non-blocking, bounded labels, cumulative across runs (JAY-122)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_fixture import Collector  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "host" / "pipeline_metrics.py"
SHA = "a" * 40


class Emitter(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.json"
        self.collector = Collector()
        self.addCleanup(self.collector.close)

    def run_record(self, *args: str, endpoint: str | None = None, extra_env: dict | None = None):
        env = {**os.environ, "PIPELINE_METRICS_STATE": str(self.state), **(extra_env or {})}
        env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        if endpoint is None:
            endpoint = self.collector.url
        if endpoint:
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        return subprocess.run([sys.executable, str(SCRIPT), "record", *args], env=env, text=True, capture_output=True)

    def test_disabled_without_endpoint(self) -> None:
        result = self.run_record("--project", "demo", "job", "stage=build", "outcome=success", endpoint="")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.collector.payloads, [])

    def test_unreachable_endpoint_never_blocks(self) -> None:
        started = time.monotonic()
        result = self.run_record("--project", "demo", "job", "stage=build", "outcome=failure",
                                 endpoint="http://127.0.0.1:9", extra_env={"PIPELINE_METRICS_TIMEOUT": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("not recorded", result.stderr)

    def test_garbage_input_and_corrupt_state_exit_zero(self) -> None:
        self.state.write_text("{not json")
        for args in (["--project", "demo", "job", "stage=build"], ["--bogus", "x"], ["--project", "demo", "wat"], []):
            self.assertEqual(self.run_record(*args).returncode, 0, args)
        ok = self.run_record("--project", "demo", "job", "stage=build", "outcome=success", "duration=3")
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(self.collector.value("delivery_jobs", stage="build"), 1)

    def test_counters_accumulate_across_processes_and_step_visibly(self) -> None:
        for _ in range(3):
            self.run_record("--project", "demo", "job", "stage=deploy", "outcome=success", "duration=30")
        self.assertEqual(self.collector.value("delivery_jobs", project="demo", stage="deploy", outcome="success"), 3)
        self.assertEqual(self.collector.value("delivery_job_duration", stage="deploy"), 3)
        # Each event is pushed as previous value then new value: 0,1 then 1,2 then 2,3.
        seen = []
        for p in self.collector.payloads:
            for m in p["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
                if m["name"] == "delivery_jobs":
                    seen.append(int(m["sum"]["dataPoints"][0]["asInt"]))
        self.assertEqual(seen, [0, 1, 1, 2, 2, 3])
        self.assertIsNotNone(self.collector.value("delivery_last_success_timestamp", stage="deploy"))

    def test_labels_are_bounded_and_never_carry_identifiers(self) -> None:
        self.run_record("--project", SHA, "job", "stage=build", "outcome=success")  # project is a SHA
        self.run_record("--project", "demo", "job", "stage=feature/x", "outcome=success")  # branch name
        self.run_record("--project", "demo", "job", "stage=build", f"outcome={SHA}")  # commit as outcome
        self.run_record("--project", "demo", "job", "stage=build", "outcome=success", "run=123456", f"sha={SHA}")
        self.run_record("--project", "demo", "verify", "point=archive", "outcome=ok", "+", "tests", "passed=1")
        allowed = {"project", "stage", "outcome", "cache_state", "point", "result"}
        for (_, labels) in self.collector.latest():
            self.assertLessEqual({k for k, _ in labels}, allowed)
            for _, value in labels:
                self.assertNotIn(SHA, value)
                self.assertNotRegex(value, r"^\d{3,}$")
        # Only the one well-formed job was recorded (unknown extra tokens are ignored).
        self.assertEqual(self.collector.value("delivery_jobs"), 1)

    def test_resource_identity_has_no_dynamic_ids(self) -> None:
        self.run_record("--project", "demo", "--env", "development", "queue", "stage=ship", "wait=4")
        resource = self.collector.payloads[-1]["resourceMetrics"][0]["resource"]["attributes"]
        got = {a["key"]: a["value"]["stringValue"] for a in resource}
        self.assertEqual(got, {"service.name": "delivery-pipeline", "service.namespace": "jaysccm",
                               "deployment.environment.name": "development"})
        before = len(self.collector.payloads)
        self.run_record("--project", "demo", "--env", "feature-x", "job", "stage=build", "outcome=success")
        self.assertEqual(len(self.collector.payloads), before)  # an unknown environment sends nothing

    def test_absent_test_counts_stay_absent(self) -> None:
        self.run_record("--project", "demo", "tests", "passed=10", "failed=0")
        self.assertEqual(self.collector.value("delivery_tests", result="passed"), 10)
        self.assertEqual(self.collector.value("delivery_tests", result="failed"), 0)
        self.assertIsNone(self.collector.value("delivery_tests", result="skipped"))


def workflow_summary_script() -> str:
    """The inline python that build-test uses to turn step results into events."""
    text = (ROOT / ".github/workflows/release.yml").read_text()
    match = re.search(r"python3 - <<'PY' >> \"\$GITHUB_OUTPUT\" \|\| true\n(.*?)\n\s+PY\n", text, re.S)
    assert match, "summary script not found"
    return textwrap.dedent(match.group(1))


class BuildSummary(unittest.TestCase):
    def summarise(self, build: str, test: str, summary: dict | None = None, no_cache: str = "false") -> dict[str, str]:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.json"
            if summary is not None:
                path.write_text(json.dumps(summary))
            now = int(time.time())
            env = {**os.environ, "BUILD_OUTCOME": build, "TEST_OUTCOME": test, "SHIP_OUTCOME": "skipped",
                   "NO_CACHE": no_cache, "PIPELINE_TEST_SUMMARY": str(path), "PM_BUILD0": str(now - 100),
                   "PM_TEST0": str(now - 40) if test != "skipped" else ""}
            out = subprocess.run([sys.executable, "-c", workflow_summary_script()], env=env, text=True,
                                 capture_output=True, check=True).stdout
        return dict(line.split("=", 1) for line in out.splitlines())

    def test_failed_build_is_recorded_without_test_events(self) -> None:
        events = self.summarise("failure", "skipped")["events"]
        self.assertRegex(events, r"^job stage=build outcome=failure duration=1\d\d cache=unknown$")

    def test_canceled_and_clean_build(self) -> None:
        self.assertIn("outcome=canceled", self.summarise("cancelled", "skipped")["events"])
        self.assertIn("cache=clean", self.summarise("success", "success", no_cache="true")["events"])

    def test_test_counts_only_when_reported_and_sane(self) -> None:
        events = self.summarise("success", "failure", {"passed": 9, "failed": 1})["events"]
        self.assertIn("job stage=test outcome=failure", events)
        self.assertTrue(events.endswith("tests passed=9 failed=1"), events)
        self.assertNotIn("tests", self.summarise("success", "success")["events"])
        hostile = self.summarise("success", "success", {"passed": "1; rm -rf /", "failed": -4})["events"]
        self.assertNotIn("tests", hostile)


if __name__ == "__main__":
    unittest.main()
