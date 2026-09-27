# Private repository checkout (crucible#157, FDY-0124)

## Decision

The operator, 2026-09-27, on #157 ("Decision: support private GitHub repositories"): "3:
build it". ADR 0019 records the shape: a read-only installation token, minted for one
preparation step, handed to the preparer and the cache refresher and never to the worker,
revoked when the step ends. It amends ADR 0017 decision 5, which refused private
repositories in the picker.

## What changed

- `repositories.private` (migration 0025). The picker takes it from GitHub; the
  Repositories form has a checkbox, the admin API and `PUT /v1/repositories` take
  `"private": true`, and `crucible admin repository register` takes `--private`.
  Registration of a private repository mints the checkout token once and revokes it, so
  the App's refusal (not connected, no installation id, HTTP 404 or 422) is the
  registration's refusal, in plain words.
- The supervisor mints the token just before `prepare`
  (`crucible/application/checkout.py`), passes it as `prepare(spec, checkout_token=...)`,
  and revokes and empties it in a `finally`. A prepare the App cannot serve ends the
  attempt as `environment` with the reason.
- `AppAuthenticator.checkout_token` asks for `{"repositories": [<name>], "permissions":
  {"contents": "read"}}` and never caches; `revoke` is `DELETE /installation/token`.
- The preparer script (and the Kubernetes refresher's) gets the publisher's helper, bound
  to `github.credential_host`, only when a token is given; it drops the token and the
  helper right after the clone and again from an `EXIT` trap. Public repositories render
  exactly the script they did before.
- Docker: the preparer container gets the token on stdin onto its own tmpfs, as the
  publisher does. Kubernetes: a per-attempt Secret `checkout-<attempt>` mounted into the
  refresher and the preparer Jobs only, deleted before `prepare` returns; `discard` and
  `cleanup` retry a failed deletion. The preparer's and refresher's egress gains the
  credential host when it is not github.com.
- The picker marks an archived repository "archived: cannot take a pull request" instead
  of marking a private one "private: not supported yet".

No new tunable: `github.credential_host` already existed for the publisher and now also
binds the preparer's helper. The one new per-repository value, `private`, is on every
surface the registration is (API, CLI, UI).

## Evidence

- Unit (`tests/unit/test_private_checkout.py`): the mint request's scope and permissions
  and that it is not cached; revocation; the helper answering only `get` for https on the
  one host (seven cases); the rendered preparer run on this host removing the token and
  the helper after a clean prepare, a failed checkout and a failed clone, with nothing in
  the workspace holding the value; the Docker create requests (only the preparer has the
  tmpfs and stdin, no body carries the value, the worker has neither); the Kubernetes
  objects (the Secret mounted into the refresher and the preparer only, gone after a
  clean prepare and after both failure paths, the worker Pod never referencing it); a
  public repository prepared with no credential on both providers; the refusal of a
  private URL the helper would not answer.
- Integration (`tests/integration/test_private_checkout.py`, against
  `tools/smoke/first_run_stubs.py`): registration's one scoped mint and its revocation;
  the four refusals; the form, the admin API and 04's path accepting and refusing alike;
  the supervisor handing a fresh token to `prepare`, revoked and emptied after it, and
  none for a public repository; the refusal at prepare; and the rendered preparer
  cloning over real HTTPS from the git stand-in, which refuses an anonymous request, a
  token for another repository, a helper bound to another host, and the revoked token.
- Kind (`make first-run-kind`, 2026-09-27, 2:35 PM to 2:44 PM): step 8 registered
  `octo-lab/secret-plans` from the picker as private, prepared a script-harness task on
  it through the refresher and the preparer, and while the worker ran: the git stand-in
  logged an anonymous 401 and then 200s for a scoped token; every token the stand-in had
  minted was scoped to `secret-plans` with `contents: read` and revoked;
  `checkout-<attempt>` no longer existed; the worker Pod's spec named no checkout Secret
  and no token mount; and a search inside the worker for every minted token (files under
  `/crucible`, `/home/worker` and `/tmp`, its environment, the token path) found nothing.
  The cluster, its registry and the run's images were removed afterwards.

## Limits

- The Docker provider's side is proved with a stub daemon and by running the rendered
  script on the host; the Docker end-to-end tier was not run (FDY-0124's standing rules:
  its fixed subnet collides with concurrent workers).
- No live GitHub run: the live tier (`make e2e-github`) needs the operator's App key.
