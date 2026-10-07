#!/usr/bin/env python3
"""Record delivery-pipeline metrics and push them to an OTLP/HTTP endpoint (JAY-122).

Optional and non-blocking by design: with no OTEL_EXPORTER_OTLP_ENDPOINT it does nothing, a
network, state or validation problem is reported on stderr, and the exit status is always 0.
Installed beside deploy.sh on the production host (~/services/pipeline_metrics.py).

    pipeline_metrics.py record [--project P] [--env E] EVENT [+ EVENT ...]

EVENT is a kind followed by key=value tokens (no spaces in values):
    job stage=<stage> outcome=<success|failure|canceled> [duration=SECONDS] [cache=clean|cached|unknown]
    queue stage=<stage> wait=SECONDS
    verify point=<archive|target> outcome=<ok|mismatch|error>
    tests passed=N failed=N skipped=N
    rollback outcome=<success|failure>

Series carry only bounded enums (project, stage, outcome, cache_state, point, result). Commit SHAs,
run/job IDs and branch names are never accepted as label values: every value must be a known enum
(project is a short slug) or the event is dropped. Counters and histograms are cumulative and kept in a
state file so they survive between invocations; a lost state file is a counter reset, which Prometheus
handles.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

STAGES = {"build", "test", "ship", "deploy", "health"}
OUTCOMES = {"success", "failure", "canceled"}
CACHE = {"clean", "cached", "unknown"}
VERIFY = {"ok", "mismatch", "error"}
POINTS = {"archive", "target"}
ROLLBACK = {"success", "failure"}
PROJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")  # a 40-hex SHA starts with a digit or is too long
SHA_LIKE = re.compile(r"^[0-9a-f]{7,}$")
ENV_NAMES = {"production", "development", "test"}
DURATION_BUCKETS = [5, 15, 30, 60, 120, 300, 600, 1200, 2400, 3600]
QUEUE_BUCKETS = [1, 5, 15, 30, 60, 120, 300, 900, 3600]
MAX_SECONDS = 7 * 24 * 3600


class Reject(Exception):
    pass


def enum(value: str, allowed: set[str], what: str) -> str:
    if value not in allowed:
        raise Reject(f"{what} must be one of {sorted(allowed)}")
    return value


def seconds(value: str, what: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise Reject(f"{what} is not a number") from None
    if not 0 <= number <= MAX_SECONDS:
        raise Reject(f"{what} out of range")
    return number


def count(value: str, what: str) -> int:
    if not re.fullmatch(r"\d{1,9}", value):
        raise Reject(f"{what} is not a count")
    return int(value)


def parse_events(tokens: list[str]) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: list[str] = []
    for token in [*tokens, "+"]:
        if token == "+":
            if current:
                kind, *pairs = current
                fields = {"kind": kind}
                for pair in pairs:
                    key, sep, value = pair.partition("=")
                    if not sep:
                        raise Reject(f"bad token {pair!r}")
                    fields[key] = value
                events.append(fields)
            current = []
        else:
            current.append(token)
    return events


def series_key(name: str, **labels: str) -> str:
    return json.dumps([name, sorted(labels.items())])


class State:
    """Cumulative series kept across invocations."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"start": time.time_ns(), "counters": {}, "histograms": {}, "gauges": {}}
        self.prior: dict[str, object] = {}  # value of each counter/histogram before this run touched it

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text())
            if all(k in loaded for k in ("start", "counters", "histograms", "gauges")):
                self.data = loaded
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"pipeline_metrics: state unreadable, counters restart: {error}", file=sys.stderr)

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)

    def add(self, name: str, amount: int = 1, **labels: str) -> None:
        key = series_key(name, **labels)
        self.prior.setdefault(key, self.data["counters"].get(key, 0))
        self.data["counters"][key] = self.data["counters"].get(key, 0) + amount

    def observe(self, name: str, value: float, bounds: list[int], **labels: str) -> None:
        key = series_key(name, **labels)
        self.prior.setdefault(key, json.loads(json.dumps(
            self.data["histograms"].get(key, {"count": 0, "sum": 0.0, "buckets": [0] * (len(bounds) + 1)}))))
        h = self.data["histograms"].setdefault(key, {"count": 0, "sum": 0.0, "buckets": [0] * (len(bounds) + 1)})
        h["count"] += 1
        h["sum"] += value
        index = next((i for i, b in enumerate(bounds) if value <= b), len(bounds))
        h["buckets"][index] += 1

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        self.data["gauges"][series_key(name, **labels)] = value


def apply_event(state: State, project: str, event: dict[str, str]) -> None:
    kind = event["kind"]
    if kind == "job":
        stage = enum(event.get("stage", ""), STAGES, "stage")
        outcome = enum(event.get("outcome", ""), OUTCOMES, "outcome")
        cache = enum(event.get("cache", "unknown"), CACHE, "cache")
        duration = seconds(event["duration"], "duration") if "duration" in event else None
        state.add("delivery_jobs", project=project, stage=stage, outcome=outcome)
        if duration is not None and outcome != "canceled":
            state.observe("delivery_job_duration", duration, DURATION_BUCKETS,
                          project=project, stage=stage, cache_state=cache if stage == "build" else "unknown")
        if outcome == "success":
            state.set_gauge("delivery_last_success_timestamp", time.time(), project=project, stage=stage)
    elif kind == "queue":
        stage = enum(event.get("stage", ""), STAGES, "stage")
        state.observe("delivery_queue_wait", seconds(event.get("wait", ""), "wait"), QUEUE_BUCKETS, stage=stage)
    elif kind == "verify":
        point = enum(event.get("point", ""), POINTS, "point")
        outcome = enum(event.get("outcome", ""), VERIFY, "outcome")
        state.add("delivery_artifact_verifications", project=project, point=point, outcome=outcome)
    elif kind == "tests":
        results = {r: count(event[r], r) for r in ("passed", "failed", "skipped") if r in event}
        if not results:
            raise Reject("tests needs passed/failed/skipped")
        for result, amount in results.items():  # absent counts stay absent, not zero
            state.add("delivery_tests", amount, project=project, result=result)
    elif kind == "rollback":
        state.add("delivery_rollbacks", project=project, outcome=enum(event.get("outcome", ""), ROLLBACK, "outcome"))
    else:
        raise Reject(f"unknown event kind {kind!r}")


def attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def build_payload(state: State, env_name: str) -> dict:
    now = str(time.time_ns())
    start = str(int(state.data["start"]))

    def labels(key: str) -> tuple[str, list[dict]]:
        name, pairs = json.loads(key)
        return name, [attr(k, v) for k, v in pairs]

    metrics: dict[str, dict] = {}

    def metric(name: str, unit: str, description: str, body: str) -> dict:
        entry = metrics.setdefault(name, {"name": name, "unit": unit, "description": description, body: {"dataPoints": []}})
        return entry[body]

    descriptions = {
        "delivery_jobs": "Terminal build/test/ship/deploy/health jobs by outcome; cancellations are separate.",
        "delivery_job_duration": "Elapsed stage duration; queue wait is separate.",
        "delivery_queue_wait": "Time from queued to start.",
        "delivery_artifact_verifications": "Archive and deployed-target artifact checks against the tested artifact.",
        "delivery_tests": "Tests reported by the project's test command (absent when not reported).",
        "delivery_rollbacks": "Rollback attempts after a failed deploy and their observed result.",
        "delivery_last_success_timestamp": "Unix time of the last successful job per stage.",
    }
    for key, value in state.data["counters"].items():
        name, attrs = labels(key)
        body = metric(name, "{" + name.removeprefix("delivery_").rstrip("s") + "}", descriptions[name], "sum")
        body["aggregationTemporality"] = 2
        body["isMonotonic"] = True
        body["dataPoints"].append({"attributes": attrs, "startTimeUnixNano": start, "timeUnixNano": now, "asInt": str(value)})
    for key, h in state.data["histograms"].items():
        name, attrs = labels(key)
        bounds = DURATION_BUCKETS if name == "delivery_job_duration" else QUEUE_BUCKETS
        body = metric(name, "s", descriptions[name], "histogram")
        body["aggregationTemporality"] = 2
        body["dataPoints"].append({"attributes": attrs, "startTimeUnixNano": start, "timeUnixNano": now,
                                   "count": str(h["count"]), "sum": h["sum"], "explicitBounds": bounds,
                                   "bucketCounts": [str(c) for c in h["buckets"]]})
    for key, value in state.data["gauges"].items():
        name, attrs = labels(key)
        metric(name, "s", descriptions[name], "gauge")["dataPoints"].append(
            {"attributes": attrs, "timeUnixNano": now, "asDouble": value})
    resource = {"attributes": [attr("service.name", "delivery-pipeline"), attr("service.namespace", "jaysccm"),
                               attr("deployment.environment.name", env_name)]}
    return {"resourceMetrics": [{"resource": resource, "scopeMetrics": [
        {"scope": {"name": "jaysccm.actions.pipeline_metrics"}, "metrics": list(metrics.values())}]}]}


def push(endpoint: str, payload: dict, timeout: float) -> None:
    url = endpoint.rstrip("/") + "/v1/metrics"
    if not url.startswith(("http://", "https://")):
        raise Reject("endpoint must be http(s)")
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def record(argv: list[str]) -> None:
    project, env_name = os.environ.get("PIPELINE_PROJECT", ""), os.environ.get("PIPELINE_ENV", "production")
    rest = list(argv)
    while rest and rest[0].startswith("--"):
        flag = rest.pop(0)
        value = rest.pop(0) if rest else ""
        if flag == "--project":
            project = value
        elif flag == "--env":
            env_name = value
        else:
            raise Reject(f"unknown flag {flag}")
    if not PROJECT_RE.fullmatch(project) or SHA_LIKE.fullmatch(project):
        raise Reject("project must be a short slug")
    enum(env_name, ENV_NAMES, "env")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return  # telemetry is optional
    path = Path(os.environ.get("PIPELINE_METRICS_STATE", Path.home() / "services" / ".pipeline-metrics.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = State(path)
        state.load()
        applied = 0
        for event in parse_events(rest):
            try:
                apply_event(state, project, event)
                applied += 1
            except (Reject, KeyError) as error:
                print(f"pipeline_metrics: dropped {event.get('kind')} event: {error}", file=sys.stderr)
        if not applied:
            return
        state.save()
        timeout = float(os.environ.get("PIPELINE_METRICS_TIMEOUT", "3"))
        if state.prior:
            # Push each touched series' previous value first (0 when new), so every event is a visible
            # step: rate/increase see a first-ever event and changes() counts events exactly.
            before = State(path)
            before.data = json.loads(json.dumps(state.data))
            for key, value in state.prior.items():
                table = "counters" if key in before.data["counters"] else "histograms"
                before.data[table][key] = value
            push(endpoint, build_payload(before, env_name), timeout)
            time.sleep(0.05)
        push(endpoint, build_payload(state, env_name), timeout)


def main(argv: list[str]) -> int:
    try:
        if argv and argv[0] == "record":
            record(argv[1:])
        else:
            print(__doc__, file=sys.stderr)
    except Exception as error:  # never block a pipeline on telemetry
        print(f"pipeline_metrics: not recorded: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
