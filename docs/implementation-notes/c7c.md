# C7c: the image manifest cannot drift from its sources

## Finding

C7a changed `images/script-harness/harness.sh` for the `login-stub` mode but
did not regenerate `images/manifest.env`. A local `make e2e` therefore selected
the old `script-harness-1.0.0-34cf5b56ca5a` image and the credential-mount test
failed with exit 70 because that image did not contain the new mode.

CI hid the drift because its e2e job runs `make e2e-image` first. That build
rewrote the manifest in the runner's checkout before `make e2e` read it, so CI
used the current image even though the committed pin was stale.

## Correction

`images/check-manifest.sh` executes the tag calculation already owned by
`images/build.sh` behind a no-build Docker shim, then compares every image tag
with the committed manifest. It reports every drifted image and its explicit
`images/build.sh <harness>` repair command. The check also requires one valid
digest entry per image.

`make lint` runs the check, and the CI lint job runs that same target. `make
e2e` also runs it before the test environment is prepared, so a stale local
pin is refused rather than rebuilt or run. Rebuilding remains the explicit
`make e2e-image` action.

The check also found that the C6d `build.sh` change had left the older `agy`,
`claude_code`, and `codex` pins stale. `hermes`, built during C6d, still
matched. The four rebuilt images are:

| Harness | Tag | OCI manifest digest |
|---|---|---|
| `agy` | `crucible-worker:agy-1.2.4-975aff4033e0` | `sha256:387801d1b35a1b13ab9aa111cc37e39a17c69d6c001126b3c4aeaa5e28e6dfc3` |
| `claude_code` | `crucible-worker:claude_code-2.1.273-1d43260eec11` | `sha256:b01332ea7658a6b4facd648d5b1073163e861979750d2615132e30908f45ab10` |
| `codex` | `crucible-worker:codex-0.153.4-8cc315ca7aab` | `sha256:37dfd22c86ac1468d49bf5b5fb67e7e3ff15d8e5ddba0dfd406af80c2e7f3ae4` |
| `script-harness` | `crucible-worker:script-harness-1.0.0-88cd22bf214a` | `sha256:b89653e6a541c04594b48533ce9b13fca44b9447d21e9094b4837ce4daea19be` |
