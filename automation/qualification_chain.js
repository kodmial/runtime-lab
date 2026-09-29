#!/usr/bin/env node
"use strict";

/*
 * Durable closed-loop controller for the OpenCode 512 MiB qualification.
 *
 * Existing tasks are reused:
 *   runtime-lab #126  Docker qualification (reusable)
 *   runtime-lab #128  exact-artifact Render delivery implementation
 *   runtime-lab #106  Render pass 1
 *   runtime-lab #110  Render pass 2
 *   runtime-lab #58   Render pass 3
 *   runtime-lab #6    integration after three consecutive passes
 *   opencode #11      profile-guided memory optimization, continuing PR #12
 *
 * State is a machine-readable comment on #126. All exact artifact identity
 * comes from the successful opencode artifact workflow manifest on opencode#11.
 */

function resolveGitHubToken() {
  // Issue #142: the qualification-chain envelope emitted the evaluated
  // secret with a stray leading backslash (escaped `\${{ ... }}` expression
  // in the workflow env block), which corrupts authentication into a 401
  // "Bad credentials". Real GitHub tokens never start with a backslash, so
  // strip exactly one while the workflow envelope is provisioned separately.
  // The static guard automation/workflow_credential_guard.py fails closed on
  // the escaped envelope syntax.
  const raw = process.env.GH_TOKEN || process.env.GITHUB_TOKEN || "";
  const token = raw.startsWith("\\") ? raw.slice(1) : raw;
  if (!token) throw new Error("GH_TOKEN is required");
  if (token.includes("${{")) throw new Error("GH_TOKEN looks like an unexpanded GitHub expression");
  return token;
}

const TOKEN = resolveGitHubToken();

const RUNTIME = "kodmial/runtime-lab";
const OPENCODE = "kodmial/opencode";

const DOCKER = 126;
const DELIVERY = 128;
const OPT_ISSUE = 11;
const OPT_PR = 12;
const INTEGRATION = 6;
const RENDER = [106, 110, 58];

const STATE_MARKER = "<!-- runtime-lab-qualification-state -->";
const RESET_MARKER = "<!-- runtime-lab-qualification-reset -->";
const DOCKER_RESULT = "<!-- runtime-lab-docker-qualification-result -->";
const RENDER_RESULT = "<!-- runtime-lab-render-qualification-result -->";
const ARTIFACT_MANIFEST = "<!-- opencode-coding-artifact-manifest -->";
const OPT_REQUEST = "<!-- runtime-lab-opencode-memory-optimization -->";
const OPT_DISPATCHED = "<!-- runtime-lab-opencode-memory-optimization-dispatched -->";
const BLOCKER = "<!-- runtime-lab-qualification-blocker -->";
const REPAIR_PREFIX = "<!-- runtime-lab-qualification-repair";

const LIMIT = 512 * 1024 * 1024;
const HEADROOM = 32 * 1024 * 1024;
const MAX_PEAK = LIMIT - HEADROOM;
const STATE_SCHEMA = "runtime-lab-qualification-chain/v1";

function splitRepo(repo) {
  const p = repo.split("/");
  return {owner: p[0], repo: p[1]};
}

