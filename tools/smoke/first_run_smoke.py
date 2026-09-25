#!/usr/bin/env python3
"""The first-run path on a disposable kind cluster (crucible#119, #120, #121, #123, #79).

`make first-run-kind` runs this after `tools/kind/deploy-kind.sh` has brought the
deployment manifests up from nothing. It does what an operator does on a fresh deploy,
through the deployed API and the rendered UI, against stand-ins for the two outside
services (tools/smoke/first_run_stubs.py, run as a Pod in `crucible-stubs`):

1. Status names the real missing steps for Hermes and lists no test fixture;
2. the gateway URL and key are set in one step and tested, in plain words;
3. a model is picked from the gateway's own list, which writes a routing version;
4. the Kubernetes egress selectors name the in-cluster gateway, and the combined worker
   image is promoted;
5. the GitHub App is connected with a throwaway key made for this run; the service
   creates `crucible-github-app` in `crucible`, and the mounted copy appears in the api
   Pod readable by its user (the #79 fsGroup question);
6. a repository is picked from the stand-in installation;
7. Status reaches "ready" for Hermes, in the document and on the rendered page.

Nothing here calls the real GitHub API or a real model. The throwaway key is generated
in memory, sent once in a request body, and never written to disk or printed.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import importlib.util
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
STUB_NAMESPACE = "crucible-stubs"
STUB_URL = f"http://crucible-stubs.{STUB_NAMESPACE}.svc.cluster.local:8080"
APP_ID = 4242
APP_SLUG = "crucible-kind"
MODELS = ["kind-fast", "kind-large"]
REPOSITORY = "octo-lab/widgets"


def _smoke_module() -> Any:
    """The deploy smoke's port forward, request, kubectl and token helpers, reused."""
    spec = importlib.util.spec_from_file_location("kubernetes_smoke", HERE / "kubernetes_smoke.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kubernetes_smoke"] = module
    spec.loader.exec_module(module)
    return module


KS = _smoke_module()
log = KS.log
request = KS.request
kubectl = KS.kubectl
SmokeError = KS.SmokeError


def step(title: str) -> None:
    log(f"\n=== {title}")


def show(label: str, value: Any) -> None:
    log(f"{label}: {json.dumps(value, indent=2, sort_keys=True)}")


def app_key() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def deploy_stubs(image: str, config: dict[str, Any]) -> None:
    """The stand-ins as one Pod behind one Service, in their own namespace so the workers'
    egress selector can name them (a selector may not name `crucible`)."""
    script = (HERE / "first_run_stubs.py").read_text(encoding="utf-8")
    objects = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": STUB_NAMESPACE}},
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "crucible-stubs", "namespace": STUB_NAMESPACE},
            "data": {"first_run_stubs.py": script, "config.json": json.dumps(config)},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "crucible-stubs", "namespace": STUB_NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "crucible-stubs"}},
                "template": {
                    "metadata": {"labels": {"app": "crucible-stubs"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "stubs",
                                "image": image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python",
                                    "/stubs/first_run_stubs.py",
                                    "--config",
                                    "/stubs/config.json",
                                    "--port",
                                    "8080",
                                ],
                                "ports": [{"containerPort": 8080}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/health/readiness", "port": 8080},
                                    "periodSeconds": 2,
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [{"name": "stubs", "mountPath": "/stubs"}],
                                "resources": {
                                    "requests": {"cpu": "20m", "memory": "64Mi"},
                                    "limits": {"cpu": "500m", "memory": "256Mi"},
                                },
                            }
                        ],
                        "volumes": [{"name": "stubs", "configMap": {"name": "crucible-stubs"}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "crucible-stubs", "namespace": STUB_NAMESPACE},
            "spec": {
                "selector": {"app": "crucible-stubs"},
                "ports": [{"port": 8080, "targetPort": 8080}],
            },
        },
    ]
    manifest = Path(os.environ.get("TMPDIR", "/tmp")) / f"crucible-stubs-{os.getpid()}.json"
    manifest.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))
    try:
        kubectl(["apply", "-f", str(manifest)])
    finally:
        manifest.unlink(missing_ok=True)
    kubectl(
        ["-n", STUB_NAMESPACE, "rollout", "status", "deployment/crucible-stubs", "--timeout=180s"]
    )
    log(f"the stand-ins answer at {STUB_URL}")


