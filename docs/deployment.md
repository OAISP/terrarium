---
title: Deployment
nav_order: 5
---

# Deployment
{: .no_toc }

1. TOC
{:toc}

---

## Images

Published to Docker Hub on every release, tagged with the exact version (`0.2.3`), the minor
line (`0.2`), and `latest`.

| Image | Role |
|---|---|
| `k3scat/terrarium-orchestrator` | Control plane, `:8900`. |
| `k3scat/terrarium-console` | Next.js console, `:3737`. |
| `k3scat/terrarium-sandbox` | Hardened agent sandbox including Warden. Launched per session by the orchestrator, never run directly. |

## Docker

```yaml
# compose.yaml
services:
  orchestrator:
    image: k3scat/terrarium-orchestrator:0.2
    environment:
      TERRA_RUNNER: docker
      TERRA_HOST: 0.0.0.0
      TERRA_TOKEN: ${TERRA_TOKEN:?set a token}
      TERRA_KEK: ${TERRA_KEK:?set a key-encryption key}
      TERRA_IMAGE: k3scat/terrarium-sandbox:0.2
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - terrarium-data:/data
    ports: ["8900:8900"]

  console:
    image: k3scat/terrarium-console:0.2
    environment:
      TERRA_URL: http://orchestrator:8900
      TERRA_TOKEN: ${TERRA_TOKEN}
    ports: ["3737:3737"]
    depends_on: [orchestrator]

volumes:
  terrarium-data:
```

{: .warning }
> Mounting the Docker socket gives the orchestrator container root-equivalent control of the
> host. That is inherent to spawning sibling containers, and it is why the orchestrator — not
> the sandbox — holds it. Keep the API behind a token and off the internet.

## Single VPS with shunt

