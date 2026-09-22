# C7f: release boot follows the shared Compose path

## Result

The release workflow now calls `make up` with
`COMPOSE_UP_FLAGS="--pull never --no-build"`. The target keeps its normal
`--build` default for local work and CI, while the release prevents Compose
from pulling or building over its versioned candidate image. Because `make up`
depends on `preflight` and `proxy-config`, both generated Squid configurations
exist before Compose creates either proxy. Candidate classification, the
supporting-image pull, and post-boot image-ID verification are unchanged.

`tests/unit/test_release_boot_path.py` reads both workflow files. It requires
CI to use `make up`, requires release to use that target with `--pull never`
and `--no-build`, and rejects a direct `docker compose up` anywhere in the
release workflow.

## Clean-tree reproduction and fixed proof

Run on the rootless host daemon in isolated Compose projects on September 21,
2026. A clean clone at `884540a` had no `var/egress` directory. The candidate
was built locally as `ghcr.io/sentania-labs/crucible:0.3.1`; no tag or image was
pushed.

The unchanged old release sequence reproduced the failure:

```text
$ CRUCIBLE_IMAGE=ghcr.io/sentania-labs/crucible:0.3.1 make release-images-pull
candidate credential-init ghcr.io/sentania-labs/crucible:0.3.1
candidate crucible ghcr.io/sentania-labs/crucible:0.3.1
candidate migrate ghcr.io/sentania-labs/crucible:0.3.1
pulling supporting image for docker-socket-proxy
pulling supporting image for egress-proxy
pulling supporting image for postgres
pulling supporting image for publish-proxy

$ docker compose up -d --wait --pull never
Error response from daemon: error mounting ".../var/egress/squid-publish.conf"
to rootfs at "/etc/squid/squid.conf": not a directory
```

From the same clean state, the fixed workflow sequence passed:

```text
$ CRUCIBLE_IMAGE=ghcr.io/sentania-labs/crucible:0.3.1 \
    make up COMPOSE_UP_FLAGS="--pull never --no-build"
wrote var/egress/squid.conf from remote hosts and enabled local routing entries
wrote var/egress/squid-publish.conf with 3 allowed hostname(s)
Container fdy0061nobuild-crucible-1 Healthy

$ CRUCIBLE_IMAGE=ghcr.io/sentania-labs/crucible:0.3.1 make release-images-verify
credential-init container ... image: sha256:...
crucible container ... image: sha256:...
migrate container ... image: sha256:...
all candidate-image service containers use the freshly built image

$ CRUCIBLE_SMOKE_EXPECT_VERSION=0.3.1 make smoke
reported version matches 0.3.1
state: accepted
compose smoke passed
```

The normal path also passed in its isolated project: `make up` reached healthy
and `make smoke` reached `accepted`.
