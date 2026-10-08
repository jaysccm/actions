# actions

Shared GitHub Actions workflows for repositories in `jaysccm`.

## Deploy

`.github/workflows/deploy.yml` is a reusable workflow. It runs on the production
host's self-hosted runner (label `pearl` by default) and calls
`~/services/deploy.sh <project> <commit>` on that host. Deploys to one host
queue; they never overlap.

Call it from a project's CI workflow, gated on that project's own test jobs:

```yaml
  deploy:
    needs: [checks]            # the project's test jobs
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    uses: jaysccm/actions/.github/workflows/deploy.yml@main
    with:
      project: sportsbook      # directory under ~/services on the host
```

## host/deploy.sh

The script installed at `~/services/deploy.sh` on the host. It holds one recipe
per project; add a `case` branch for a new project. The workflow does not copy
it to the host: after changing it here, install it by hand.

```sh
scp host/deploy.sh pearl:services/deploy.sh
```

## Adding a project

1. Check the project out on the host at `~/services/<project>` with its `.env`.
2. Add a recipe for it to `host/deploy.sh` and install the script on the host.
3. Add the `deploy` job above to the project's workflow.

## Release: GitHub builds/tests, pearl deploys (JAY-124)

`release.yml` builds and tests the caller commit on `ubuntu-24.04`, using Buildx
with a GitHub Actions layer cache. The project's `test-command` receives `IMAGE`.
Only after success does it upload a gzipped Docker image as an immutable Actions
artifact (seven-day retention). A retagged image after testing is rejected.

The receive job downloads the specific artifact ID from the same run, verifies
its SHA-256 before loading, and accepts only an image ID derived from that archive's
config, platform manifest or OCI index. Docker engines can report different IDs
for the same archive; the receiver passes its actual loaded ID to the updater. It then calls
`~/services/deploy.sh` with `DEPLOY_IMAGE`. No build or full test suite runs on
pearl. Transfer needs outbound HTTPS to GitHub's artifact service, not inbound
LAN access, a registry password, or the old bee-to-pearl SSH key. Self-hosted
runner support for the download action must be verified during rollout.

Production is restricted to push-to-main runs and serialized per deploy host.
The caller must still gate the release job on all its checks. Build/test has a
45-minute timeout; receive/deploy has 90 minutes to accommodate the updater's
one-hour scheduled-job wait plus update/rollback time. This targets linux/amd64, matching
the current hosts. Other architectures need a separate validated configuration.

Set `deploy: false` to run the same download, integrity and load steps on a fresh
GitHub-hosted runner. `checks.yml` rehearses that path with a synthetic image on
PRs, and exercises corrupt archives, failed decompression and mismatched IDs.
Rehearsals never schedule a production runner. Tests use no production secrets.

```yaml
  release:
    needs: [checks]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    uses: jaysccm/actions/.github/workflows/release.yml@<reviewed-commit>
    with:
      project: sportsbook
      target: dev
      test-command: scripts/release_checks.sh "$IMAGE"
```

The old `build-host` and `deploy-address` inputs are removed; callers using them
must migrate. `deploy-host` still defaults to pearl. The ship scripts remain for
manual rollback compatibility but are not used by the new workflow. Do not retire
keys/accounts until a reviewed production handoff succeeds. Existing project
updaters retain their health-check and rollback behavior; a failed archive/image
check exits before invoking the updater.

Rollout: review the shared workflow and caller PRs, run the actual caller's
`deploy: false` rehearsal, then schedule the first production release with Jay.
Verify outbound artifact access, loaded image identity against the archive, health and rollback. Revert
the caller's pinned workflow reference to its prior revision to restore the old
pipeline if necessary; never bypass a failed test to deploy.

Usage/cost: this moves CPU usage to Actions minutes and adds one compressed image
per successful release/rehearsal, retained seven days, plus Buildx cache storage.
Record actual build minutes and compressed image size from initial runs. Billing
and included allowances depend on the organization's plan and remain unknown;
no paid larger runner or billing setting is enabled here.

## Pipeline metrics (JAY-122)

`host/pipeline_metrics.py` records delivery metrics and pushes them to an OTLP/HTTP
endpoint. It is optional and never blocks: with no `OTEL_EXPORTER_OTLP_ENDPOINT` it does
nothing, and a bad argument, unreachable endpoint or corrupt state file is logged to stderr
with exit status 0. Install it beside `deploy.sh` (`scp host/pipeline_metrics.py
pearl:services/`); it keeps cumulative counters in `~/services/.pipeline-metrics.json`.

