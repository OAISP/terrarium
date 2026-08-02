# Orchestrator (control plane) image — the primary deployable.
# Runs the FastAPI API and spawns sandboxes via the Docker, Kubernetes or local runner
# (TERRA_RUNNER). The k8s runner talks to the Kubernetes API directly, so no Docker daemon
# is needed in-cluster; the docker runner needs the host socket mounted.

# --- build: install the frozen project environment ---
FROM ghcr.io/astral-sh/uv:0.9.9 AS uv

FROM python:3.13-slim AS build
WORKDIR /src
COPY --from=uv /uv /usr/local/bin/uv
# Build the virtualenv at its final runtime path. Console scripts contain an
# absolute shebang, so relocating /src/.venv would leave `terra` pointing at a
# Python interpreter that does not exist in the runtime image.
ENV UV_PROJECT_ENVIRONMENT=/opt/terrarium

# Dependencies first, WITHOUT the project. This layer is keyed only on pyproject.toml +
# uv.lock, so editing orchestrator/ or terracore/ no longer re-resolves and re-installs
# fastapi, uvicorn, cryptography and the kubernetes client on every build.
# The SDK (sdk/) is NOT installed here — it's a separately published package.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra server --extra k8s --no-install-project

# Then the project itself, which is the part that actually changes.
COPY README.md ./
COPY terracore ./terracore
COPY orchestrator ./orchestrator
RUN uv sync --frozen --no-dev --extra server --extra k8s --no-editable

# --- runtime: copy only the frozen environment (no resolver/cache/build source) ---
FROM python:3.13-slim

# The docker CLI, for TERRA_RUNNER=docker. The orchestrator drives sandboxes by shelling out
# to `docker run/attach/inspect/cp/rm/network/volume` against a mounted host socket, so
# without a client binary that runner fails at the first session with "docker: not found" —
# at runtime, on the first launch, rather than at startup. Only the static client is copied:
# no daemon, no containerd, no buildx (~35 MB). The k8s runner does not use it and simply
# ignores it.
COPY --from=docker.io/library/docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

RUN useradd -r -u 1000 -s /usr/sbin/nologin app \
    # HOME must exist and belong to the app user. `USER 1000` used to make Docker derive HOME
    # from the passwd entry; the entrypoint drops privileges with setpriv instead, which
    # preserves the environment and would leave HOME=/root — where uid 1000 cannot even stat
    # ~/.claude/.credentials.json, so startup died on PermissionError reading the credential
    # seed. ENV HOME below pins it; this creates it so anything writing there succeeds.
    && mkdir -p /home/app && chown 1000:1000 /home/app \
    # The event log, session registry, sealed credential store and drained egress audit all
    # live under /data. Create it OWNED BY THE RUNTIME USER: Config.__post_init__ tolerates a
    # failed mkdir (in-cluster the path is a mount), so without this the container starts
    # cleanly and then silently persists nothing. A named volume mounted here inherits this
    # ownership, so `docker run -v terrarium-data:/data` works without a manual chown.
    && mkdir -p /data && chown 1000:1000 /data
COPY --from=build /opt/terrarium /opt/terrarium
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh
WORKDIR /app
# No `USER 1000`. The entrypoint starts as root solely to join the mounted socket's group —
# whose GID is a host property and cannot be known at build time — and then drops to uid 1000
# with setpriv before exec'ing the app. The app itself never runs as root.
#
# Passing an explicit user (`docker run -u 1000`, or Kubernetes runAsUser) short-circuits that
# and execs directly, so anything already pinning the user keeps the old behaviour. A
# Kubernetes runAsNonRoot policy with no runAsUser will reject this image on the image config
# alone: set runAsUser: 1000 there. The k8s runner talks to the API and needs no socket, so it
# loses nothing by pinning the user.
ENV PATH=/opt/terrarium/bin:$PATH \
    HOME=/home/app \
    TERRA_HOST=0.0.0.0 \
    TERRA_PORT=8900 \
    TERRA_LOGS_DIR=/data/logs \
    TERRA_RUNTIME_DIR=/data/runtime \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# TERRA_RUNNER is deliberately NOT set: orchestrator/config.py owns the default, and
# pinning it here too meant one of the two files was always the stale answer.

EXPOSE 8900
# /readyz is 503 until rehydrate + orphan-reap finish, so `depends_on: service_healthy`
# waits for a control plane that has actually recovered its sessions rather than one that
# has merely bound the port. urllib rather than curl — the slim base has no curl, and
# adding one for a healthcheck is a package and a CVE surface for nothing.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8900/readyz', timeout=4).status==200 else 1)"]
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "terra"]
