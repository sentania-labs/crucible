# Worker images resolve through crane (108)

## The decision

On 2026-09-24 the operator chose to replace the hand-written registry client
(`HttpRegistryClient`) with `crane`, go-containerregistry's CLI, rather than patch the
client: "Crane works for me." A widely used tool already handles each registry's
quirks. It is the v0.5.0 lesson again: use the standard tool for a third-party service,
because a custom client that needs a real-service proof job is the wrong design.

## What broke

On a real v0.5.3 deployment `GET /v1/admin/images` was empty, so no worker image could
be promoted and nothing launched. GHCR answers a config blob GET with a 307 to
`pkg-containers.githubusercontent.com`. The old client used a raw `HTTPSConnection`,
treated any status below 400 as success, and parsed the redirect's empty body as the
config ("returned something that is not JSON"). The only test that used it ran against
a local registry that never redirects.

## crane: version and pin

crane v0.22.1, released 2026-09-03 at 7:17 PM Central. The Dockerfile pins it by
version and by the SHA-256 of the release archive
(`go-containerregistry_Linux_x86_64.tar.gz`,
`0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0`, which matches the
release's own `checksums.txt`), checks the archive with `sha256sum -c` before
extracting, and fails the build on a mismatch. `tools/crane/fetch.sh` reads the same two
build args, so the unit, registry and kind tiers run the binary the image ships.

The service image grows by 11.9 MB (216,959,578 to 228,829,876 bytes, local builds of
e69829a and of this change).

## What was verified before building

With the real binary, before any code changed (2026-09-24, about 10:10 PM):

- `crane digest ghcr.io/sentania-labs/crucible-worker:0.5.3` returned
  `sha256:05f5be8e...0d96c`, the manifest's own digest, through GHCR's anonymous token.
- `crane config --platform linux/amd64` on the same reference returned the harness
  labels, through the 307, for a single-platform manifest.
- On `docker.io/library/busybox:1.36.1`, an OCI index, `crane digest` returned the
  index's digest and `crane config --platform linux/amd64` the amd64 image's config,
  which is what the old client recorded and read.
- `crane ls` listed every tag, following pagination.
- Against a local `registry:2` behind htpasswd, a call with an empty `DOCKER_CONFIG`
  failed `UNAUTHORIZED` and a call with a `DOCKER_CONFIG` holding the credential in the
  `auths` form of a pull Secret succeeded.

## How the adapter runs it

`CraneRegistryClient` (`crucible/adapters/execution/k8sregistry.py`) keeps the port the
provider already used (`resolve`, `list_tags`, `RegistryError`, an `auths` map the
provider fills from the image pull Secret).

- `resolve` runs `crane digest <ref>`, then `crane config --platform linux/amd64
  <repo>@<digest>`. Reading the config by the digest just resolved means a tag that
  moves between the two calls cannot pair one image's digest with another's labels.
- Each call gets a new directory from `mkdtemp` (0700) holding one `config.json` (0600,
  created exclusively) with at most the credential for the registry being read, and an
  empty `auths` when there is none, so crane never falls back to a config in the service
  user's home. The directory is removed in a `finally`, on success, failure, timeout and
  a missing binary alike. The credential is never on the command line.
- Each call is bounded by a timeout (20 seconds; the child is killed when it expires).
- A registry crane would read over plain HTTP from another host is refused (below).
- crane inherits the service's environment, so it trusts what the service trusts:
  `SSL_CERT_FILE` and the system store the lab CA is installed in.
- A failure is a `RegistryError` carrying the registry name and crane's own `Error:`
  line, with any credential value replaced by `[redacted]`.

## Differences from the old client worth knowing

- An index with no linux/amd64 entry is refused. The old client fell back to any linux
  entry; the lab runs one architecture and the contract names linux/amd64.
- crane refuses a redirect from a registry to a private or link-local IP literal (its
  SSRF guard, in `pkg/v1/remote/fetcher.go`). A redirect to a host name is followed.
  GHCR redirects to a name. A private registry that redirects blob downloads to a bare
  private IP would be refused; the stub registry in `tests/e2e/test_registry.py`
  redirects to `localhost` for this reason.
- crane falls back from HTTPS to plain HTTP for `localhost`, loopback, names under
  `.localhost` and RFC 1918 IP literals (`pkg/name/registry.go`, `Scheme`). The old
  client was HTTPS only, and the review round showed crane sending the pull credential
  in the clear to a private-IP listener. The adapter therefore refuses a registry named
  by an RFC 1918 address or a `.localhost` name before crane runs, and so before any
  credential is written. Loopback is still read: it never leaves the host, and the kind
  tier relies on it. `make deploy-kind` keeps its TLS registry, named by host name.
- `list_images` makes two crane calls per tag. Run one after another, a listing of the
  30 tags on GHCR on 2026-09-24 took 14.8 seconds, against the 15 seconds the harness
  and image endpoints wait for it. The provider now resolves six tags at a time
  (`LIST_IMAGES_CONCURRENCY`, a constant, not a setting), and the same listing took
  2.9 seconds.
- The review of PR 109 (Codex, 2026-09-24) found that when an endpoint gave up on a
  listing after 15 seconds, the crane calls already running carried on in Python's
  shared thread pool, the one every Kubernetes API call also uses, so repeated
  requests against a slow registry could stall unrelated cluster operations. Now:
  registry reads have their own pool of `LIST_IMAGES_CONCURRENCY` threads; one
  listing runs at a time and later callers wait on it rather than starting another;
  and a listing carries a deadline (`LIST_IMAGES_DEADLINE`, 12 seconds, a constant)
  that every crane call is clipped to, so the listing and every crane process in it
  end before the endpoint's 15 seconds. crane's own timeout kill does the stopping,
  and the `DOCKER_CONFIG` directory is removed on that path as on every other.

## Proof

- Unit: `tests/unit/test_kubernetes_registry.py`, against a stub `crane`.
- `make registry-check` (CI job `registry` on every branch push): the real crane
  against a stub registry that demands a password and 307s every blob GET to another
  host (proving the credential is not forwarded there), then an anonymous resolve of
  `ghcr.io/sentania-labs/crucible-worker:latest` with `tools/registry/check_published.py`.
- The release runs the same check inside the service image it has just built.
- `make e2e-kind` resolves the tier's registry tag through the same real crane.