async function api(repoFull, method, path, body) {
  const r = splitRepo(repoFull);
  const url = "https://api.github.com/repos/" + r.owner + "/" + r.repo + "/" + path.replace(/^\/+/, "");
  const opts = {
    method,
    headers: {
      Authorization: "Bearer " + TOKEN,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const text = await res.text();
  if (!res.ok) throw new Error(method + " " + url + " -> " + res.status + " " + text.slice(0, 500));
  return text ? JSON.parse(text) : null;
}

async function paginate(repo, path, arrayKey) {
  let out = [];
  const join = path.includes("?") ? "&" : "?";
  for (let page = 1; page <= 20; page++) {
    const data = await api(repo, "GET", path + join + "per_page=100&page=" + page);
    const rows = Array.isArray(data) ? data : ((data && data[arrayKey]) || []);
    out = out.concat(rows);
    if (rows.length < 100) break;
  }
  return out;
}

async function issue(repo, n) {
  return await api(repo, "GET", "issues/" + n);
}

async function comments(repo, n) {
  return await paginate(repo, "issues/" + n + "/comments?", "");
}

async function addComment(repo, n, body) {
  await api(repo, "POST", "issues/" + n + "/comments", {body});
}

async function patchIssue(repo, n, fields) {
  await api(repo, "PATCH", "issues/" + n, fields);
}

async function dispatch(repo, workflow, inputs) {
  await api(repo, "POST", "actions/workflows/" + encodeURIComponent(workflow) + "/dispatches", {
    ref: "main",
    inputs: inputs || {},
  });
}

async function pulls(repo, state) {
  return await paginate(repo, "pulls?state=" + (state || "all") + "&sort=updated&direction=desc&", "");
}

function labelNames(obj) {
  return new Set((obj.labels || []).map(x => typeof x === "string" ? x : x.name).filter(Boolean));
}

function markerRows(rows, marker) {
  const out = [];
  for (const row of rows) {
    const body = String(row.body || "");
    const pos = body.indexOf(marker);
    if (pos < 0) continue;
    const tail = body.slice(pos + marker.length).trim();
    const line = tail.split(/\r?\n/).map(x => x.trim()).find(x => x.startsWith("{"));
    if (!line) continue;
    try {
      const value = JSON.parse(line);
      if (value && typeof value === "object") {
        value._comment_id = row.id;
        value._created_at = row.created_at;
        out.push(value);
      }
    } catch (_) {}
  }
  return out;
}

function latestMarker(rows, marker) {
  const parsed = markerRows(rows, marker);
  return parsed.length ? parsed[parsed.length - 1] : null;
}

function clean(value) {
  const out = {};
  for (const [k, v] of Object.entries(value || {})) {
    if (!k.startsWith("_")) out[k] = v;
  }
  return out;
}

function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(k => JSON.stringify(k) + ":" + canonical(value[k])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function markerBody(marker, payload) {
  return marker + "\n" + canonical(payload);
}

function normalizeState(raw) {
  const base = raw ? clean(raw) : {};
  if (!base.schema) base.schema = STATE_SCHEMA;
  if (!Number.isInteger(base.generation)) base.generation = 0;
  if (!base.stage) base.stage = "bootstrap";
  if (!("artifact" in base)) base.artifact = null;
  if (!base.docker_status) base.docker_status = "unknown";
  if (typeof base.delivery_ready !== "boolean") base.delivery_ready = false;
  if (!Number.isInteger(base.render_successes)) base.render_successes = 0;
  if (!Array.isArray(base.render_results)) base.render_results = [];
  if (!Array.isArray(base.optimization_history)) base.optimization_history = [];
  if (!base.infra_attempts || typeof base.infra_attempts !== "object") base.infra_attempts = {};
  if (!("blocked" in base)) base.blocked = null;
  base.min_headroom_bytes = HEADROOM;
  base.max_peak_bytes = MAX_PEAK;
  return base;
}

async function saveState(oldState, state) {
  if (canonical(clean(oldState)) !== canonical(clean(state))) {
    await addComment(RUNTIME, DOCKER, markerBody(STATE_MARKER, state));
  }
}

async function latestArtifactManifest() {
  const pr = await api(OPENCODE, "GET", "pulls/" + OPT_PR);
  const headSha = pr && pr.head && pr.head.sha;
  if (!/^[0-9a-f]{40}$/.test(headSha || "")) return null;
  const rows = markerRows(await comments(OPENCODE, OPT_ISSUE), ARTIFACT_MANIFEST);
  const matches = rows.filter(x =>
    x.head_sha === headSha &&
    Number.isInteger(x.artifact_id) &&
    /^[0-9a-f]{64}$/.test(String(x.binary_sha256 || "")) &&
    /^sha256:[0-9a-f]{64}$/.test(String(x.archive_digest || "")) &&
    String(x.version || "").length > 0
  );
  if (!matches.length) return null;
  const m = clean(matches[matches.length - 1]);
  return {
    schema: "runtime-lab-opencode-artifact/v1",
    repository: OPENCODE,
    pr: OPT_PR,
    branch: "opencode/issue11-max-headless",
    head_sha: m.head_sha,
    built_source_sha: m.built_source_sha || m.head_sha,
    workflow_run_id: m.workflow_run_id,
    artifact_id: m.artifact_id,
    artifact_name: m.artifact_name || "opencode-coding-linux-x64",
    archive_sha256: String(m.archive_digest).replace(/^sha256:/, ""),
    binary_sha256: m.binary_sha256,
    version: m.version,
  };
}

function artifactText(a) {
  return [
    "- Repository: " + a.repository,
    "- PR: #" + a.pr,
    "- Branch: " + a.branch,
    "- Source/head SHA: " + a.head_sha,
    "- Built source SHA: " + a.built_source_sha,
    "- Source workflow run: " + a.workflow_run_id,
    "- Artifact name: " + a.artifact_name,
    "- Artifact ID: " + a.artifact_id,
    "- Artifact archive digest: sha256:" + a.archive_sha256,
    "- Expected binary SHA-256: " + a.binary_sha256,
    "- Expected --version: " + a.version,
  ].join("\n");
}

function qualificationBody(kind, a, generation, passNumber, previous) {
  const title = kind === "docker" ? "Docker qualification" : "Render qualification pass " + passNumber + "/3";
  const renderRules = kind === "docker" ? "" : [
    "",
    "## Render invariants",
    "",
    "- Exactly one automation-owned Render service may exist at a time globally.",
    "- Free tier only; no paid resource.",
    "- One service per attempt; no second service for retry/fallback.",
    "- External evidence must survive worker failure/replacement.",
    "- Verify archive SHA and binary SHA before launch.",
    "- Launch exact binary by deterministic absolute path with no baseline/PATH/HOME/installer fallback.",
    "- Record downloaded SHA separately from /proc/<pid>/exe realpath + SHA-256 + cmdline.",
    "- Cleanup runs on every terminal path and success requires verified service absence.",
  ].join("\n");
  return [
    "<!-- runtime-lab-qualification-managed -->",
    "## " + title,
    "",
    "Generation: " + generation,
    "",
    "## Exact immutable artifact",
    "",
    artifactText(a),
    "",
    "## Coding workload",
    "",
    "Run a real autonomous coding task requiring repository inspection/search, file reasoning,",
    "at least one real edit, shell/build/test execution, and deterministic verification.",
    "A version smoke test, hello-world prompt, documentation-only run, or fake harness does not count.",
    "",
    "## Memory acceptance contract",
    "",
    "- hard limit: 512 MiB / " + LIMIT + " bytes",
    "- swap: disabled",
    "- precommitted minimum headroom: 32 MiB",
    "- accepted peak: <= 480 MiB / " + MAX_PEAK + " bytes",
    "- no OOM/oom_kill",
    "- no cgroup max-pressure event for an accepted pass",
    "- no memory-pressure instance replacement",
    "- exact artifact identity must be proven",
    "- coding/test result must pass",
    "",
    "Memory failure is an intermediate result and feeds profile-guided optimization.",
    "Do not rebuild or substitute a binary inside this stage.",
    "",
    "Previous-stage evidence: " + canonical(previous || {}),
    renderRules,
    "",
    "## Result contract",
    "",
    "Publish one machine-readable result marker for this generation and exact artifact.",
    "Distinguish infrastructure, memory, correctness/capability and identity failures.",
  ].join("\n");
}

function integrationBody(a, generation, results) {
  return [
    "<!-- runtime-lab-qualification-integration -->",
    "## Integrate the verified low-memory OpenCode build",
    "",
    "Generation: " + generation,
    "",
    "## Verified artifact",
    "",
    artifactText(a),
    "",
    "## Qualification proof",
    "",
    "The exact artifact above completed three consecutive full coding passes on Render Free 512 MiB",
    "with the precommitted 32 MiB headroom requirement.",
    "",
    "Evidence: " + canonical({render_passes: results}),
    "",
    "## Integration requirements",
    "",
    "- Integrate this exact fingerprint; never substitute upstream/main/installer binary.",
    "- Preserve SHA verification, absolute-path launch and /proc executable identity proof.",
    "- Preserve free-tier-only, global single-Render-service and mandatory verified deletion invariants.",
    "- Preserve all required coding capabilities and run integration/E2E tests.",
  ].join("\n");
}

async function resetIssue(n, body, labels, state) {
  const current = await issue(RUNTIME, n);
  const existing = labelNames(current);
  const kept = Array.from(existing).filter(x =>
    !x.startsWith("automation:") && !x.startsWith("execution:") && !x.startsWith("priority:")
  );
  const finalLabels = Array.from(new Set(kept.concat(labels).concat(["priority:p0"])));
  await patchIssue(RUNTIME, n, {body, state: "open", labels: finalLabels});
  await addComment(RUNTIME, n, markerBody(RESET_MARKER, {
    schema: "runtime-lab-qualification-result/v1",
    generation: state.generation,
    artifact_id: state.artifact.artifact_id,
    binary_sha256: state.artifact.binary_sha256,
  }));
}

async function pause(n) {
  const obj = await issue(RUNTIME, n);
  const labels = labelNames(obj);
  labels.delete("automation:in-progress");
  labels.add("automation:paused");
  await patchIssue(RUNTIME, n, {labels: Array.from(labels)});
}

function latestResultAfterReset(rows, marker, artifact) {
  const reset = markerRows(rows, RESET_MARKER)
    .filter(x => Number(x.artifact_id) === Number(artifact.artifact_id) && x.binary_sha256 === artifact.binary_sha256)
    .slice(-1)[0];
  const resetAt = reset && reset._created_at ? Date.parse(reset._created_at) : 0;
  const results = markerRows(rows, marker).filter(x =>
    Number(x.artifact_id) === Number(artifact.artifact_id) &&
    x.binary_sha256 === artifact.binary_sha256 &&
    (!x._created_at || Date.parse(x._created_at) >= resetAt)
  );
  return results.length ? results[results.length - 1] : null;
}

async function mergedPrForIssue(n) {
  const prefix = "opencode/issue" + n + "-";
  const rows = await pulls(RUNTIME, "closed");
  return rows.find(pr => pr.merged_at && pr.head && String(pr.head.ref || "").startsWith(prefix)) || null;
}

async function ensureDocker(state) {
  const rows = await comments(RUNTIME, DOCKER);
  const result = latestResultAfterReset(rows, DOCKER_RESULT, state.artifact);
  if (result) return;
  const reset = markerRows(rows, RESET_MARKER)
    .filter(x => Number(x.artifact_id) === Number(state.artifact.artifact_id) && x.binary_sha256 === state.artifact.binary_sha256)
    .slice(-1)[0];
  const obj = await issue(RUNTIME, DOCKER);
  const labels = labelNames(obj);
  if (reset && (labels.has("automation:in-progress") || (labels.has("execution:docker-qualify") && !labels.has("automation:paused")))) {
    return;
  }
  await resetIssue(
    DOCKER,
    qualificationBody("docker", state.artifact, state.generation, null, null),
    ["execution:docker-qualify"],
    state
  );
  await dispatch(RUNTIME, "issue-scheduler.yml", {});
}

function optimizationPayload(state, failure, scope) {
  return {
    schema: "runtime-lab-opencode-memory-optimization/v1",
    generation: state.generation,
    scope,
    artifact: state.artifact,
    failure: clean(failure),
    reproduce: {
      memory_limit_bytes: LIMIT,
      swap: "disabled",
      workload: "real repo search/read/edit + shell/build/test + deterministic verification",
    },
    required_profile: [
      "cgroup memory.current/memory.peak/memory.events",
      "process tree VmRSS/VmHWM",
      "/proc smaps or smaps_rollup for OpenCode and material children",
      "child commands/processes",
      "wall time and exit status",
    ],
    previous_optimizations: state.optimization_history || [],
    do_not_repeat_without_new_evidence: [
      "libvmmalloc/file-backed malloc",
      "memkind transparent tiering",
      "generic malloc interposition when JSC direct mmap is dominant",
      "identical artifact retry",
    ],
  };
}

async function dispatchOptimization(state, failure, scope) {
  const payload = optimizationPayload(state, failure, scope);
  const fingerprint = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(payload)));
  const hex = Array.from(new Uint8Array(fingerprint)).map(x => x.toString(16).padStart(2, "0")).join("");
  const rows = await comments(OPENCODE, OPT_ISSUE);
  const prior = markerRows(rows, OPT_DISPATCHED).find(x => x.fingerprint === hex);
  if (prior) {
    state.stage = "optimizing";
    return;
  }
  await addComment(OPENCODE, OPT_ISSUE, markerBody(OPT_REQUEST, payload));
  await addComment(OPENCODE, OPT_ISSUE, markerBody(OPT_DISPATCHED, {
    schema: "runtime-lab-opencode-memory-optimization-dispatch/v1",
    fingerprint: hex,
    generation: state.generation,
    artifact_id: state.artifact.artifact_id,
    binary_sha256: state.artifact.binary_sha256,
  }));
  const oi = await issue(OPENCODE, OPT_ISSUE);
  const labels = labelNames(oi);
  labels.add("automation:in-progress");
  labels.delete("automation:paused");
  await patchIssue(OPENCODE, OPT_ISSUE, {state: "open", labels: Array.from(labels)});
  await dispatch(OPENCODE, "memory-optimize.yml", {});
  state.stage = "optimizing";
  state.blocked = null;
}

function classification(r) {
  return String((r && r.classification) || "");
}

async function ensureDockerRepair(state, result) {
  const fp = String(result.fingerprint || result.reason || "unknown");
  const key = "docker:" + fp;
  const attempts = Number(state.infra_attempts[key] || 0);
  const marker = REPAIR_PREFIX + " source=" + DOCKER + " fingerprint=" + fp + " -->";
  const all = await paginate(RUNTIME, "issues?state=all&", "");
  const matching = all.filter(x => !x.pull_request && String(x.body || "").includes(marker));
  const open = matching.find(x => x.state === "open");
  if (open) {
    state.stage = "docker-infrastructure-repair";
    return;
  }
  const closed = matching.sort((a, b) => Number(b.number) - Number(a.number))[0];
  if (closed) {
    const merged = await mergedPrForIssue(closed.number);
    if (merged && attempts <= 2) {
      await resetIssue(
        DOCKER,
        qualificationBody("docker", state.artifact, state.generation, null, result),
        ["execution:docker-qualify"],
        state
      );
      await dispatch(RUNTIME, "issue-scheduler.yml", {});
      state.stage = "docker-retry-after-repair";
      return;
    }
  }
  if (attempts >= 2) {
    state.stage = "blocked";
    state.blocked = {reason: "same Docker infrastructure failure repeated after bounded repairs", fingerprint: fp};
    await addComment(RUNTIME, DOCKER, markerBody(BLOCKER, state.blocked));
    return;
  }
  const body = [
    marker,
    "",
    "## Goal",
    "",
    "Repair the Docker qualification infrastructure defect for the exact pinned artifact.",
    "Do not rebuild or substitute OpenCode as a workaround.",
    "",
    "Failure evidence: " + canonical(clean(result)),
    "",
    "After the repair PR merges, the chain retries this exact artifact once.",
  ].join("\n");
  await api(RUNTIME, "POST", "issues", {
    title: "P0: Repair Docker qualification for artifact " + state.artifact.artifact_id,
    body,
    labels: ["priority:p0"],
  });
  state.infra_attempts[key] = attempts + 1;
  state.stage = "docker-infrastructure-repair";
  await dispatch(RUNTIME, "issue-scheduler.yml", {});
}

async function reconcile() {
  const dockerComments = await comments(RUNTIME, DOCKER);
  const oldState = normalizeState(latestMarker(dockerComments, STATE_MARKER));
  const state = normalizeState(oldState);

  const artifact = await latestArtifactManifest();
  if (!artifact) {
    state.stage = "awaiting-artifact-manifest";
    await saveState(oldState, state);
    return;
  }

  const changed = !state.artifact ||
    Number(state.artifact.artifact_id) !== Number(artifact.artifact_id) ||
    state.artifact.binary_sha256 !== artifact.binary_sha256;

  if (changed) {
    if (state.artifact) {
      state.optimization_history.push({
        generation: state.generation,
        artifact_id: state.artifact.artifact_id,
        binary_sha256: state.artifact.binary_sha256,
        terminal_stage: state.stage,
        docker_status: state.docker_status,
        render_results: state.render_results,
      });
    }
    state.generation += 1;
    state.artifact = artifact;
    state.docker_status = "pending";
    state.render_successes = 0;
    state.render_results = [];
    state.stage = "parallel-docker-and-delivery";
    state.blocked = null;
    for (const n of RENDER) await pause(n);
    await ensureDocker(state);
  }

  state.delivery_ready = !!(await mergedPrForIssue(DELIVERY));

  const drows = await comments(RUNTIME, DOCKER);
  const docker = latestResultAfterReset(drows, DOCKER_RESULT, state.artifact);
  if (!docker) {
    state.docker_status = "pending";
    state.stage = "parallel-docker-and-delivery";
    await ensureDocker(state);
    await saveState(oldState, state);
    return;
  }

  state.docker_status = classification(docker);

  if (["memory", "marginal"].includes(classification(docker))) {
    for (const n of RENDER) await pause(n);
    state.render_successes = 0;
    state.render_results = [];
    await dispatchOptimization(state, docker, "docker");
    await saveState(oldState, state);
    return;
  }

  if (["correctness", "capability"].includes(classification(docker))) {
    state.render_successes = 0;
    state.render_results = [];
    await dispatchOptimization(state, docker, "docker-correctness");
    await saveState(oldState, state);
    return;
  }

  if (classification(docker) === "infrastructure") {
    await ensureDockerRepair(state, docker);
    await saveState(oldState, state);
    return;
  }

  if (classification(docker) !== "pass") {
    state.stage = "blocked";
    state.blocked = {reason: "unrecognized Docker result", result: clean(docker)};
    await addComment(RUNTIME, DOCKER, markerBody(BLOCKER, state.blocked));
    await saveState(oldState, state);
    return;
  }

  if (!state.delivery_ready) {
    state.stage = "awaiting-render-delivery-merge";
    await saveState(oldState, state);
    return;
  }

  const successes = Number(state.render_successes || 0);
  if (successes >= 3) {
    state.stage = "integration";
    await resetIssue(
      INTEGRATION,
      integrationBody(state.artifact, state.generation, state.render_results),
      ["execution:render-e2e"],
      state
    );
    await dispatch(RUNTIME, "issue-scheduler.yml", {});
    await saveState(oldState, state);
    return;
  }

  const passIssue = RENDER[successes];
  const passNo = successes + 1;
  const rrows = await comments(RUNTIME, passIssue);
  const rr = latestResultAfterReset(rrows, RENDER_RESULT, state.artifact);

  if (!rr) {
    state.stage = "render-pass-" + passNo;
    const obj = await issue(RUNTIME, passIssue);
    const labels = labelNames(obj);
    const reset = markerRows(rrows, RESET_MARKER)
      .filter(x => Number(x.artifact_id) === Number(state.artifact.artifact_id) && x.binary_sha256 === state.artifact.binary_sha256)
      .slice(-1)[0];
    if (!reset && !labels.has("automation:in-progress")) {
      await resetIssue(
        passIssue,
        qualificationBody("render", state.artifact, state.generation, passNo, state.render_results.slice(-1)[0] || docker),
        ["execution:render-smoke"],
        state
      );
      for (const n of RENDER) if (n !== passIssue) await pause(n);
      await dispatch(RUNTIME, "issue-scheduler.yml", {});
    }
    await saveState(oldState, state);
    return;
  }

  if (classification(rr) === "pass") {
    const already = state.render_results.some(x => Number(x.pass_number) === passNo);
    if (!already) {
      const stored = clean(rr);
      stored.pass_number = passNo;
      state.render_results.push(stored);
      state.render_successes = passNo;
    }
    state.stage = passNo === 3 ? "integration" : "render-pass-" + (passNo + 1);
    await saveState(oldState, state);
    await dispatch(RUNTIME, "qualification-chain.yml", {});
    return;
  }

  if (["memory", "marginal"].includes(classification(rr))) {
    state.render_successes = 0;
    state.render_results = [];
    await dispatchOptimization(state, rr, "render-service");
    await saveState(oldState, state);
    return;
  }

  if (classification(rr) === "infrastructure") {
    state.stage = "render-pass-" + passNo + "-infrastructure-repair";
    await saveState(oldState, state);
    return;
  }

  if (["correctness", "capability", "identity"].includes(classification(rr))) {
    state.render_successes = 0;
    state.render_results = [];
    await dispatchOptimization(state, rr, "render-correctness");
    await saveState(oldState, state);
    return;
  }

  state.stage = "blocked";
  state.blocked = {reason: "unrecognized Render result", result: clean(rr)};
  await addComment(RUNTIME, passIssue, markerBody(BLOCKER, state.blocked));
  await saveState(oldState, state);
}

reconcile().then(() => {
  console.log("qualification chain reconcile completed");
}).catch(err => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
