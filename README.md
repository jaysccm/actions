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
its SHA-256 before loading, verifies the Docker image ID after loading, then calls
`~/services/deploy.sh` with `DEPLOY_IMAGE`. No build or full test suite runs on
pearl. Transfer needs outbound HTTPS to GitHub's artifact service, not inbound
LAN access, a registry password, or the old bee-to-pearl SSH key. Self-hosted
runner support for the download action must be verified during rollout.

Production is restricted to push-to-main runs and serialized per deploy host.
The caller must still gate the release job on all its checks. Build/test has a
45-minute timeout; receive/deploy has 30 minutes. This targets linux/amd64, matching
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
Verify outbound artifact access, identical image ID, health and rollback. Revert
the caller's pinned workflow reference to its prior revision to restore the old
pipeline if necessary; never bypass a failed test to deploy.

Usage/cost: this moves CPU usage to Actions minutes and adds one compressed image
per successful release/rehearsal, retained seven days, plus Buildx cache storage.
Record actual build minutes and compressed image size from initial runs. Billing
and included allowances depend on the organization's plan and remain unknown;
no paid larger runner or billing setting is enabled here.

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

`poke` is in the knowledge base but absent from this organization's repository
list; its repository/workflow location remains an inventory gap.

References: [GitHub artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data),
[Docker Actions cache](https://docs.docker.com/build/ci/github-actions/cache/).