| Series (Prometheus name) | Type | Labels |
| --- | --- | --- |
| `delivery_jobs_total` | counter | project, stage (build/test/ship/deploy/health), outcome (success/failure/canceled) |
| `delivery_job_duration_seconds` | histogram | project, stage, cache_state (clean/cached/unknown; build only) |
| `delivery_queue_wait_seconds` | histogram | stage (`ship`: build finished to receive job started) |
| `delivery_artifact_verifications_total` | counter | project, point (archive/target), outcome (ok/mismatch/error) |
| `delivery_tests_total` | counter | project, result (passed/failed/skipped) |
| `delivery_rollbacks_total` | counter | project, outcome (success/failure) |
| `delivery_last_success_timestamp_seconds` | gauge | project, stage |

Resource: `service.name=delivery-pipeline`, `service.namespace=jaysccm`,
`deployment.environment.name` (production by default). Commit SHAs, run/job IDs and
branch names are rejected as label values; correlate them in logs. `point` is a
deviation from the proposed catalog (which had project, outcome only) so the archive
check and the deployed-target check are not mixed.

Where it runs. GitHub-hosted runners cannot reach the LAN stack, so `release.yml`
collects stage results on the hosted runners (job outputs) and a `report` job on the
production runner records them. It runs only for push-to-main releases, nothing depends
on it, and it is skipped when `telemetry-endpoint`/`vars.PIPELINE_OTLP_ENDPOINT` is
empty or the script is not installed. PR rehearsals report nothing. `deploy.sh` itself
records deploy, health, target verification and rollback.

Definitions. Deploy success is not the job exit status: it needs the updater to exit 0,
a running container labelled `com.docker.compose.project=<project>` whose image is the
tested `EXPECTED_IMAGE_ID`, and no unhealthy/starting container. Otherwise a deploy that
exits 0 is recorded as a failure with a warning; set `DEPLOY_VERIFY_ENFORCE=1` to also
fail the job (off by default so merging this changes no production behaviour). After a
failed update, rollback is `success` only when the previously running images are running
again and healthy; the first deploy of a project (nothing running before) records none.
Config-only projects (telemetry) have no artifact check, only health. A cancelled deploy
is `canceled`, not failure.

Known limits: `cache_state` is `clean` only for `no-cache: true` builds, otherwise
`unknown` (GitHub gives no cache-hit signal to later steps); test counts appear only if
the test command writes `{"passed":N,"failed":N,"skipped":N}` to `$PIPELINE_TEST_SUMMARY`;
first-job runner queue wait is not measured (the API needs `actions: read`); "target runs
the tested image" is any-running-container, so a project with several images must also be
checked by its own updater; native (non-Docker) services record health/verification as
failed/error until an adapter exists (JAY-132).

Tests: `python3 -m unittest discover -s tests` covers the emitter (disabled, outage,
corrupt state, bounded labels), the workflow's summary step (failed/cancelled build, bad
test summaries), and `deploy.sh` against a scratch project with a fake docker: verified
success, failed health check, artifact mismatch, failed update with and without a
holding rollback, first deploy, and telemetry outage/absence.

## Inventory (2026-10-06)

All seven non-archived repositories returned by the jaysccm organization inventory:

- `gamblor`: checks/taskfile/ui-smoke already hosted; shared release build/test was
  on bee. Migrate the shared workflow and pin the caller to the reviewed revision.
- `telemetry`: checks already hosted; config-only deployment on pearl (no image build).
- `algo` (Pinchy): tests already hosted, with a disposable Postgres service.
- `proof`: its optional walkthrough workflow is hosted; this is not evidence of a
  comprehensive test suite. No self-hosted build/test job to migrate.
- `actions`: release build/test migrated here; deployment remains on pearl.
- `pearl`, `bigbrother`: no `.github/workflows` on main; no existing CI jobs to move.
  Adding new test coverage is separate work, not a completed migration claim.

`jayshaffer/poke` remains in the personal account (verified separately); its main
branch has no `.github/workflows`, so there is no existing CI workload to move.

References: [GitHub artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data),
[Docker Actions cache](https://docs.docker.com/build/ci/github-actions/cache/).

## strta/ — standard project scripts (JAY-147)

Reference implementation of the `script/` entry points every project exposes (`bootstrap`, `setup`, `update`,
`deploy`, `status`, `test`) with `script/ext/<hook>` customization. Copy `strta/script/` into a project as
`script/`. Contracts, exit codes and hook list: `infrastructure/project-scripts-standard.md` in the knowledge base.

```sh
sh strta/tests/run.sh
```
