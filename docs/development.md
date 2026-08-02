---
title: Development
nav_order: 8
---

# Development
{: .no_toc }

1. TOC
{:toc}

---

## Toolchain

Python uses `uv` and `ruff`, the frontend uses `bun`, Warden uses `cargo`.

```bash
make setup   # uv sync --extra server
make test    # every suite CI runs: python x 3 + cargo
make lint    # ruff · uv lock --check · clippy · tsc · eslint
```

The unit suite runs without Docker or Kubernetes — the runners no-op their container calls —
so it is fast and CI-safe.

## Tests

Each suite discovers its own `test_*` functions via `tests/_runner.py`, so writing a test is
enough to make it run.

```bash
uv run python tests/test_unit.py           # control plane
uv run python tests/test_sdk.py            # SDK, offline via httpx MockTransport
uv run python tests/test_worker_rewind.py  # worker rewind and reconnect
cargo test --manifest-path warden/Cargo.toml
```

### Red-team checks

These need a built sandbox image and are not in the unit CI. Run them before a deploy:

```bash
make redteam          # sandbox isolation boundary
make redteam-pinhole  # the firewall opens exactly one endpoint
make redteam-conceal  # no environment tells, combined CA store
```

## Drift guards

Some tests exist to stop a class of drift rather than to check a behaviour. A failure means a
duplicate has reappeared.

| Test | Asserts |
|---|---|
| `test_harness_surfaces_are_one_schema` | The harness field set matches across the dataclass, the API body, the agent PATCH model and `web/lib/types.ts`. It parses the TypeScript, since the console has no Python to import. |
| `test_tool_catalog_is_single_source` | The tool and skill catalog is served from `terracore/toolset.py`, not re-listed in the console. |
| `test_stores_are_owner_only` | Every durable JSON store is written `0600`. |
| `test_warden_mandatory` | No config can produce an unmediated sandbox, and the Pod manifest builder has no form that omits the sidecar. |

## Releases

Releases are automated from [Conventional Commits](https://www.conventionalcommits.org/) on
`main`.

| Prefix | Effect |
|---|---|
| `fix:` | Patch release |
| `feat:` | Minor release |
| `feat!:` or a `BREAKING CHANGE:` footer | Major release |
| `chore:`, `docs:`, `refactor:`, `test:`, `ci:` | No release |

semantic-release derives the version, runs `scripts/bump-version.sh` to stamp it into
`pyproject.toml`, `sdk/pyproject.toml` and `warden/Cargo.toml`, then commits, tags and
publishes the GitHub release. Only then do the three images and the SDK wheel build, so a
published artifact always corresponds to a tag.

### One-time setup

Three console settings, each of which fails a job with a message that does not name it. Do all
three before the first push to `main`.

1. **Baseline tag.** semantic-release computes the next version from git tags and ignores the
   version in `pyproject.toml`. With no tags it starts at **1.0.0**. Tag the first commit:

   ```bash
   git tag v0.1.0 <first-commit-sha> && git push origin v0.1.0
   ```

   The release workflow refuses to run without a `v*` tag rather than silently publishing a
   major.

2. **PyPI trusted publisher.** Create a pending publisher at
   <https://pypi.org/manage/account/publishing/>:

   | Field | Value |
   |---|---|
   | Project | `terrarium-python` |
   | Owner | `OAISP` |
   | Repository | `terrarium` |
   | Workflow | `release.yml` |
   | Environment | `publish and release` |

   The environment string must match the `environment:` in the workflow's `sdk` job exactly,
   and the publisher must exist before the first upload. Otherwise the OIDC exchange returns
   403 and the job fails with a permissions error rather than a useful message.

3. **GitHub Pages.** Settings → Pages → Source: **GitHub Actions**. Until then
   `actions/configure-pages` fails and the Docs workflow is red.

### Repository secrets

| Secret | Used by |
|---|---|
| `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` | Pushing the three images |
| *(none)* | The SDK publishes via OIDC trusted publishing |

## Documentation

Jekyll and [Just the Docs](https://just-the-docs.com), built from `docs/` by GitHub Actions on
every push to `main`.

```bash
cd docs && bundle install && bundle exec jekyll serve
```
