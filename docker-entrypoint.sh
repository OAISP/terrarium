#!/bin/sh
# Reconcile the container's group membership with the mounted Docker socket, then drop
# privileges and exec the app.
#
# Why this exists: with TERRA_RUNNER=docker the orchestrator drives sandboxes through the
# host's socket, which is root:docker 0660 on every mainstream distro. The docker GID is a
# property of the HOST and differs between them (988, 999, 119, ...), so it cannot be baked
# into the image. Group membership is resolved from the CONTAINER's /etc/group, so a user
# being in `docker` on the host has no effect inside — the process gets one group, `app`,
# matches neither owner nor group on the socket, and every session dies at
# "permission denied while trying to connect to the Docker daemon socket".
#
# The alternative is making each operator run `setfacl -m u:1000:rw /var/run/docker.sock` on
# the host and re-apply it after every daemon restart. That is real setup work, it is silent
# when forgotten, and it fails at the first session rather than at deploy time.
#
# So: start as root, read the socket's actual GID, join it, then drop to uid 1000 before the
# app ever runs. Root exists for the few syscalls above and is gone by exec.
set -e

SOCK="${DOCKER_SOCKET:-/var/run/docker.sock}"

# Started with an explicit non-root user (`docker run -u`, or Kubernetes runAsUser /
# runAsNonRoot): we have no privilege to adjust anything and none is wanted. Exec straight
# through, which is exactly the pre-existing behaviour. The k8s runner needs no socket, so
# this path stays correct there.
if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

if [ -S "$SOCK" ]; then
    SOCK_GID="$(stat -c '%g' "$SOCK")"
    if [ "$SOCK_GID" = "0" ]; then
        # A root:root socket would force us to grant gid 0, which carries far more than
        # docker access. Refuse and say so: the operator wants `chgrp docker` on the host,
        # not a container quietly running with the root group.
        echo "terrarium: $SOCK is group-owned by root (gid 0); refusing to join gid 0." >&2
        echo "terrarium: chgrp the socket to a docker group on the host, or grant uid 1000" >&2
        echo "terrarium: directly with: setfacl -m u:1000:rw $SOCK" >&2
    elif [ "$SOCK_GID" != "$(id -g app)" ]; then
        # Reuse an existing group with that GID if the base image already has one, otherwise
        # create it. Creating a duplicate GID would work for the kernel but leaves getent
        # returning whichever entry it finds first, which makes this hard to debug later.
        SOCK_GROUP="$(getent group "$SOCK_GID" | cut -d: -f1)"
        if [ -z "$SOCK_GROUP" ]; then
            SOCK_GROUP=dockerhost
            groupadd -g "$SOCK_GID" "$SOCK_GROUP"
        fi
        usermod -aG "$SOCK_GROUP" app
        echo "terrarium: joined group $SOCK_GROUP (gid $SOCK_GID) for $SOCK" >&2
    fi
fi

# --init-groups rebuilds the supplementary set from /etc/group, which is what picks up the
# group added above. setpriv is already in the base image; gosu would be a package for this
# one call. No --no-new-privs: the app shells out to the docker client, which needs to keep
# the groups it inherits.
exec setpriv --reuid=app --regid=app --init-groups -- "$@"