def readiness(base_url: str, token: str) -> dict[str, Any]:
    document = request("GET", f"{base_url}/v1/admin/status", token=token)
    return dict(document["readiness"])


def hermes_of(document: dict[str, Any]) -> dict[str, Any]:
    return next(h for h in document["harnesses"] if h["name"] == "hermes")


def gateway(base_url: str, token: str, key: str) -> None:
    step("2. set the gateway URL and key in one step, and test both (#119)")
    endpoint = f"{STUB_URL}/v1"
    result = request(
        "POST",
        f"{base_url}/v1/admin/gateway",
        token=token,
        body={"reason": "first-run kind proof", "endpoint_url": endpoint, "api_key": key},
    )
    show("test", result["test"])
    expected = f"Gateway {endpoint} reachable, key accepted, {len(MODELS)} models."
    if result["test"]["summary"] != expected or not result["test"]["passed"]:
        raise SmokeError(f"the gateway test said {result['test']!r}, not {expected!r}")
    if key in json.dumps(result):
        raise SmokeError("the gateway answer carried the key")

    step("3. pick a model from what the key can see (#121)")
    listing = request("GET", f"{base_url}/v1/admin/gateway/models", token=token)
    show(
        "models",
        [{k: r[k] for k in ("id", "offered", "in_policy", "note")} for r in listing["models"]],
    )
    offered = [row["id"] for row in listing["models"] if row["offered"]]
    if offered != MODELS:
        raise SmokeError(f"the gateway listed {offered}, not {MODELS}")
    saved = request(
        "POST",
        f"{base_url}/v1/admin/gateway/models",
        token=token,
        body={
            "reason": "first-run kind proof",
            "models": [{"id": MODELS[0], "enabled": True, "enable_thinking": False}],
        },
    )
    show("saved", {k: saved[k] for k in ("routing_policy", "enabled", "added")})


def egress_and_image(base_url: str, token: str) -> None:
    step("4. name the in-cluster gateway for the workers' egress, promote the worker image")
    request(
        "POST",
        f"{base_url}/v1/admin/kubernetes/egress",
        token=token,
        body={
            "reason": "first-run kind proof: the gateway runs in the cluster",
            "dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
            "local_endpoint": {
                "namespace": STUB_NAMESPACE,
                "pod_labels": {"app": "crucible-stubs"},
                "port": 8080,
            },
        },
    )
    deadline = time.monotonic() + 180
    while True:
        listing = request("GET", f"{base_url}/v1/admin/images", token=token)
        items = [i for i in listing["items"] if "hermes" in (i.get("harnesses") or {})]
        if items:
            break
        if time.monotonic() > deadline:
            raise SmokeError(f"no image carrying hermes is visible: {json.dumps(listing)[:800]}")
        time.sleep(3)
    image = items[0]
    request(
        "POST",
        f"{base_url}/v1/admin/images/{image['digest']}/promote",
        token=token,
        body={"reason": "first-run kind proof: the combined worker image"},
    )
    log(f"promoted {image['reference']} ({image['digest']}), carrying {image['harnesses']}")


