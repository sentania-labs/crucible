# The one-click GitHub App (crucible#168, FDY-0127)

## Decision

The operator, 2026-09-27, on the GitHub page of v0.6.1: "What am i supposed to do here?
Should an app install be like 'Click the button' install the app? That's what happened for
chronicle. this is like - here's a bunch of options - have fun!" And on this dispatch:
"let's do a single dispatch to get the github app working like it works on chronicle
'click here to install the app'". ADR 0017's amendment of the same day records the shape
and corrects its earlier claim that the manifest flow needs a public DNS record.

## What changed

- The GitHub page leads with Create GitHub App: an App name (default `Hades-` and six hex
  characters) and an optional organization. The existing-App form (App ID and `.pem`) is
  behind the link "Already have a GitHub App?" (`/ui/github?existing=1`).
- Create records a start (`github_manifest_states`, migration 0026: the state's sha256,
  the administrator, 15 minutes) and returns a page that posts the manifest to GitHub's
  create page on its own (a Continue button if scripts are off). GitHub's web origin is
  derived from `github.api_base` (`https://github.com` for api.github.com, the host for
  GitHub Enterprise Server, the origin itself for the stand-in), so no new setting.
- `/ui/github/callback` spends the state, exchanges the code once
  (`RestGitHubApps.convert_manifest`, the transport's one unauthenticated call), and keeps
  the App through the same audited path as Connect GitHub (`github.keep`). It lands on
  the GitHub page, where Install on GitHub now sits right under the connection.
  `/ui/github/installed` is the manifest's `setup_url`: GitHub sends the browser there
  after an install, and it lands on the picker.
- Both returns bounce a cookieless (cross-site) arrival once through a same-site reload,
  because the session cookie is `SameSite=Strict`.
- `github.external_url` (a `provider_settings` row): Return address on the GitHub page,
  `GET`/`POST /v1/admin/github/external-url`, `crucible admin github external-url` and
  `set-external-url --url`. Empty means the browser's own `Origin`.
- The access log blanks `code` and `state` query values (`crucible/logs.py`).
- The stand-in (`tools/smoke/first_run_stubs.py`) serves github.com's half: the create
  page and its confirm button, the one-time conversion (a fresh RSA key per App), and the
  install page that redirects to `setup_url`.

The flow has no admin API or CLI verb: GitHub needs the operator's browser to post the
manifest and to confirm. The one new tunable, the external URL, has all three surfaces.

## Evidence

- Unit (`tests/unit/test_github_manifest.py`): the manifest's permissions, events, webhook
  and URLs; the default name; the web origin for github.com, GHES and the stand-in; the
  account and organization targets; the install link's origin check; the external URL's
  accepted and refused forms; the conversion call carrying no `Authorization` header,
  keeping no OAuth secret and never showing the key or the webhook secret in `repr`; a
  spent code's 404. `tests/unit/test_logs_and_time.py`: the access-log redaction.
- Integration (`tests/integration/test_first_run.py`, against the stand-in over loopback):
  `test_github_app_is_created_with_one_click_and_installed` walks Create (the exact
  manifest, the state stored only as its hash, the organization target, a refused login),
  the cookieless return (reload page, then sign-in, nothing exchanged), the signed-in
  return (one conversion; the Secret holds `app-id`, `app.pem`, `webhook.secret`), a
  reused, a wrong, an expired and another administrator's state (each refused, GitHub
  not asked again), Install and the return to the picker, and then checks that no page,
  answer or audit entry carries the key or the webhook secret.
  `test_the_return_address_setting_overrides_the_browsers` covers the setting on the API,
  the UI and the CLI's local mode; the remote CLI calls are in
  `test_the_cli_remote_mode_builds_the_first_run_calls`.
- Kind (`make first-run-kind`, finished 2026-09-27 at 10:53 PM): step 5 pressed Create
  GitHub App on the rendered page, posted the manifest to the stand-in through a second
  port forward, followed its redirect back, saw the cookieless reload page with nothing
  exchanged, finished the callback signed in (one conversion, `crucible-github-app`
  created and labelled by the service with `app-id`, `app.pem`, `webhook.secret`),
  refused a second return with the same state, and pressed Install on GitHub, landing on
  the picker. Steps 6 to 8 then ran on the App the flow created, the private checkout
  included.
- A real browser (headless Chrome, 2026-09-27 around 10:55 PM), Crucible on `localhost`
  and the stand-in on `127.0.0.1` so the redirect is cross-site: Create opened the
  stand-in's page filled in; its button came back through the reload page (the access log
  shows the callback answered 200, then 303 on the same-site reload) signed in and
  connected; Install came back to the picker the same way.
