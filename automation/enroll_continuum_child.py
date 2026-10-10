#!/usr/bin/env python3
"""Owner-authorized enrollment. No tracked child map; never reveal private names."""
import json
import os
import re
import secrets
import subprocess

TITLE = "[Continuum] Enroll Child (v1)"
MARKER = "<!-- continuum:enroll-child:v1 -->"
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
SCRIPT = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*\.sh$")


class EnrollmentError(Exception):
    pass


def api(*args):
    try:
        result = subprocess.run(
            ["gh", "api", *args], check=True, capture_output=True,
            text=True, timeout=60
        )
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        raise EnrollmentError("GitHub API unavailable; enrollment stopped.") from None


def vars_for(repo, get=api):
    found = {}
    for page in range(1, 51):
        data = get("-X", "GET", f"repos/{repo}/actions/variables",
                   "-f", "per_page=100", "-f", f"page={page}")
        if not isinstance(data.get("variables"), list):
            raise EnrollmentError("Actions variables unavailable.")
        for item in data["variables"]:
            found[item["name"]] = item["value"]
        if len(data["variables"]) < 100:
            return found
    raise EnrollmentError("Actions variables pagination exceeded.")


def valid_parent(values):
    if values.get("CONTINUUM_ROLE") != "parent":
        raise EnrollmentError("Parent role is not configured.")
    if values.get("CONTINUUM_REVIEW_PROVIDER") != "pr-agent":
        raise EnrollmentError("Parent review provider is not PR-Agent.")
    try:
        ids = json.loads(values.get("CONTINUUM_CHILDREN", "[]"))
    except ValueError:
        raise EnrollmentError("Invalid parent child inventory.") from None
    if not isinstance(ids, list) or any(
        not isinstance(x, str) or not ID.fullmatch(x) for x in ids
    ) or len(ids) != len(set(ids)):
        raise EnrollmentError("Invalid parent child inventory.")
    return ids


def request_details(issue, parent):
    if issue.get("title") != TITLE or "pull_request" in issue:
        raise EnrollmentError("Invalid issue type.")
    body = issue.get("body", "")
    if not isinstance(body, str) or not body.startswith(MARKER + "\n"):
        raise EnrollmentError("Invalid enrollment marker.")
    try:
        spec = json.loads(body[len(MARKER) + 1:].strip())
    except ValueError:
        raise EnrollmentError("Invalid enrollment payload.") from None
    if not isinstance(spec, dict) or set(spec) != {
        "parent", "review_provider", "validation_script"
    } or spec["parent"] != parent or spec["review_provider"] != "pr-agent":
        raise EnrollmentError("Enrollment contract mismatch.")
    path = spec["validation_script"]
    if not isinstance(path, str) or not SCRIPT.fullmatch(path) or ".." in path:
        raise EnrollmentError("Invalid delegated validation entrypoint.")
    return path


def upsert(repo, key, value, values, get=api):
    if values.get(key) == value:
        return
    if key in values:
        get("-X", "PATCH", f"repos/{repo}/actions/variables/{key}",
            "-f", f"name={key}", "-f", f"value={value}")
    else:
        get("-X", "POST", f"repos/{repo}/actions/variables",
            "-f", f"name={key}", "-f", f"value={value}")
    values[key] = value


def recheck_open_pull_requests(child, get=api):
    """Replay failed exact-HEAD child CI after moving execution to Parent.

    Never mark CI as success. The shared CI job will re-evaluate the Child role
    on replay and legitimately skip. Parent deterministic validation remains
    mandatory and distinct from this admission signal.
    """
    replayed = 0
    for page in range(1, 11):
        data = get("-X", "GET", f"repos/{child}/pulls",
                   "-f", "state=open", "-f", "per_page=100",
                   "-f", f"page={page}")
        prs = data if isinstance(data, list) else None
        if prs is None:
            raise EnrollmentError("Open pull request discovery unavailable.")
        for pr in prs:
            head = pr.get("head") or {}
            if pr.get("draft") or (head.get("repo") or {}).get("full_name") != child:
                continue
            sha = head.get("sha", "")
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise EnrollmentError("Pull request head identity is invalid.")
            result = get("-X", "GET", f"repos/{child}/actions/runs",
                         "-f", "event=pull_request", "-f", f"head_sha={sha}",
                         "-f", "per_page=100")
            runs = result.get("workflow_runs")
            if not isinstance(runs, list):
                raise EnrollmentError("Child CI-run discovery unavailable.")
            # GitHub lists newest runs first. Admission belongs to exactly
            # the PR's current commit; never touch an earlier head.
            for run in runs:
                if run.get("name") != "CI":
                    continue
                if run.get("head_sha") != sha:
                    continue
                if not any(x.get("number") == pr["number"] for x in run.get("pull_requests", [])):
                    continue
                if run.get("status") == "completed" and run.get("conclusion") == "failure":
                    get("-X", "POST",
                        f"repos/{child}/actions/runs/{run['id']}/rerun-failed-jobs")
                    replayed += 1
                break
        if len(prs) < 100:
            return replayed
    raise EnrollmentError("Child pull request pagination exceeded.")


