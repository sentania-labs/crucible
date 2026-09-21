# C7b: readable administration panels

## Result

All fifteen document sections that used the generic JSON branch now render as
labelled fields, nested groups, or tables. The shared renderer keeps every
non-secret leaf, translates booleans into operator words, and displays times in
America/Chicago. Empty collections say `none`. The generic `section.json` and
`tojson` paths are gone, so new document sections must choose an explicit
presentation.

The Images manifest named in the contract was already an explicit table in the
base revision. It remains a table and was included in live browser evidence.
The fifteenth actual JSON caller found in the base router was the Bootstrap
manifest detail.

## Section mapping

`test_all_fifteen_sections_preserve_real_service_output_shapes` obtains its
representative documents from the application services, repository migration
policy constants, and the domain records those services consume. It compares
the source and panel leaf counts, then checks that every resulting leaf is in
the rendered HTML.

| Page and section | Panel kind | Coverage |
|---|---|---|
| Status: Supervisor | labelled fields with nested groups | fifteen-section service-shape test and focused supervisor test |
| Status: Providers | labelled fields with nested groups | fifteen-section service-shape test using `providers_status` |
| Status: Task state | labelled fields with nested tables | fifteen-section service-shape test using `status.tasks` |
| Status: Pending wakes | labelled fields with nested tables | fifteen-section service-shape test using `status.wakes` |
| Routing: Active policy | labelled fields with nested groups and tables | fifteen-section service-shape test using the default policy |
| Routing: Routing policy | labelled fields with nested groups and tables | fifteen-section service-shape test using the verified routing policy |
| Routing: Pool exhaustion | table | fifteen-section service-shape test using `routing.list_exhaustions` |
| GitHub: App and repository connectivity | labelled fields with nested repository table | fifteen-section service-shape test using `github.status` |
| Tasks: Task state | labelled fields with nested tables | fifteen-section service-shape test using `status.tasks` |
| Wakes: Wakes | labelled fields with nested tables | fifteen-section service-shape test using `status.wakes` |
| Retention: Summary | labelled fields with nested recent-action table | fifteen-section service-shape test using `status.retention` |
| Audit: Next cursor | labelled field | fifteen-section service-shape test using `audit.tail` |
| Bootstrap: Imports | table with nested groups | fifteen-section service-shape test using `bootstrap.list_imports` |
| Bootstrap: Manifest | labelled fields with nested groups and tables | fifteen-section service-shape test using `bootstrap.show` |
| Worker log: Tail | explicit text | fifteen-section test plus dedicated preformatted-output and redaction tests |

The signed-in page walk checks every ordinary page for `<pre>` and JSON dump
patterns. Only the one-time token result and live worker log tail retain an
explicit text presentation.

## Safety and time behavior

Secret-shaped field names render `not displayed`; presence flags render
`present` or `absent`. URLs with user information or secret-shaped query keys
lose that sensitive portion. Every string also passes through the existing
secret-pattern redactor, including benignly named error and description
fields. The live worker log tail uses the same redactor. Nested list and map
cells recurse into panels, so they cannot fall back to a Python or JSON
representation.

The page context recursively converts datetime objects, standalone ISO values,
and ISO timestamps embedded in status text into a dated America/Chicago clock
value with the zone abbreviation. Tests assert the local value and absence of
the source UTC form.

## Live compose evidence

The final branch image, `sha256:0e9f6dbed4fd3a50779bd1e6205bfd98ef9f8f42b7ac6bb87491db2b78a2dfa0`,
ran on the operator host daemon as the isolated
`crucible-c7b` project, with application port 18082 and PostgreSQL port 15434.
The project's first network allocation overlapped an existing network, so the
isolated override was assigned unused worker and publication subnets and the
stack then started healthy. The first-run administrator signed in and every
changed page was opened in Chrome. Each committed image was inspected and no
token, credential, provider code, or authentication code is visible.

`make smoke` was first invoked without the isolated port override and could not
connect to port 8080. With `CRUCIBLE_PORT=18082`, smoke passed on the first live
iteration and again after the final image was force-recreated. The final run
registered the repository, submitted task `01M31V8PGR7SGNZ4X9P48XDTMG`, passed
twelve gates with one pending review gate, recorded the non-author review, and
accepted the task. Its automatic first-run UI walk was unavailable because the
one-time migration token is not reprinted; the authenticated Chrome walk and
screenshots exercised the final image directly.

## Verification

