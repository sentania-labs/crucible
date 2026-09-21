# C7e: release candidate image classification

## Result

The release pull guard and image identity check now derive their candidate
service set from `docker compose --profile "*" config --format json`. A service
is a candidate only when its resolved `image` equals `CRUCIBLE_IMAGE`. Candidate
services are never passed to `docker compose pull`, and every candidate service
container is checked against the locally built image ID after boot. The check
uses all containers, including exited one-shot services.

The classify-only Make target is also a CI compose-smoke step. Its unit test
runs against the repository's `compose.yaml`, asserts that the current
candidate set is exactly `credential-init`, `crucible`, and `migrate`, and
proves a build-only service with no resolved image fails by name.

## Local release proof

Run on the host daemon in the isolated `fdy0059` Compose project on 2026-09-21.
The override removed the PostgreSQL host port and assigned application port
18059, worker subnet `10.90.59.0/24`, publisher subnet `10.90.60.0/24`, and
project-specific networks and volumes. The image was built locally with
`VERSION=0.3.1`; no Git tag or image was pushed.

The material transcript was:

```text
$ docker build --build-arg VERSION=0.3.1 --build-arg REVISION=9e897ab \
    -t ghcr.io/sentania-labs/crucible:0.3.1 .
=> writing image sha256:1d8bb6965c13d3d102d187af0ff38b3ce54f67aca70cd2e7344e94db5a5a70f0
=> naming to ghcr.io/sentania-labs/crucible:0.3.1

$ CRUCIBLE_IMAGE=ghcr.io/sentania-labs/crucible:0.3.1 make release-images-pull
candidate credential-init ghcr.io/sentania-labs/crucible:0.3.1
candidate crucible ghcr.io/sentania-labs/crucible:0.3.1
candidate migrate ghcr.io/sentania-labs/crucible:0.3.1
supporting docker-socket-proxy tecnativa/docker-socket-proxy@sha256:1f5038b5...
supporting egress-proxy ubuntu/squid@sha256:6a097f68...
supporting postgres postgres:16@sha256:f1c3376c...
supporting publish-proxy ubuntu/squid@sha256:6a097f68...
pulling supporting image for docker-socket-proxy
pulling supporting image for egress-proxy
pulling supporting image for postgres
pulling supporting image for publish-proxy

$ CRUCIBLE_IMAGE=ghcr.io/sentania-labs/crucible:0.3.1 \
    docker compose ... up -d --wait --pull never
Container fdy0059-credential-init-1 Exited
Container fdy0059-migrate-1 Exited
Container fdy0059-crucible-1 Healthy

$ CRUCIBLE_IMAGE=ghcr.io/sentania-labs/crucible:0.3.1 make release-images-verify
credential-init container 5af38f6489c4 image: sha256:1d8bb6965c13d3d102d187af0ff38b3ce54f67aca70cd2e7344e94db5a5a70f0
crucible container 37d829b68f67 image: sha256:1d8bb6965c13d3d102d187af0ff38b3ce54f67aca70cd2e7344e94db5a5a70f0
migrate container de887f7e0e35 image: sha256:1d8bb6965c13d3d102d187af0ff38b3ce54f67aca70cd2e7344e94db5a5a70f0
all candidate-image service containers use the freshly built image

$ CRUCIBLE_SMOKE_EXPECT_VERSION=0.3.1 make smoke
health: {"schema_version": "1.0", "status": "ok", "version": "0.3.1"}
reported version matches 0.3.1
state: awaiting_internal_review
state: awaiting_acceptance
state: accepted
compose smoke passed
```

The required normal compose path also passed in the same isolated project:
`make up` reached healthy and `make smoke` drove task
`01M32MPRW0C9MK84MAXT8CZS1W` to `accepted`.