def github(base_url: str, token: str, private_key: str) -> None:
    step("5. connect the GitHub App; the service creates its own Secret (#120, #79)")
    before = request("GET", f"{base_url}/v1/admin/github", token=token)
    show("before", {k: before[k] for k in ("configured", "key_present", "stored_in")})
    connected = request(
        "POST",
        f"{base_url}/v1/admin/github/app",
        token=token,
        body={"reason": "first-run kind proof", "app_id": APP_ID, "private_key": private_key},
    )
    show(
        "connected",
        {
            k: connected[k]
            for k in ("configured", "app_id", "key_fingerprint", "stored_in", "install_url")
        },
    )
    if connected["install_url"] != f"https://github.com/apps/{APP_SLUG}/installations/new":
        raise SmokeError(f"the install link is {connected['install_url']!r}")
    if "PRIVATE KEY" in json.dumps(connected):
        raise SmokeError("the connect answer carried the key")
    secret = json.loads(
        kubectl(
            ["-n", "crucible", "get", "secret", "crucible-github-app", "-o", "json"], redact=True
        )
    )
    labels = secret["metadata"].get("labels") or {}
    show(
        "the Secret (keys and labels only)",
        {"keys": sorted(secret.get("data") or {}), "labels": labels},
    )
    if labels.get("app.kubernetes.io/managed-by") != "crucible":
        raise SmokeError("crucible-github-app is not labelled as the service's own")
    stored_id = base64.b64decode(secret["data"]["app-id"]).decode()
    if stored_id != str(APP_ID):
        raise SmokeError(f"the Secret holds App id {stored_id}")

    # #79: the mounted copy, as the api's own user sees it. The kubelet projects a new
    # Secret into an optional volume on its next sync, so this waits for it.
    probe = (
        "import os, stat; p = '/var/lib/crucible/credentials/github/app.pem'; "
        "s = os.stat(p); print(oct(stat.S_IMODE(s.st_mode)), os.getuid(), s.st_uid, "
        "s.st_gid, os.access(p, os.R_OK), len(open(p, 'rb').read()) > 0)"
    )
    deadline = time.monotonic() + 180
    while True:
        seen = kubectl(
            ["-n", "crucible", "exec", "deployment/crucible-api", "--", "python", "-c", probe],
            check=False,
        ).strip()
        if seen.endswith("True True"):
            log(
                "api Pod, mounted app.pem (mode, process uid, file uid, gid, readable, "
                f"non-empty): {seen}"
            )
            break
        if time.monotonic() > deadline:
            raise SmokeError(f"the mounted app.pem never became readable in the api Pod: {seen!r}")
        time.sleep(5)

    step("6. pick a repository from the installation (#120)")
    picker = request("GET", f"{base_url}/v1/admin/github/installations", token=token)
    show(
        "installations",
        [
            {
                "account": i["account"],
                "id": i["id"],
                "repositories": [r["full_name"] for r in i["repositories"]],
            }
            for i in picker["installations"]
        ],
    )
    installation = picker["installations"][0]
    registered = request(
        "POST",
        f"{base_url}/v1/admin/github/repositories",
        token=token,
        body={
            "reason": "first-run kind proof",
            "installation_id": installation["id"],
            "repository": REPOSITORY,
            "attested_all_prs": True,
        },
    )
    show("registered", registered)
    if (
        registered["default_branch"] != "trunk"
        or registered["installation_id"] != installation["id"]
    ):
        raise SmokeError("the registration did not take GitHub's default branch and installation")


