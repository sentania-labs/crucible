#!/usr/bin/env python3
"""Spike S10 and S12 driver (Crucible's side). Throwaway, not product code.

Subcommands:
  run       mint a repository-scoped installation token, run the publisher
            container, then prove the token's absence everywhere by hash
  observe   poll a PR through the REST API for reviewer signals (S12)
  cleanup   close the PR, delete the branch and tag, remove containers,
            images (except the base), and the network

The App private key is read into memory only. The token is held in memory
and written to the publisher container's stdin; it never touches argv, env,
a host file, a log, or this program's output. Only its SHA-256 is recorded,
for the absence proof.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

CRED_DIR = Path.home() / ".local/state/foundry/crucible-credentials/github"
API = "https://api.github.com"
REPO = "sentania-labs/crucible-spike-target"
BASE_IMAGE = "crucible-worker:codex-0.153.4-fdfc3a7c883e"
SQUID_IMAGE = "ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029"
NETWORK = "crucible-s10-internal"
EGRESS = "s10-egress"
PUBLISHER = "s10-publisher"
TZ = ZoneInfo("America/Chicago")
HERE = Path(__file__).resolve().parent
# Installation tokens are `ghs_` plus a variable-length body (390 characters
# on 2026-09-16, not the 36 of the older format) that contains dots, dashes,
# and underscores, so the shape check must pin neither length nor alphabet.
# The positive control below is what proves this regex matches a real token.
TOKEN_RE = re.compile(rb"ghs_[A-Za-z0-9_.-]{20,2000}")


def local(ts=None):
    dt = datetime.now(TZ) if ts is None else datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(*argv, input=None, check=True, capture=True):
    return subprocess.run(list(argv), input=input, check=check, capture_output=capture)


# ---------------------------------------------------------------- GitHub API
def api(method, path, *, jwt=None, token=None, body=None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            status, hdrs = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        status, hdrs = e.code, dict(e.headers)
    ms = int((time.monotonic() - t0) * 1000)
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = raw.decode(errors="replace")
    accepted = hdrs.get("X-Accepted-GitHub-Permissions") or hdrs.get("x-accepted-github-permissions")
    return {"status": status, "ms": ms, "accepted": accepted, "body": payload}


def sign_jwt(app_id, pem_bytes):
    key = serialization.load_pem_private_key(pem_bytes, password=None)
    now = int(time.time())
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
    claims = base64.urlsafe_b64encode(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode()).rstrip(b"=")
    signing_input = header + b"." + claims
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()


def mint(permissions=None):
    """Return (token, evidence). Evidence carries no secret."""
    meta = json.loads((CRED_DIR / "app.json").read_text())
    t0 = time.monotonic()
    pem = (CRED_DIR / "app.pem").read_bytes()
    jwt = sign_jwt(meta["id"], pem)
    del pem   # drops this reference only; CPython does not scrub the buffer
    t_sign = time.monotonic()
    who = api("GET", "/app", jwt=jwt)
    insts = api("GET", "/app/installations", jwt=jwt)
    t_list = time.monotonic()
    if insts["status"] != 200:
        raise SystemExit(f"installations list failed: {insts['status']} {insts['body']}")
    inst = next(i for i in insts["body"] if i["id"] == meta["installation_id"])
    body = {"repositories": [REPO.split("/")[1]]}
    if permissions:
        body["permissions"] = permissions
    tok = api("POST", f"/app/installations/{inst['id']}/access_tokens", jwt=jwt, body=body)
    t_tok = time.monotonic()
    if tok["status"] != 201:
        raise SystemExit(f"token mint failed: {tok['status']} {tok['body']}")
    token = tok["body"]["token"]
    evidence = {
        "app_slug": who["body"]["slug"],
        "app_id": meta["id"],
        "installation_id": inst["id"],
        "installation_repository_selection": inst["repository_selection"],
        "installation_permissions": inst["permissions"],
        "installations_visible": len(insts["body"]),
        "token_expires_at_utc": tok["body"]["expires_at"],
        "token_expires_at_local": local(tok["body"]["expires_at"]),
        "token_repository_selection": tok["body"]["repository_selection"],
        "token_repositories": [r["full_name"] for r in tok["body"].get("repositories", [])],
        "token_permissions": tok["body"]["permissions"],
        "token_prefix_shape": token[:4] + "..." + f"({len(token)} chars)",
        "latency_ms": {
            "read_key_and_sign_jwt": int((t_sign - t0) * 1000),
            "get_app_and_list_installations": int((t_list - t_sign) * 1000),
            "post_access_token": int((t_tok - t_list) * 1000),
            "total": int((t_tok - t0) * 1000),
        },
        "accepted_permissions_header": {"POST access_tokens": tok["accepted"]},
        "minted_at_local": local(),
    }
    return token, evidence


# ------------------------------------------------------------- absence proof
class Scanner:
    def __init__(self, token: str):
        self.value = token.encode()
        self.digest = hashlib.sha256(self.value).hexdigest()
        self.rows = []
        self.lock = threading.Lock()

    def check(self, location, data: bytes, note=""):
        hits = data.count(self.value)
        cands = TOKEN_RE.findall(data)
        hash_hits = sum(1 for c in cands if hashlib.sha256(c).hexdigest() == self.digest)
        with self.lock:
            existing = next((r for r in self.rows if r["location"] == location), None)
            if existing:
                existing["bytes"] += len(data)
                existing["samples"] += 1
                existing["substring_hits"] += hits
                existing["pattern_candidates"] += len(cands)
                existing["hash_matches"] += hash_hits
            else:
                self.rows.append({"location": location, "bytes": len(data), "samples": 1,
                                  "substring_hits": hits, "pattern_candidates": len(cands),
                                  "hash_matches": hash_hits, "note": note})
        return hits + hash_hits

    def positive_control(self):
        """A scan that must find the token, so a silently empty scan cannot pass."""
        probe = b"prefix " + self.value + b" suffix"
        row = {"location": "POSITIVE CONTROL (synthetic buffer containing the token)",
               "bytes": len(probe), "samples": 1,
               "substring_hits": probe.count(self.value),
               "pattern_candidates": len(TOKEN_RE.findall(probe)),
               "hash_matches": sum(1 for c in TOKEN_RE.findall(probe)
                                   if hashlib.sha256(c).hexdigest() == self.digest),
               "note": "must be non-zero; proves the scanner detects this token's value and shape"}
        with self.lock:
            self.rows.append(row)
        return row["substring_hits"] > 0 and row["hash_matches"] > 0

    def check_stream(self, location, stream, note=""):
        total, hits, cands, hh = 0, 0, 0, 0
        tail = b""
        while True:
            chunk = stream.read(8 << 20)
            if not chunk:
                break
            buf = tail + chunk
            hits += buf.count(self.value)
            found = TOKEN_RE.findall(buf)
            cands += len(found)
            hh += sum(1 for c in found if hashlib.sha256(c).hexdigest() == self.digest)
            total += len(chunk)
            tail = buf[-64:]
        with self.lock:
            self.rows.append({"location": location, "bytes": total, "samples": 1, "substring_hits": hits,
                              "pattern_candidates": cands, "hash_matches": hh, "note": note})

    def check_tree(self, location, root: Path, max_file=64 << 20, note=""):
        total, files, hits, cands, hh, skipped = 0, 0, 0, 0, 0, 0
        unreadable_dirs = []
        for dirpath, dirnames, filenames in os.walk(root, onerror=unreadable_dirs.append):
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                    size = p.stat().st_size
                    if size > max_file:
                        skipped += 1
                        continue
                    data = p.read_bytes()
                except OSError:
                    skipped += 1
                    continue
                files += 1
                total += len(data)
                hits += data.count(self.value)
                found = TOKEN_RE.findall(data)
                cands += len(found)
                hh += sum(1 for c in found if hashlib.sha256(c).hexdigest() == self.digest)
        with self.lock:
            self.rows.append({"location": location, "bytes": total, "samples": files, "substring_hits": hits,
                              "pattern_candidates": cands, "hash_matches": hh,
                              "note": f"{note} files={files} skipped_files(unreadable or >64MiB)={skipped} "
                                      f"unreadable_dirs={len(unreadable_dirs)}".strip()})


def sample_process_state(scanner, container, stop_evt):
    """While the publisher runs: host ps (with env) and /proc/<pid>/environ of
    every process in the container, sampled twice a second."""
    while not stop_evt.is_set():
        try:
            ps = sh("sudo", "ps", "auxwwe", check=False).stdout
            scanner.check("host ps auxwwe (argv+env of every process, during run)", ps)
            top = sh("docker", "top", container, "-eo", "pid", check=False).stdout.decode().split()[1:]
            for pid in top:
                env = sh("sudo", "cat", f"/proc/{pid}/environ", check=False).stdout
                cmd = sh("sudo", "cat", f"/proc/{pid}/cmdline", check=False).stdout
                scanner.check("container processes /proc/<pid>/environ + cmdline (during run)", env + b"\0" + cmd)
        except Exception:
            pass
        stop_evt.wait(0.5)


# --------------------------------------------------------------------- run
def cmd_run(args):
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    out = scratch / f"out-{stamp}"
    out.mkdir()
    out.chmod(0o777)  # the container writes /out as uid 1000; nothing secret lands here
    result = {"stamp": stamp, "started_local": local(), "repo": REPO,
              "flags": {"mode": args.mode, "permissions": args.permissions, "light": args.light}}

    perms = dict(kv.split("=", 1) for kv in args.permissions.split(",")) if args.permissions else None
    log(f"minting installation token (permissions={perms or 'App default'})")
    token, evidence = mint(perms)
    scanner = Scanner(token)
    result["mint"] = evidence
    log(f"token scoped to {evidence['token_repositories']} sel={evidence['token_repository_selection']} "
        f"perms={evidence['token_permissions']} expires {evidence['token_expires_at_local']} "
        f"latency {evidence['latency_ms']}")

    image = f"crucible-s10-publisher:{stamp}"
    log(f"building {image}")
    b = sh("docker", "build", "-q", "-t", image, "--build-arg", f"BASE={BASE_IMAGE}", str(HERE))
    result["publisher_image"] = {"tag": image, "id": b.stdout.decode().strip()}

    log("egress network and proxy")
    if sh("docker", "network", "inspect", NETWORK, check=False).returncode != 0:
        sh("docker", "network", "create", "--internal", NETWORK)
    subnet = sh("docker", "network", "inspect", NETWORK, "--format", "{{(index .IPAM.Config 0).Subnet}}").stdout.decode().strip()
    conf = (HERE / "squid.conf.in").read_text().replace("@SUBNET@", subnet)
    conf_path = scratch / "squid.conf"
    conf_path.write_text(conf)
    conf_path.chmod(0o644)
    if sh("docker", "inspect", EGRESS, check=False).returncode != 0:
        sh("docker", "run", "-d", "--name", EGRESS, "--network", "bridge",
           "-v", f"{conf_path}:/etc/squid/squid.conf:ro", SQUID_IMAGE)
        sh("docker", "network", "connect", NETWORK, EGRESS)
    result["network"] = {"name": NETWORK, "subnet": subnet, "egress": EGRESS, "squid_image": SQUID_IMAGE}
    time.sleep(2)

    branch = f"crucible/S10-{stamp}"
    tag = f"s10-{stamp}"
    publisher = f"{PUBLISHER}-{stamp}"
    argv = ["docker", "run", "-i", "--name", publisher,
            "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--pids-limit", "256", "--memory", "512m",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--tmpfs", "/home/worker:rw,nosuid,nodev,size=512m,mode=0700,uid=1000,gid=1000",
            "--tmpfs", "/run/crucible-token:rw,nosuid,nodev,noexec,size=64k,mode=0700,uid=1000,gid=1000",
            "--network", NETWORK,
            "-e", f"HTTPS_PROXY=http://{EGRESS}:3128", "-e", f"https_proxy=http://{EGRESS}:3128",
            "-e", "NO_PROXY=localhost,127.0.0.1",
            "-e", f"S10_REPO={REPO}", "-e", f"S10_BRANCH={branch}", "-e", f"S10_TAG={tag}",
            "-e", f"S10_STAMP={stamp}", "-e", f"S10_MODE={args.mode}",
            "-v", f"{out}:/out",
            image]
    result["publisher_argv"] = argv
    result["branch"], result["tag"] = branch, tag
    log("starting publisher: " + " ".join(argv))
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    proc.stdin.write(token.encode())
    proc.stdin.close()  # EOF: the container's cat finishes and the script proceeds
    stop_evt = threading.Event()
    sampler = threading.Thread(target=sample_process_state, args=(scanner, publisher, stop_evt), daemon=True)
    sampler.start()
    live_output = proc.stdout.read()
    rc = proc.wait()
    stop_evt.set()
    sampler.join()
    result["publisher_exit"] = rc
    result["publisher_wall_ms"] = int((time.monotonic() - t0) * 1000)
    scanner.check("publisher attached stdout+stderr", live_output)
    print(live_output.decode(errors="replace"))
    log(f"publisher exited {rc} after {result['publisher_wall_ms']} ms")
    result["publisher_result"] = json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else None
    if (out / "create-pr.json").exists():
        pr = json.loads((out / "create-pr.json").read_text())
        result["pr"] = {k: pr.get(k) for k in ("number", "html_url", "state", "created_at")}
        if pr.get("user"):
            result["pr"]["author_login"] = pr["user"]["login"]
            result["pr"]["author_type"] = pr["user"]["type"]
        if pr.get("head"):
            result["pr"]["head_sha"] = pr["head"]["sha"]
        if pr.get("created_at"):
            result["pr"]["created_at_local"] = local(pr["created_at"])
    if (out / "api-calls.tsv").exists():
        result["api_calls_in_container"] = [l.split("\t") for l in (out / "api-calls.tsv").read_text().splitlines()]
    for f in ("push-branch.err", "push-tag.err"):
        if (out / f).exists():
            result[f] = (out / f).read_text()[-2000:]

    log("absence proof: docker logs, inspect, diff, export, image layers")
    scanner.check("docker logs " + publisher, sh("docker", "logs", publisher, check=False).stdout + sh("docker", "logs", publisher, check=False).stderr)
    inspect = sh("docker", "inspect", publisher).stdout
    scanner.check("docker inspect " + publisher + " (Config.Env, Args, Mounts, HostConfig)", inspect)
    env = json.loads(inspect)[0]["Config"]["Env"]
    result["container_env_names"] = [e.split("=", 1)[0] for e in env]
    scanner.check("container Config.Env (from inspect)", "\n".join(env).encode())
    diff = sh("docker", "diff", publisher, check=False).stdout
    result["docker_diff"] = diff.decode().splitlines()
    scanner.check("docker diff " + publisher, diff)
    with subprocess.Popen(["docker", "export", publisher], stdout=subprocess.PIPE) as p:
        scanner.check_stream("docker export " + publisher + " (container rootfs incl. writable layer, tmpfs excluded)", p.stdout)
    with subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE) as p:
        scanner.check_stream("docker save " + image + " (every image layer, incl. base)", p.stdout)
    scanner.check("docker inspect " + EGRESS, sh("docker", "inspect", EGRESS).stdout)
    scanner.check("docker logs " + EGRESS + " (squid access log)", sh("docker", "logs", EGRESS, check=False).stdout + sh("docker", "logs", EGRESS, check=False).stderr)
    squid_log = sh("docker", "exec", EGRESS, "cat", "/var/log/squid/access.log", check=False).stdout
    scanner.check("squid /var/log/squid/access.log", squid_log)
    result["squid_access_log"] = squid_log.decode(errors="replace").splitlines()[-40:]
    scanner.check("docker inspect " + image, sh("docker", "inspect", image).stdout)
    scanner.check("docker history " + image, sh("docker", "history", "--no-trunc", image).stdout)

    log("absence proof: host filesystem")
    for label, root in (("worktree " + args.worktree, Path(args.worktree)),
                        ("publisher output dir " + str(out), out),
                        ("scratch root " + str(scratch) + " (run records, rendered squid.conf)", scratch),
                        ("/tmp (whole tree)" if not args.light else "/tmp: NOT SCANNED (--light)",
                         Path("/tmp") if not args.light else out),
                        ("/dev/shm", Path("/dev/shm")),
                        ("~/.config/gh", Path.home() / ".config/gh"),
                        ("~/.gitconfig and ~/.git-credentials", Path.home())):
        if label.startswith("~/.gitconfig"):
            data = b""
            for fn in (".gitconfig", ".git-credentials", ".netrc"):
                fp = root / fn
                if fp.exists():
                    data += fp.read_bytes()
            scanner.check(label, data, note="present: " + ",".join(fn for fn in (".gitconfig", ".git-credentials", ".netrc") if (root / fn).exists()))
        elif root.exists():
            scanner.check_tree(label, root)
    scanner.check("this process argv (sys.argv)", " ".join(sys.argv).encode())
    scanner.check("this process environ", "\n".join(f"{k}={v}" for k, v in os.environ.items()).encode())

    control_ok = scanner.positive_control()
    negative = [r for r in scanner.rows if not r["location"].startswith("POSITIVE CONTROL")]
    empty = [r["location"] for r in negative if r["bytes"] == 0]
    result["absence_proof"] = scanner.rows
    result["positive_control_detects"] = control_ok
    result["empty_scans"] = empty
    result["absence_clean"] = (control_ok and not empty
                               and all(r["substring_hits"] == 0 and r["hash_matches"] == 0 for r in negative))
    result["finished_local"] = local()
    (scratch / f"s10-{stamp}.json").write_text(json.dumps(result, indent=2))
    del token
    log(f"absence proof clean={result['absence_clean']}; rows={len(scanner.rows)}")
    for r in scanner.rows:
        log(f"  {r['location'][:80]:80} bytes={r['bytes']:>11} samples={r['samples']:>4} substr={r['substring_hits']} cands={r['pattern_candidates']} hash={r['hash_matches']}")
    log(f"result written to {scratch / f's10-{stamp}.json'}")


# ------------------------------------------------------------------ observe
def cmd_observe(args):
    token, evidence = mint()
    pr_number = args.pr
    scratch = Path(args.scratch)
    events = []
    seen = set()
    accepted_headers = {}
    pr0 = api("GET", f"/repos/{REPO}/pulls/{pr_number}", token=token)
    pr_created = pr0["body"]["created_at"]
    head = pr0["body"]["head"]["sha"]
    t_open = datetime.fromisoformat(pr_created.replace("Z", "+00:00"))
    log(f"observing PR #{pr_number} head {head[:10]} opened {local(pr_created)} by {pr0['body']['user']['login']}")

    def delta(ts):
        return int((datetime.fromisoformat(ts.replace("Z", "+00:00")) - t_open).total_seconds())

    def note(kind, obj_id, login, user_type, ts, extra):
        key = (kind, obj_id)
        if key in seen:
            return
        seen.add(key)
        ev = {"kind": kind, "id": obj_id, "login": login, "user_type": user_type, "at_utc": ts,
              "at_local": local(ts), "seconds_after_pr_open": delta(ts), "observed_local": local(), **extra}
        events.append(ev)
        log(f"SIGNAL {kind} id={obj_id} login={login} type={user_type} at={ev['at_local']} (+{ev['seconds_after_pr_open']}s) {json.dumps(extra)[:300]}")

    def get(path, label):
        r = api("GET", path, token=token)
        accepted_headers.setdefault(label, r["accepted"])
        return r["body"] if r["status"] == 200 else []

    started = time.monotonic()
    last_signal = None
    trigger_posted = None
    cycle = 0
    while True:
        cycle += 1
        pr = get(f"/repos/{REPO}/pulls/{pr_number}", "GET pulls/{n}")
        head = pr["head"]["sha"] if pr else head
        for rv in get(f"/repos/{REPO}/pulls/{pr_number}/reviews", "GET pulls/{n}/reviews"):
            note("review", rv["id"], rv["user"]["login"], rv["user"]["type"], rv["submitted_at"],
                 {"state": rv["state"], "commit_id": rv["commit_id"], "body": rv["body"], "html_url": rv["html_url"]})
        for c in get(f"/repos/{REPO}/pulls/{pr_number}/comments", "GET pulls/{n}/comments"):
            note("review_comment", c["id"], c["user"]["login"], c["user"]["type"], c["created_at"],
                 {"path": c.get("path"), "line": c.get("line"), "commit_id": c["commit_id"],
                  "original_commit_id": c.get("original_commit_id"), "review_id": c.get("pull_request_review_id"),
                  "body": c["body"], "html_url": c["html_url"]})
            for rx in get(f"/repos/{REPO}/pulls/comments/{c['id']}/reactions", "GET pulls/comments/{id}/reactions"):
                note("reaction_on_review_comment", rx["id"], rx["user"]["login"], rx["user"]["type"], rx["created_at"],
                     {"content": rx["content"], "comment_id": c["id"]})
        for c in get(f"/repos/{REPO}/issues/{pr_number}/comments", "GET issues/{n}/comments"):
            note("issue_comment", c["id"], c["user"]["login"], c["user"]["type"], c["created_at"],
                 {"body": c["body"], "html_url": c["html_url"]})
            for rx in get(f"/repos/{REPO}/issues/comments/{c['id']}/reactions", "GET issues/comments/{id}/reactions"):
                note("reaction_on_issue_comment", rx["id"], rx["user"]["login"], rx["user"]["type"], rx["created_at"],
                     {"content": rx["content"], "comment_id": c["id"]})
        for rx in get(f"/repos/{REPO}/issues/{pr_number}/reactions", "GET issues/{n}/reactions"):
            note("reaction_on_pr", rx["id"], rx["user"]["login"], rx["user"]["type"], rx["created_at"], {"content": rx["content"]})
        cr = get(f"/repos/{REPO}/commits/{head}/check-runs", "GET commits/{sha}/check-runs")
        for run in (cr.get("check_runs", []) if isinstance(cr, dict) else []):
            note("check_run", run["id"], run["app"]["slug"] if run.get("app") else "?", "App", run["started_at"] or pr_created,
                 {"name": run["name"], "status": run["status"], "conclusion": run["conclusion"], "head_sha": run["head_sha"]})
        cs = get(f"/repos/{REPO}/commits/{head}/check-suites", "GET commits/{sha}/check-suites")
        for suite in (cs.get("check_suites", []) if isinstance(cs, dict) else []):
            note("check_suite", suite["id"], suite["app"]["slug"] if suite.get("app") else "?", "App", suite["created_at"],
                 {"status": suite["status"], "conclusion": suite["conclusion"], "head_sha": suite["head_sha"]})
        actor_events = [e for e in events if e["kind"] not in ("check_run", "check_suite")]
        n_now = len(actor_events)
        if n_now and n_now != last_signal:
            last_signal = n_now
            last_signal_mono = time.monotonic()
        elapsed = time.monotonic() - started
        log(f"cycle {cycle}: {n_now} actor signals ({len(events)} objects) so far, {int(elapsed)}s elapsed, head {head[:10]}, PR state {pr.get('state') if pr else '?'}")
        if n_now and time.monotonic() - last_signal_mono >= args.settle_minutes * 60:
            log("settled: no new signal for the settle window")
            break
        if elapsed >= args.max_minutes * 60:
            if not n_now and args.fallback_trigger and not trigger_posted:
                r = api("POST", f"/repos/{REPO}/issues/{pr_number}/comments", token=token, body={"body": "@codex review"})
                trigger_posted = {"status": r["status"], "at_local": local(), "accepted": r["accepted"],
                                  "comment_id": r["body"].get("id") if isinstance(r["body"], dict) else None}
                log(f"no signal after {args.max_minutes} min; posted '@codex review' -> {r['status']}")
                started = time.monotonic()
                args.max_minutes = args.post_trigger_minutes
                continue
            log("max wait reached")
            break
        time.sleep(args.interval)
    outp = scratch / f"s12-pr{pr_number}.json"
    outp.write_text(json.dumps({"pr": pr_number, "pr_created_at_utc": pr_created, "pr_created_at_local": local(pr_created),
                                "pr_author": pr0["body"]["user"]["login"], "head": head, "events": events,
                                "flags": {"interval": args.interval, "max_minutes": args.max_minutes,
                                          "settle_minutes": args.settle_minutes,
                                          "fallback_trigger": args.fallback_trigger,
                                          "post_trigger_minutes": args.post_trigger_minutes},
                                "trigger_posted": trigger_posted, "accepted_permissions_headers": accepted_headers,
                                "mint": evidence, "finished_local": local()}, indent=2))
    del token
    log(f"observe finished; {len(events)} signals; written {outp}")


# ------------------------------------------------------------------ cleanup
def cmd_cleanup(args):
    token, _ = mint()
    steps = []
    for pr_number in args.pr:
        r = api("PATCH", f"/repos/{REPO}/pulls/{pr_number}", token=token, body={"state": "closed"})
        steps.append(("close PR #%d" % pr_number, r["status"], r["accepted"]))
    for ref in args.ref:
        r = api("DELETE", f"/repos/{REPO}/git/refs/{ref}", token=token)
        steps.append((f"delete ref {ref}", r["status"], r["accepted"]))
    del token
    names = sh("docker", "ps", "-a", "--filter", f"name={PUBLISHER}", "--format", "{{.Names}}").stdout.decode().split() + [EGRESS]
    for name in names:
        steps.append((f"docker rm -f {name}", sh("docker", "rm", "-f", name, check=False).returncode, ""))
    steps.append((f"docker network rm {NETWORK}", sh("docker", "network", "rm", NETWORK, check=False).returncode, ""))
    imgs = sh("docker", "images", "crucible-s10-publisher", "-q").stdout.decode().split()
    for i in imgs:
        steps.append((f"docker rmi {i}", sh("docker", "rmi", "-f", i, check=False).returncode, ""))
    if not args.keep_squid:
        steps.append(("docker rmi squid", sh("docker", "rmi", SQUID_IMAGE, check=False).returncode, ""))
    for s in steps:
        log(f"{s[0]}: {s[1]} {s[2] or ''}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scratch", required=True)
    r.add_argument("--worktree", required=True)
    r.add_argument("--mode", default="full", choices=["full", "push-only", "pr-expect-403"])
    r.add_argument("--permissions", default=None, help="downscope the token, e.g. contents=write")
    r.add_argument("--light", action="store_true", help="skip the /tmp walk (repeat runs)")
    r.set_defaults(fn=cmd_run)
    o = sub.add_parser("observe")
    o.add_argument("--pr", type=int, required=True)
    o.add_argument("--scratch", required=True)
    o.add_argument("--interval", type=int, default=60)
    o.add_argument("--max-minutes", type=float, default=20)
    o.add_argument("--settle-minutes", type=float, default=6)
    o.add_argument("--fallback-trigger", action="store_true")
    o.add_argument("--post-trigger-minutes", type=float, default=15)
    o.set_defaults(fn=cmd_observe)
    c = sub.add_parser("cleanup")
    c.add_argument("--pr", type=int, action="append", default=[])
    c.add_argument("--ref", action="append", default=[], help="heads/... or tags/...")
    c.add_argument("--keep-squid", action="store_true")
    c.set_defaults(fn=cmd_cleanup)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