| Tier | Result |
|---|---|
| `make lint` | Passed: formatting, Ruff, mypy across 237 files, and all three import contracts. |
| `make test` | Passed before the PR: 622 unit tests in 5.69 seconds and 314 integration tests in 368.28 seconds, with three dependency deprecation warnings. After the automatic-review fixes, the final unit tier passed 623 tests in 5.63 seconds and the focused administration unit and integration slice passed 47 tests. |
| `make scan` | Passed: no secret patterns in 3.70 MB of tracked content or the branch commits. |
| `make e2e` | Limited by the local rootless container tier. First run: 8 passed, 9 failed, 12 deselected. Clean rerun: 12 passed, 5 failed, 12 deselected. The remaining failures were an identity entrypoint exit 70 without its identity bundle, expected timeout or stalled states observed as lost after worker exit, a cancellation case with no worker container to inspect, and absent V9 verifier evidence. None exercised the administration renderer. |
| `make up` | Passed after assigning unused isolated subnets. Application, migration, proxy, and PostgreSQL containers became healthy or completed successfully. |
| `make smoke` | Passed on the isolated port after the initial default-port invocation failed to connect. |

## Screenshots

| Page | Screenshot |
|---|---|
| Status | `docs/implementation-notes/c7b/status.png` |
| Routing | `docs/implementation-notes/c7b/routing.png` |
| GitHub | `docs/implementation-notes/c7b/github.png` |
| Images | `docs/implementation-notes/c7b/images.png` |
| Tasks | `docs/implementation-notes/c7b/tasks.png` |
| Wakes | `docs/implementation-notes/c7b/wakes.png` |
| Retention | `docs/implementation-notes/c7b/retention.png` |
| Audit | `docs/implementation-notes/c7b/audit.png` |
| Bootstrap | `docs/implementation-notes/c7b/bootstrap.png` |
| Workers | `docs/implementation-notes/c7b/workers.png` |
| Worker tail | `docs/implementation-notes/c7b/worker-tail.png` |

## Review findings

The single required non-author adversarial review found four high-severity and
two medium-severity defects. All were corrected during that review round. No
second round was started. The reviewer's combined administration test slice
passed 46 tests after the dispositions.

| Severity | Finding | Disposition |
|---|---|---|
| high | A list-valued cell inside a table row could render a nested mapping through its Python representation, dropping the readable-panel guarantee and exposing a secret-shaped nested field. | Nested collection cells now carry recursive panels. A regression test uses a list containing a secret-bearing mapping and checks readability, suppression, and absence of dump syntax. |
| high | Redaction depended on the field name, so a credential-shaped value in a benign field such as a provider error or policy description could render. | Every scalar string now passes through the domain secret-pattern redactor. The regression test places a credential-shaped value in `description`. |
| high | Coded URL handling did not recognize fragment parameters or percent-encoded parameter names. | Query and fragment parameter names are decoded before inspection, and a sensitive parameter removes both portions. Tests cover fragment and percent-encoded keys. |
| high | Scalar cells in existing hand-built tables bypassed the document renderer's sanitization, including stored repository URLs with user information. | Every ordinary table scalar now uses the shared safe-value function with column context. Tests cover URL user information and a credential-shaped description. |
| medium | Empty dictionaries nested in list rows disappeared during flattening. | Empty maps now remain explicit empty panels that render `none`; the nested-list test covers the provider-check shape. |
| medium | A dotted table path caused the permitted operational `fenced_token` counter to be treated as a secret. | Secret-name matching now applies the operational exception to the final path component. The nested-list test retains the counter. |

The repository's single automatic PR review found three medium-priority
defects. All were corrected on the same PR without asking for another review
round.

| Finding | Disposition |
|---|---|
| The broad `code` secret-name rule also suppressed operational `exit_code` values. | Exit, error, HTTP, and status codes are explicit non-secret operational fields. Nested rendering now proves exit code 70 remains visible while authentication codes stay suppressed. |
| A malformed stored value containing `://` could raise during URL parsing and return a 500 page. | URL parsing is guarded and renders `invalid URL` without echoing the rejected value. |
| An invalid timestamp-shaped substring could raise inside embedded-time replacement. | The replacement leaves invalid timestamp-shaped text unchanged, and a regression proves the page renderer does not fail. |

## Follow-ups, limitations, and risks

No panel requested a field that its application service does not provide.

The e2e failures above are limitations of the available local container tier,
not accepted product behavior. The renderer is recursive and intentionally
generic, so a future service shape remains visible, but unusually deep or wide
documents may still need a purpose-built presentation for faster operator
scanning. Secret redaction is defense in depth around the existing rule that
secret values must not enter operational documents. Secret field detection
also normalizes camelCase names, while current service contracts continue to
use snake_case.