def enroll(issue, parent, get=api, new_id=None):
    owner = parent.split("/")[0]
    if (issue.get("user") or {}).get("login", "").lower() != owner.lower():
        raise EnrollmentError("Owner authorization required.")
    url = issue.get("repository_url", "")
    prefix = "https://api.github.com/repos/"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise EnrollmentError("Invalid repository binding.")
    child = url[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", child):
        raise EnrollmentError("Invalid repository identity.")
    if child.split("/")[0].lower() != owner.lower() or child == parent:
        raise EnrollmentError("Enrollment owner or repository mismatch.")
    print("::add-mask::" + child, flush=True)
    path = request_details(issue, parent)
    parent_vars = vars_for(parent, get)
    allowed = valid_parent(parent_vars)
    child_vars = vars_for(child, get)
    if child_vars.get("CONTINUUM_ROLE", "") not in ("", "child"):
        raise EnrollmentError("Child has incompatible role.")
    if child_vars.get("CONTINUUM_PARENT", "") not in ("", parent):
        raise EnrollmentError("Child belongs to another parent.")
    # An owner-approved request may explicitly migrate the legacy CodeRabbit provider.
    if child_vars.get("CONTINUUM_REVIEW_PROVIDER", "") not in ("", "pr-agent", "coderabbit"):
        raise EnrollmentError("Child has incompatible review provider.")
    file_info = get("-X", "GET", f"repos/{child}/contents/{path}")
    if file_info.get("type") != "file":
        raise EnrollmentError("Child validation script is absent.")
    child_id = child_vars.get("CONTINUUM_CHILD_ID", "")
    if child_id:
        if not ID.fullmatch(child_id):
            raise EnrollmentError("Existing child ID is invalid.")
    else:
        child_id = (new_id or (lambda: "c-" + secrets.token_hex(12)))()
        if not ID.fullmatch(child_id) or child_id in allowed:
            raise EnrollmentError("Generated child ID collision.")
    # Child is configured first; only afterwards is it granted parent admission.
    for name, val in (
        ("CONTINUUM_CHILD_ID", child_id),
        ("CONTINUUM_PARENT", parent),
        ("CONTINUUM_REVIEW_PROVIDER", "pr-agent"),
        ("CONTINUUM_VALIDATION_SCRIPT", path),
        ("CONTINUUM_ROLE", "child"),
    ):
        upsert(child, name, val, child_vars, get)
    # Re-read the allow-list at mutation time and preserve existing IDs.
    parent_vars = vars_for(parent, get)
    allowed = valid_parent(parent_vars)
    if child_id not in allowed:
        upsert(parent, "CONTINUUM_CHILDREN",
               json.dumps(allowed + [child_id], separators=(",", ":")),
               parent_vars, get)
    recheck_open_pull_requests(child, get)
    get("-X", "PATCH", f"repos/{child}/issues/{issue['number']}",
        "-f", "state=closed")


def pending(owner, get=api):
    query = f'"Enroll Child" in:title is:issue is:open author:{owner}'
    results = []
    for page in range(1, 11):
        payload = get("-X", "GET", "search/issues",
                      "-f", f"q={query}", "-f", "per_page=100",
                      "-f", f"page={page}")
        items = payload.get("items")
        if not isinstance(items, list):
            raise EnrollmentError("Enrollment discovery unavailable.")
        for issue in items:
            if (issue.get("title") == TITLE and
                    str(issue.get("body", "")).startswith(MARKER)):
                results.append(issue)
        if len(items) < 100:
            return sorted(results, key=lambda x: x.get("url", ""))
    raise EnrollmentError("Enrollment discovery pagination exceeded.")


def main():
    parent = os.environ.get("GITHUB_REPOSITORY", "")
    if not os.environ.get("GH_TOKEN"):
        raise EnrollmentError("Parent TAP_PAT unavailable.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", parent):
        raise EnrollmentError("Invalid parent runtime identity.")
    count = 0
    for issue in pending(parent.split("/")[0]):
        enroll(issue, parent)
        count += 1
    print(f"Enrollment reconciliation: {count} authorized private request(s).")


if __name__ == "__main__":
    try:
        main()
    except EnrollmentError as exc:
        print(f"::error::{exc}")
        raise SystemExit(1)