[shunt](https://github.com/OAISP/shunt) deploys Docker services to one host over ssh. A ready
manifest is at [`shunt.example.toml`](https://github.com/OAISP/terrarium/blob/main/shunt.example.toml); it
pulls released images from Docker Hub, so you deploy exactly what CI built. Copy it to
`shunt.toml` (gitignored, so your host stays out of version control), set `host`, fill in
`secrets/prod.env`, then:

```sh
shunt validate && shunt audit && shunt plan
shunt up
```

Three things about the manifest are worth knowing:

- **Sandboxes are not services.** The manifest declares the orchestrator and console only;
  the orchestrator creates sandboxes itself. shunt leaves them alone and reports them as
  orphaned, which is correct — but it also means shunt never pulls the sandbox image. Do that
  once per release: `docker pull docker.io/k3scat/terrarium-sandbox:latest`.
- **The Docker socket is all you mount.** The entrypoint joins the socket's group at startup
  and drops back to uid 1000 before the app runs. No host-side permission setup.
- **Secrets are mounted as files** (`mode = "file"`), keeping the admin token out of
  `docker inspect`. Terrarium reads `/run/secrets/<NAME>` for `TERRA_TOKEN`, `TERRA_KEK` and
  `ANTHROPIC_API_KEY` — the same convention Compose and Swarm use.

### Behind nginx

A ready config is at
[`deploy/nginx/terrarium.conf`](https://github.com/OAISP/terrarium/blob/main/deploy/nginx/terrarium.conf).
Only the console is proxied; the orchestrator is the admin API and should not be routable from
the internet.

Four settings are load-bearing:

| Setting | Without it |
|---|---|
| TLS | The auth cookie is `Secure`, and browsers discard a Secure cookie sent over plain `http://` except on localhost. Login appears to succeed and leaves you logged out, with nothing in any log. |
| `X-Forwarded-Proto`, `X-Forwarded-Host` | The console believes it is serving `http://` while the browser sends an `https://` Origin. Its CSRF check compares the two and rejects every mutation, including login, as `cross_site_request`. |
| `client_max_body_size 26m` | Workspace uploads fail at nginx with a 413 the app never sees. The orchestrator accepts 25 MiB; nginx defaults to 1 MiB. |
| `proxy_buffering off` | nginx holds SSE events until a buffer fills, so the live transcript arrives in bursts or not at all. |

{: .note }
> As the only operator, skip nginx. Bind the console to loopback
> (`publish = ["127.0.0.1:3737:3737"]`) and use an ssh tunnel:
> `ssh -L 3737:127.0.0.1:3737 you@your-vps`. localhost is exempt from the Secure-cookie rule,
> and the admin UI is never exposed.

## Kubernetes

Set `TERRA_RUNNER=k8s` and the orchestrator spawns sandbox Pods through the Kubernetes API; no
Docker daemon in-cluster. Warden runs as a native sidecar (an init-container with
`restartPolicy: Always`), so it is up and has written its CA before the worker starts.

{: .note }
> No reference manifests ship with this release. Terrarium was extracted from a deployment
> whose chart was specific to one cluster. Bring your own; below is the contract they must
> satisfy.

The orchestrator needs:

- A **ServiceAccount** with RBAC in its namespace for `pods`, `pods/attach`, `pods/exec`,
  `persistentvolumeclaims`, `secrets` and `configmaps` (`get`, `list`, `create`, `patch`,
  `delete`).
- A **ReadWriteOnce PVC** at `/data`. Single-writer, so run one replica with
  `strategy: Recreate`.
- **Secrets** for `TERRA_TOKEN` and `TERRA_KEK`, plus `ANTHROPIC_API_KEY` on the key path.
- `TERRA_IMAGE` pointing at the sandbox image, and a pull secret for a private registry.
- `runAsUser: 1000` if you enforce `runAsNonRoot`. The image entrypoint starts as root to
  adjust to a mounted Docker socket, which the k8s runner does not use.

Storage sizing is tunable per session: `TERRA_K8S_MEMORY_SIZE`, `TERRA_K8S_WORKSPACE_SIZE`,
`TERRA_K8S_MEMORY_EMPTYDIR_SIZE`, `TERRA_K8S_AUDIT_SIZE`.

Kubernetes Secrets are base64-encoded API objects, not encrypted storage. Enable encryption at
rest for the datastore and limit RBAC on sandbox Secrets to the Terrarium service account.

**Memory mode** is the biggest lever on launch latency: the per-agent RWO PVC costs roughly
11s of volume attach per launch. The default `synced` mode skips the mount. See
[Configuration]({% link configuration.md %}#memory).

## Limits

32 live sessions globally and 4 per agent; event and egress logs 256 MiB per session. Crossing
a log limit terminates the producer rather than truncating evidence. All tunable — see
[Configuration]({% link configuration.md %}).

## Troubleshooting

### `permission denied` on the Docker socket

Sessions fail with `warden sidecar failed to start`, and the orchestrator log shows:

```
docker: permission denied while trying to connect to the Docker daemon socket
at unix:///var/run/docker.sock
```

The socket is `root:docker 0660` and the `docker` GID differs per host, so the image cannot
know it at build time. From **0.2.3** the entrypoint reads the mounted socket's GID at startup,
joins that group, and drops to uid 1000 — mounting the socket is all that is required.

On **0.2.2 and earlier**, either upgrade or grant the uid directly on the host:

```sh
setfacl -m u:1000:rw /var/run/docker.sock
```

That does not survive a Docker daemon restart. To persist it:

```sh
printf '[Service]\nExecStartPost=-/usr/bin/setfacl -m u:1000:rw /var/run/docker.sock\n' \
  | sudo tee /etc/systemd/system/docker.service.d/terrarium-acl.conf
sudo systemctl daemon-reload
```

{: .note }
> Adding your own account to the `docker` group does not fix this. Group membership is
> resolved from the container's `/etc/group`, never the host's, so it changes what your shell
> can do and nothing about the container.

Check what the process actually has — the socket's GID must appear:

```sh
docker exec terrarium-orchestrator sh -c 'grep Groups /proc/1/status'
```

If the socket is owned `root:root`, the entrypoint refuses to join GID 0 and says so on
stderr. Run `chgrp docker` on the socket instead.

### `cross_site_request` on login

The reverse proxy is not forwarding `X-Forwarded-Proto` and `X-Forwarded-Host`. See
[Behind nginx](#behind-nginx).

### Sessions terminate after a day or two of idling

Fixed in 0.2.x. Older builds treated the first end-of-stream as death; the pump now probes the
sandbox and reattaches. See
[Architecture]({% link architecture.md %}#stream-loss-versus-session-death) for recovering a
stranded session.
