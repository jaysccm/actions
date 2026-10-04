# actions

Shared GitHub Actions workflows for repositories in `jaysccm`.

## Deploy

`.github/workflows/deploy.yml` is a reusable workflow. It runs on the production
host's self-hosted runner (label `bee` by default) and calls
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
scp host/deploy.sh bee:services/deploy.sh
```

## Adding a project

1. Check the project out on the host at `~/services/<project>` with its `.env`.
2. Add a recipe for it to `host/deploy.sh` and install the script on the host.
3. Add the `deploy` job above to the project's workflow.