def sign_in(base_url: str) -> Any:
    """The first-run administrator from the migration Job's framed block, as the operator
    would; never printed."""
    logs = kubectl(["-n", "crucible", "logs", "job/crucible-migrate"], redact=True, check=False)
    match = re.search(r"\bcru_[A-Z0-9]{26}\.[A-Za-z0-9_-]+\b", logs)
    if match is None:
        raise SmokeError("the migration log has no one-time token")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{base_url}/ui/sign-in", timeout=60) as response:
        page = response.read().decode()
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page)
    assert csrf is not None
    body = urllib.parse.urlencode({"csrf": csrf.group(1), "token": match.group(0), "next": "/ui"})
    post = urllib.request.Request(
        f"{base_url}/ui/sign-in",
        data=body.encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(post, timeout=120):
        pass
    return opener


def page_text(opener: Any, url: str) -> str:
    with opener.open(url, timeout=180) as response:
        html = response.read().decode("utf-8", "replace")
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def smoke(stub_image: str) -> None:
    if not os.environ.get("KUBECONFIG"):
        raise SmokeError("KUBECONFIG must name the disposable cluster's kubeconfig")
    key = "vk_" + secrets.token_urlsafe(24)
    private_key, public_key = app_key()
    deploy_stubs(
        stub_image,
        {
            "gateway_key": key,
            "models": MODELS,
            "app_id": APP_ID,
            "app_slug": APP_SLUG,
            "public_key_pem": public_key,
            "installations": [
                {
                    "id": 77,
                    "account": "octo-lab",
                    "type": "Organization",
                    "repositories": [
                        {"full_name": REPOSITORY, "default_branch": "trunk"},
                        {"full_name": "octo-lab/gadgets", "default_branch": "main"},
                    ],
                }
            ],
        },
    )
    with KS.PortForward() as base_url:
        admin = KS.mint_token(f"first-run-{int(time.time())}", "admin")
        KS.await_supervisor(base_url, admin)

        step("1. Status on an empty deploy (#123)")
        before = readiness(base_url, admin)
        show("readiness", before)
        names = [h["name"] for h in before["harnesses"]]
        if "script-harness" in names or "script-harness" in json.dumps(before["steps"]):
            raise SmokeError("the readiness list names the script harness, a test fixture")
        codes = [s["code"] for s in hermes_of(before)["steps"]]
        for wanted in ("credential_missing", "endpoint_not_configured", "no_promoted_image"):
            if wanted not in codes:
                raise SmokeError(f"Hermes's steps {codes} do not name {wanted}")

        gateway(base_url, admin, key)
        egress_and_image(base_url, admin)
        github(base_url, admin, private_key)

        step("7. Status reaches ready for Hermes (#123)")
        deadline = time.monotonic() + 240
        while True:
            after = readiness(base_url, admin)
            if after["ready"] and "hermes" in after["ready_harnesses"]:
                break
            if time.monotonic() > deadline:
                show("readiness", after)
                raise SmokeError("Status never reached ready for Hermes")
            time.sleep(5)
        show("readiness", after)

        opener = sign_in(base_url)
        status_page = page_text(opener, f"{base_url}/ui")
        match = re.search(r"System status (.{0,120})", status_page)
        log(f"rendered /ui: {match.group(0) if match else status_page[:200]}")
        if "Ready for a task on hermes" not in status_page or " ready " not in status_page:
            raise SmokeError("the rendered Status page does not read ready for Hermes")
        gateway_page = page_text(opener, f"{base_url}/ui/gateway")
        for wanted in (f"{STUB_URL}/v1", "the last test passed"):
            if wanted not in gateway_page:
                raise SmokeError(f"the rendered Local gateway page lacks {wanted!r}")
        if key in gateway_page:
            raise SmokeError("the rendered Local gateway page shows the key")
        github_page = page_text(opener, f"{base_url}/ui/github")
        for wanted in (
            "connected",
            f"https://github.com/apps/{APP_SLUG}/installations/new",
            REPOSITORY,
        ):
            if wanted not in github_page:
                raise SmokeError(f"the rendered GitHub page lacks {wanted!r}")
        log("rendered /ui, /ui/gateway and /ui/github read as expected")
        log("\nfirst-run kind proof passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stub-image",
        default=os.environ.get("CRUCIBLE_DEPLOY_KIND_STUB_IMAGE"),
        help="an image with python and cryptography the cluster has (the service image)",
    )
    args = parser.parse_args(argv)
    if not args.stub_image:
        print("set --stub-image or CRUCIBLE_DEPLOY_KIND_STUB_IMAGE", file=sys.stderr)
        return 2
    try:
        smoke(args.stub_image)
    except SmokeError as exc:
        print(f"first-run kind proof failed: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
