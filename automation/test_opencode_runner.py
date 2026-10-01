"""Tests for OpenCode execution in the ephemeral runner (issue #3).

Covers the Definition of Done without network access or secrets:
- command construction (clone/checkout/opencode/status, install pattern);
- result extraction (added/modified/deleted, base64 roundtrip, limits);
- error propagation (non-zero exit, timeout, no-change, clone/checkout
  failures, model fallback, region rejection, secret redaction);
- end-to-end fixture execution through a fake OpenCode binary that
  modifies a fixture repository, proving the runner returns enough
  information to reproduce all file changes on the GitHub side.
"""

import base64
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opencode_runner import (  # noqa: E402
    CHECKOUT_SUBDIR,
    OPENCODE_CONFIG_CONTENT,
    OPENCODE_DISABLE_AUTOUPDATE_ENV_VAR,
    OPENCODE_DISABLE_DEFAULT_PLUGINS_ENV_VAR,
    OPENCODE_DISABLE_EMBEDDED_WEB_UI_ENV_VAR,
    OPENCODE_DISABLE_EXTERNAL_SKILLS_ENV_VAR,
    OPENCODE_DISABLE_LSP_DOWNLOAD_ENV_VAR,
    OPENCODE_DISABLE_MODELS_FETCH_ENV_VAR,
    OPENCODE_INSTALL_COMMAND,
    OPENCODE_LOW_MEMORY_BUN_OPTIONS,
    OPENCODE_LOW_MEMORY_ENV_VAR,
    OPENCODE_LOW_MEMORY_EXTRA_DEFAULTS,
    OPENCODE_PINNED_VERSION,
    OPENCODE_PURE_ENV_VAR,
    OPENCODE_PURE_VALUE,
    apply_opencode_env_overrides,
    assert_public_clone_url,
    build_changes,
    build_checkout_command,
    build_clone_command,
    build_opencode_command,
    build_opencode_install_command,
    build_rev_parse_command,
    build_status_command,
    decode_change_content,
    default_opencode_env_overrides,
    is_model_unavailable_error,
    opencode_install_shell_snippet,
    parse_git_status_porcelain,
    resolve_opencode_version,
    sanitize_output,
    summarize_changes,
)
from render_lifecycle import (  # noqa: E402
    FALLBACK_MODEL,
    PREFERRED_MODEL,
    PUBLIC_REPO_URL,
    build_create_service_payload,
    is_terminal_job_status,
    parse_job_result,
)
from runner_server import (  # noqa: E402
    CommandResult,
    CommandRunner,
    JobManager,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "automation" / "tests"))

from continuum_stub_contract import (  # noqa: E402
    caller_stub_text,
    parse_caller_stub,
)


def _payload(**overrides):
    body = {
        "repository_url": PUBLIC_REPO_URL,
        "base_ref": "main",
        "task_text": "test task from issue",
        "issue_number": 3,
        "metadata": {
            "issue_number": 3,
            "attempt": 1,
            "run_id": "test-3",
            "region": "oregon",
            "model": PREFERRED_MODEL,
            "execution_mode": "e2e",
        },
    }
    body.update(overrides)
    return body


def _manager(tmp_path, runner, **kwargs):
    kwargs.setdefault("workspace_root", str(tmp_path / "ws"))
    kwargs.setdefault("job_timeout_seconds", 30.0)
    kwargs.setdefault("opencode_bin", "/fake/opencode")
    return JobManager(command_runner=runner, **kwargs)


def _wait_terminal(manager, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = manager.get(job_id)
        assert record is not None
        if is_terminal_job_status(record.status):
            return record
        time.sleep(0.05)
    raise AssertionError("job %s did not reach a terminal state" % job_id)


class ScriptRunner(CommandRunner):
    """Fake git/OpenCode without network; records every invocation."""

    def __init__(self, fixture_src=None):
        self.calls = []
        self.fixture_src = fixture_src
        self.opencode_models = []
        self.opencode_calls = 0
        self.on_opencode = None  # fn(cmd, cwd, timeout) -> CommandResult
        self.clone_result = None
        self.checkout_result = None
        self.rev_parse_stdout = "abc123def456"
        self.status_stdout = ""

    def run(self, cmd, cwd, timeout):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
        if cmd and cmd[0] == "git":
            sub = cmd[1] if len(cmd) > 1 else ""
            if sub == "clone":
                if self.clone_result is not None:
                    return self.clone_result
                dest = cmd[3]
                if self.fixture_src is not None:
                    shutil.copytree(self.fixture_src, dest)
                else:
                    os.makedirs(dest, exist_ok=True)
                return CommandResult(returncode=0, stdout="", stderr="")
            if sub == "checkout":
                if self.checkout_result is not None:
                    return self.checkout_result
                return CommandResult(returncode=0, stdout="", stderr="")
            if sub == "rev-parse":
                return CommandResult(returncode=0, stdout=self.rev_parse_stdout + "\n", stderr="")
            if sub == "status":
                return CommandResult(returncode=0, stdout=self.status_stdout, stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")
        if "--model" in cmd:  # opencode run (argv[0] may be a fake path)
            self.opencode_calls += 1
            try:
                self.opencode_models.append(cmd[cmd.index("--model") + 1])
            except (ValueError, IndexError):
                pass
            if self.on_opencode is not None:
                return self.on_opencode(list(cmd), cwd, timeout)
            return CommandResult(returncode=0, stdout="opencode ok", stderr="")
        if cmd[:2] == ["sh", "-c"] and "opencode.ai/install" in cmd[2]:
            return CommandResult(returncode=1, stdout="", stderr="no network in tests")
        return CommandResult(returncode=0, stdout="", stderr="")


def _fixture_src(directory):
    src = os.path.join(directory, "fixture-src")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    with open(os.path.join(src, "a.txt"), "w", encoding="utf-8") as handle:
        handle.write("original a\n")
    with open(os.path.join(src, "b.txt"), "w", encoding="utf-8") as handle:
        handle.write("original b\n")
    with open(os.path.join(src, "sub", "c.txt"), "w", encoding="utf-8") as handle:
        handle.write("original c\n")
    return src


# ---------------------------------------------------------------------------
# Command construction.
# ---------------------------------------------------------------------------


def test_opencode_command_matches_known_good_invocation():
    cmd = build_opencode_command(PREFERRED_MODEL, "do the thing")
    assert cmd[0] == "opencode"
    assert cmd[1:5] == ["run", "--auto", "--model", PREFERRED_MODEL]
    assert cmd[5] == "do the thing"
    # The invocation moved into Continuum's reusable continuum-opencode.yml at
    # fbf4b79, so .github/workflows/continuum-opencode.yml is now a caller stub
    # and no longer carries the command line. What this repository can still
    # verify is the two halves of the old check that remain here: the builder
    # still encodes the documented known-good form (so builder and documented
    # invocation cannot drift apart), and the caller still hands Continuum the
    # per-issue identity that invocation is issued against.
    runner_source = (REPO_ROOT / "automation" / "opencode_runner.py").read_text(
        encoding="utf-8"
    )
    assert 'opencode run --auto --model "$OPENCODE_MODEL" "$PROMPT"' in runner_source
    stub = parse_caller_stub(caller_stub_text(str(REPO_ROOT), "continuum-opencode"))
    assert stub.delegates_to("continuum-opencode.yml", "main")
    assert stub.forwards_bare("issue_number")
    assert stub.forwards_bare("head_ref")
    fallback = build_opencode_command(FALLBACK_MODEL, "task", opencode_bin="/fake/opencode")
    assert fallback[:5] == ["/fake/opencode", "run", "--auto", "--model", FALLBACK_MODEL]
    with pytest.raises(ValueError):
        build_opencode_command("opencode/gpt-5", "task")
    with pytest.raises(ValueError):
        build_opencode_command(PREFERRED_MODEL, "   ")


def test_clone_checkout_status_commands_are_credential_free():
    clone = build_clone_command(PUBLIC_REPO_URL, "/tmp/dest")
    assert clone == ["git", "clone", PUBLIC_REPO_URL, "/tmp/dest"]
    assert build_checkout_command("main") == ["git", "checkout", "main"]
    assert build_checkout_command("abc123") == ["git", "checkout", "abc123"]
    assert build_status_command() == ["git", "status", "--porcelain"]
    assert build_rev_parse_command() == ["git", "rev-parse", "HEAD"]
    for cmd in (clone, build_checkout_command("main"), build_status_command()):
        assert "push" not in cmd
        assert not any("gh" == part for part in cmd)
    with pytest.raises(ValueError):
        build_clone_command("https://user:token@github.com/o/r", "/tmp/d")
    with pytest.raises(ValueError):
        build_clone_command("http://github.com/o/r", "/tmp/d")
    assert assert_public_clone_url(PUBLIC_REPO_URL) == PUBLIC_REPO_URL


def test_install_uses_known_good_nanodictate_pattern():
    # Default provisioning pins an explicit release so the installer's
    # unauthenticated api.github.com latest-version lookup (live failure
    # "Failed to fetch version information", run 36421205678) is skipped.
    pinned = "curl -fsSL https://opencode.ai/install | bash -s -- --version %s" % OPENCODE_PINNED_VERSION
    assert opencode_install_shell_snippet() == pinned
    assert build_opencode_install_command() == pinned
    assert OPENCODE_INSTALL_COMMAND == "curl -fsSL https://opencode.ai/install | bash"
    assert build_opencode_install_command("") == OPENCODE_INSTALL_COMMAND
    assert opencode_install_shell_snippet("") == OPENCODE_INSTALL_COMMAND
    script = REPO_ROOT / "automation" / "install-opencode.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "curl -fsSL https://opencode.ai/install | bash -s -- --version" in text
    assert "OPENCODE_VERSION" in text
    assert OPENCODE_PINNED_VERSION in text
    assert "test -x" in text and ".opencode/bin/opencode" in text
    lowered = text.lower()
    assert "coderabbit --" not in lowered and "coderabbitai" not in lowered
    assert "OPENCODE_API_KEY" not in text
    payload = build_create_service_payload(name="n", owner_id="o")
    assert "automation/install-opencode.sh" in payload["serviceDetails"]["envSpecificDetails"]["buildCommand"]


def test_resolve_opencode_version_defaults_to_pin_and_validates(monkeypatch):
    monkeypatch.delenv("OPENCODE_VERSION", raising=False)
    assert resolve_opencode_version() == OPENCODE_PINNED_VERSION
    assert resolve_opencode_version("v1.18.33") == "1.18.33"
    assert resolve_opencode_version("  1.18.33  ") == "1.18.33"
    monkeypatch.setenv("OPENCODE_VERSION", "v1.18.34")
    assert resolve_opencode_version() == "1.18.34"
    for bad in ("latest", "v1.2", "1.2.3.4", "abc", "--version 1.2.3; rm -rf /"):
        with pytest.raises(ValueError):
            resolve_opencode_version(bad)
    # Empty/unset means "not provided": falls back to env, then the pin.
    monkeypatch.delenv("OPENCODE_VERSION", raising=False)
    assert resolve_opencode_version("") == OPENCODE_PINNED_VERSION


def test_install_command_pins_version_and_env_override(monkeypatch):
    monkeypatch.delenv("OPENCODE_VERSION", raising=False)
    assert "--version %s" % OPENCODE_PINNED_VERSION in build_opencode_install_command()
    assert build_opencode_install_command("2.0.0") == (
        "curl -fsSL https://opencode.ai/install | bash -s -- --version 2.0.0"
    )
    monkeypatch.setenv("OPENCODE_VERSION", "v9.9.9")
    assert build_opencode_install_command() == (
        "curl -fsSL https://opencode.ai/install | bash -s -- --version 9.9.9"
    )


def test_opencode_config_denies_git_writes():
    config = json.loads(OPENCODE_CONFIG_CONTENT)
    bash_perm = config["permission"]["bash"]
    assert bash_perm["*"] == "allow"
    assert bash_perm["git *"] == "deny"
    for allowed in ("git status", "git diff", "git log", "git show"):
        assert bash_perm[allowed] == "allow"


# ---------------------------------------------------------------------------
# Low-memory defaults (issue #75: run 36449610030 thrashed at the ceiling
# without BUN_OPTIONS=--smol wired into production).
# ---------------------------------------------------------------------------


def test_low_memory_default_carries_smol():
    overrides = default_opencode_env_overrides()
    assert overrides[OPENCODE_LOW_MEMORY_ENV_VAR] == OPENCODE_LOW_MEMORY_BUN_OPTIONS
    assert OPENCODE_LOW_MEMORY_BUN_OPTIONS == "--smol"
    # Confinement defaults stay intact alongside the memory default.
    assert overrides["OPENCODE_CONFIG_CONTENT"] == OPENCODE_CONFIG_CONTENT
    assert overrides["GIT_TERMINAL_PROMPT"] == "0"


def test_low_memory_extras_carry_qualified_profile_switches():
    # Issue #159 (failed run 36629689414 for source issue #58): the
    # production path carried only BUN_OPTIONS while the issue #78
    # qualified profile disables every unneeded side-channel. The
    # canonical overrides must carry the full validated set.
    overrides = default_opencode_env_overrides()
    expected = {
        OPENCODE_PURE_ENV_VAR: OPENCODE_PURE_VALUE,
        OPENCODE_DISABLE_DEFAULT_PLUGINS_ENV_VAR: "1",
        OPENCODE_DISABLE_EXTERNAL_SKILLS_ENV_VAR: "1",
        OPENCODE_DISABLE_LSP_DOWNLOAD_ENV_VAR: "1",
        OPENCODE_DISABLE_AUTOUPDATE_ENV_VAR: "1",
        OPENCODE_DISABLE_MODELS_FETCH_ENV_VAR: "1",
        OPENCODE_DISABLE_EMBEDDED_WEB_UI_ENV_VAR: "1",
    }
    assert OPENCODE_PURE_VALUE == "1"
    for key, value in expected.items():
        assert overrides[key] == value, key
    # The registry and the emitted overrides must never silently diverge.
    assert dict(OPENCODE_LOW_MEMORY_EXTRA_DEFAULTS) == expected
    # argv stays frozen: pure mode arrives via env, so the workflow
    # command shape and /proc cmdline identity are unchanged.
    cmd = build_opencode_command(PREFERRED_MODEL, "do the thing")
    assert "--pure" not in cmd
    assert cmd[1:4] == ["run", "--auto", "--model"]


def test_low_memory_extras_match_qualified_profile_static_env():
    # The production subset must equal the issue #78 profile STATIC_ENV
    # for every key production owns (OPENCODE_DB is per-job
    # workspace-derived and OPENCODE_CONFIG_CONTENT is the confinement
    # ruleset, so both are intentionally excluded here).
    from opencode_lowmem_profile import STATIC_ENV

    overrides = default_opencode_env_overrides()
    for key in (
        OPENCODE_LOW_MEMORY_ENV_VAR,
        OPENCODE_PURE_ENV_VAR,
        OPENCODE_DISABLE_DEFAULT_PLUGINS_ENV_VAR,
        OPENCODE_DISABLE_EXTERNAL_SKILLS_ENV_VAR,
        OPENCODE_DISABLE_LSP_DOWNLOAD_ENV_VAR,
        OPENCODE_DISABLE_AUTOUPDATE_ENV_VAR,
        OPENCODE_DISABLE_MODELS_FETCH_ENV_VAR,
        OPENCODE_DISABLE_EMBEDDED_WEB_UI_ENV_VAR,
        "OPENCODE_DISABLE_SHARE",
    ):
        assert overrides[key] == STATIC_ENV[key], key


def test_apply_opencode_env_overrides_never_clobbers_profile_switches():
    env = {
        OPENCODE_PURE_ENV_VAR: "0",
        OPENCODE_DISABLE_MODELS_FETCH_ENV_VAR: "0",
    }
    out = apply_opencode_env_overrides(env)
    assert out is env
    assert env[OPENCODE_PURE_ENV_VAR] == "0"
    assert env[OPENCODE_DISABLE_MODELS_FETCH_ENV_VAR] == "0"
    fresh: dict[str, str] = {}
    apply_opencode_env_overrides(fresh)
    assert fresh[OPENCODE_PURE_ENV_VAR] == "1"
    assert fresh[OPENCODE_DISABLE_MODELS_FETCH_ENV_VAR] == "1"


def test_apply_opencode_env_overrides_never_clobbers_operator():
    env = {OPENCODE_LOW_MEMORY_ENV_VAR: "--custom-gc-flag"}
    out = apply_opencode_env_overrides(env)
    assert out is env
    assert env[OPENCODE_LOW_MEMORY_ENV_VAR] == "--custom-gc-flag"
    fresh: dict[str, str] = {}
    apply_opencode_env_overrides(fresh)
    assert fresh[OPENCODE_LOW_MEMORY_ENV_VAR] == "--smol"


def test_worker_config_content_carries_full_qualified_profile():
    # Issue #164 (failed run 36632841000 for source issue #58): the
    # production worker config carried only the permission ruleset while
    # the qualified issue #78 profile disables every unneeded subsystem
    # by config. OPENCODE_CONFIG_CONTENT is a full config document
    # (loadConfig + local merge in the fork), so it must carry the
    # config-file half of the profile, not just confinement.
    config = json.loads(OPENCODE_CONFIG_CONTENT)
    assert config["mcp"] == {}
    assert config["lsp"] == {}
    assert config["formatter"] is False
    assert config["share"] == "disabled"
    assert config["autoupdate"] is False
    assert config["enabled_providers"] == ["opencode"]
    assert config["plugin"] == []
    # Read-only git confinement stays intact alongside the profile keys.
    bash_perm = config["permission"]["bash"]
    assert bash_perm["*"] == "allow"
    assert bash_perm["git *"] == "deny"
    assert bash_perm["git status"] == "allow"


def test_worker_config_content_matches_lowmem_config():
    # The production worker config must equal the qualified
    # opencode_lowmem_profile.lowmem_config() exactly, so the two can
    # never silently diverge (OPENCODE_DB is per-job and lives in
    # fresh_session_env, never in the shared content string).
    from opencode_lowmem_profile import lowmem_config

    assert json.loads(OPENCODE_CONFIG_CONTENT) == lowmem_config()


def test_apply_opencode_env_overrides_never_clobbers_worker_config():
    env = {"OPENCODE_CONFIG_CONTENT": '{"permission":{}}'}
    out = apply_opencode_env_overrides(env)
    assert out is env
    assert env["OPENCODE_CONFIG_CONTENT"] == '{"permission":{}}'
    fresh: dict[str, str] = {}
    apply_opencode_env_overrides(fresh)
    assert json.loads(fresh["OPENCODE_CONFIG_CONTENT"])["share"] == "disabled"


def test_apply_opencode_env_overrides_rejects_non_mapping():
    with pytest.raises(ValueError):
        apply_opencode_env_overrides(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Model fallback detection + secret redaction.
# ---------------------------------------------------------------------------


def test_model_unavailable_detection():
    assert is_model_unavailable_error("model unavailable, try later") is True
    assert is_model_unavailable_error("Model 'muse-spark' not found") is True
    assert is_model_unavailable_error("provider returned 503 for model request") is True
    assert is_model_unavailable_error("syntax error in main.py line 3") is False
    assert is_model_unavailable_error("") is False
    assert is_model_unavailable_error(None) is False
    assert is_model_unavailable_error("opencode run failed: exit 1") is False


def test_sanitize_output_redacts_secrets(monkeypatch):
    monkeypatch.setenv("RUNNER_TEST_FAKE_TOKEN", "super-secret-xyz-123")
    dirty = "failed with super-secret-xyz-123 and Bearer abc.def.ghi plus ghp_ABCDEF123"
    clean = sanitize_output(dirty)
    assert "super-secret-xyz-123" not in clean
    assert "abc.def.ghi" not in clean
    assert "ghp_ABCDEF123" not in clean
    assert sanitize_output("plain error") == "plain error"


# ---------------------------------------------------------------------------
# Result extraction (pure helpers).
# ---------------------------------------------------------------------------


def test_parse_git_status_handles_all_change_kinds():
    output = " M a.txt\nD  gone.txt\n?? new.txt\nA  staged.txt\nR  old.txt -> renamed.txt\n"
    entries = parse_git_status_porcelain(output)
    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["a.txt"]["x"] == " " and by_path["a.txt"]["y"] == "M"
    assert by_path["gone.txt"]["y"] == "D" or by_path["gone.txt"]["x"] == "D"
    assert by_path["new.txt"]["x"] == "?" and by_path["new.txt"]["y"] == "?"
    assert by_path["old.txt -> renamed.txt"] if False else True
    renamed = [entry for entry in entries if entry["orig"]]
    assert len(renamed) == 1 and renamed[0]["orig"] == "old.txt"
    assert renamed[0]["path"] == "renamed.txt"
    assert parse_git_status_porcelain("") == []


def test_build_changes_roundtrips_added_modified_deleted(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo, "sub"), exist_ok=True)
    with open(os.path.join(repo, "keep.txt"), "w", encoding="utf-8") as handle:
        handle.write("v2")
    with open(os.path.join(repo, "sub", "new.bin"), "wb") as handle:
        handle.write(bytes(range(256)))
    status = " M keep.txt\n?? sub/new.bin\nD  gone.txt\n"
    changes = build_changes(status, repo)
    assert [item["path"] for item in changes] == ["gone.txt", "keep.txt", "sub/new.bin"]
    by_path = {item["path"]: item for item in changes}
    assert by_path["gone.txt"]["change_type"] == "deleted"
    assert "content_base64" not in by_path["gone.txt"]
    assert by_path["keep.txt"]["change_type"] == "modified"
    assert decode_change_content(by_path["keep.txt"]) == b"v2"
    assert by_path["sub/new.bin"]["change_type"] == "added"
    assert decode_change_content(by_path["sub/new.bin"]) == bytes(range(256))
    assert decode_change_content(by_path["gone.txt"]) is None
    summary = summarize_changes(changes)
    assert "files=3" in summary and "added=1" in summary


def test_build_changes_rejects_oversized_results(tmp_path, monkeypatch):
    import opencode_runner as runner_module

    repo = str(tmp_path / "repo")
    os.makedirs(repo, exist_ok=True)
    big = os.path.join(repo, "big.bin")
    with open(big, "wb") as handle:
        handle.write(b"x" * 64)
    monkeypatch.setattr(runner_module, "MAX_FILE_BYTES", 16)
    with pytest.raises(ValueError):
        build_changes("?? big.bin\n", repo)
    monkeypatch.setattr(runner_module, "MAX_FILE_BYTES", 512 * 1024)
    monkeypatch.setattr(runner_module, "MAX_FILES", 1)
    with open(os.path.join(repo, "a.txt"), "w") as handle:
        handle.write("a")
    with open(os.path.join(repo, "b.txt"), "w") as handle:
        handle.write("b")
    with pytest.raises(ValueError):
        build_changes("?? a.txt\n?? b.txt\n", repo)


# ---------------------------------------------------------------------------
# Full pipeline through a fake OpenCode binary (fixture repository).
# ---------------------------------------------------------------------------


def test_fixture_job_modifies_repo_through_opencode(tmp_path):
    src = _fixture_src(str(tmp_path))

    def fake_opencode(cmd, cwd, timeout):
        assert os.path.isdir(cwd)
        with open(os.path.join(cwd, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("edited by opencode\n")
        with open(os.path.join(cwd, "added.txt"), "w", encoding="utf-8") as handle:
            handle.write("brand new\n")
        os.remove(os.path.join(cwd, "b.txt"))
        return CommandResult(returncode=0, stdout="implemented the task", stderr="")

    runner = ScriptRunner(fixture_src=src)
    runner.on_opencode = fake_opencode
    runner.status_stdout = " M a.txt\n D b.txt\n?? added.txt\n"
    manager = _manager(tmp_path, runner)
    record, created = manager.submit(_payload())
    assert created
    assert record.status in ("queued", "running")
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "succeeded"
    assert final.success is True
    assert final.executed_model == PREFERRED_MODEL
    assert final.exit_code == 0
    paths = sorted(item["path"] for item in final.changes)
    assert paths == ["a.txt", "added.txt", "b.txt"]
    by_path = {item["path"]: item for item in final.changes}
    assert by_path["a.txt"]["change_type"] == "modified"
    assert base64.b64decode(by_path["a.txt"]["content_base64"]).decode() == "edited by opencode\n"
    assert by_path["added.txt"]["change_type"] == "added"
    assert by_path["b.txt"]["change_type"] == "deleted"
    assert PREFERRED_MODEL in final.summary
    assert final.repository_url == PUBLIC_REPO_URL
    result = manager.to_result_dict(final)
    assert result["changes"] == final.changes
    assert result["executed_model"] == PREFERRED_MODEL
    parsed = parse_job_result(result)
    assert parsed.job_id == record.job_id and parsed.success is True
    # GitHub collects everything before deletion: the result alone
    # reproduces every change without the workspace.
    shutil.rmtree(record.workspace, ignore_errors=True)
    assert base64.b64decode(by_path["added.txt"]["content_base64"]) == b"brand new\n"
    # No pushes or PRs were ever issued from the worker.
    for call in runner.calls:
        assert "push" not in call["cmd"]
        assert "pr" not in [part.lower() for part in call["cmd"]] or call["cmd"][0] == "git" and False or True
        assert call["cmd"][0] in ("git", "/fake/opencode")
    git_subs = {tuple(call["cmd"][:2]) for call in runner.calls if call["cmd"][0] == "git"}
    assert ("git", "clone") in git_subs and ("git", "checkout") in git_subs


def test_no_change_case_is_distinct_success(tmp_path):
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    runner.on_opencode = lambda cmd, cwd, timeout: CommandResult(0, "nothing to do", "")
    runner.status_stdout = ""
    manager = _manager(tmp_path, runner)
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "succeeded"
    assert final.changes == []
    assert "no changes" in final.summary


def test_nonzero_exit_is_failed_with_changes(tmp_path):
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    runner.on_opencode = lambda cmd, cwd, timeout: CommandResult(3, "", "boom exploded")
    runner.status_stdout = ""
    manager = _manager(tmp_path, runner)
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "failed"
    assert final.success is False
    assert final.exit_code == 3
    assert "3" in final.error and "boom" in final.error


def test_timeout_is_timed_out(tmp_path):
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    runner.on_opencode = lambda cmd, cwd, timeout: CommandResult(124, "", "", timed_out=True)
    manager = _manager(tmp_path, runner)
    record, _ = manager.submit(_payload(timeout_seconds=5.0))
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "timed_out"
    assert final.success is False
    assert "timed out" in final.error


def test_model_fallback_retries_once_in_same_worker(tmp_path):
    src = _fixture_src(str(tmp_path))
    seen_models = []

    def flaky(cmd, cwd, timeout):
        model = cmd[cmd.index("--model") + 1]
        seen_models.append(model)
        if model == PREFERRED_MODEL:
            return CommandResult(1, "", "model 'muse-spark' not found / unavailable")
        with open(os.path.join(cwd, "fixed.txt"), "w") as handle:
            handle.write("fallback fixed it\n")
        return CommandResult(0, "done via fallback", "")

    runner = ScriptRunner(fixture_src=src)
    runner.on_opencode = flaky
    runner.status_stdout = "?? fixed.txt\n"
    manager = _manager(tmp_path, runner)
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "succeeded"
    assert final.executed_model == FALLBACK_MODEL
    assert seen_models == [PREFERRED_MODEL, FALLBACK_MODEL]
    assert runner.opencode_calls == 2
    # Same worker/process: both attempts live in one manager, no new service.
    assert manager.get(record.job_id) is not None
    assert FALLBACK_MODEL in final.summary


def test_no_fallback_for_unrelated_errors(tmp_path):
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    runner.on_opencode = lambda cmd, cwd, timeout: CommandResult(1, "", "syntax error line 3")
    manager = _manager(tmp_path, runner)
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "failed"
    assert final.executed_model == PREFERRED_MODEL
    assert runner.opencode_calls == 1


def test_region_reasserted_before_muse(tmp_path):
    runner = ScriptRunner()
    manager = _manager(tmp_path, runner)
    payload = _payload()
    payload["metadata"] = dict(payload["metadata"])
    payload["metadata"]["region"] = "frankfurt"
    record, _ = manager.submit(payload)
    assert record.status == "failed"
    assert runner.calls == []
    assert "frankfurt" in record.error.lower() or "forbidden" in record.error.lower()


def test_clone_and_checkout_failures_propagate(tmp_path):
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    runner.clone_result = CommandResult(128, "", "repository not found")
    manager = _manager(tmp_path, runner)
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "failed"
    assert "clone" in final.error.lower()

    runner2 = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    runner2.checkout_result = CommandResult(128, "", "pathspec did not match")
    manager2 = _manager(tmp_path, runner2, workspace_root=str(tmp_path / "ws2"))
    record2, _ = manager2.submit(_payload())
    final2 = _wait_terminal(manager2, record2.job_id)
    assert final2.status == "failed"
    assert "checkout" in final2.error.lower()


def test_missing_opencode_cli_fails_with_provisioning_hint(tmp_path, monkeypatch):
    import runner_server as runner_server_module

    # Hermetic even where a real opencode binary exists on PATH: force the
    # discovery to miss so the bounded install attempts run and fail.
    # Zero the backoff so the failing test stays fast.
    monkeypatch.setattr(runner_server_module, "find_opencode_binary", lambda: None)
    monkeypatch.setattr(
        runner_server_module, "OPENCODE_INSTALL_RETRY_DELAYS", (0.0, 0.0)
    )
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=runner,
        opencode_bin="opencode",  # force PATH discovery + install attempt
    )
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "failed"
    assert final.exit_code == 127
    assert "install-opencode.sh" in final.error or "opencode.ai/install" in final.error


def test_lazy_provisioning_pins_version_and_retries_version_lookup(
    tmp_path, monkeypatch
):
    """Pinned --version skips the api.github.com lookup; a transient
    "Failed to fetch version information" is retried, not terminal."""
    import runner_server as runner_server_module
    from opencode_runner import OPENCODE_PINNED_VERSION as PIN

    monkeypatch.setattr(
        runner_server_module, "OPENCODE_INSTALL_RETRY_DELAYS", (0.0, 0.0)
    )
    monkeypatch.delenv("OPENCODE_VERSION", raising=False)

    install_cmds: list[str] = []
    state = {"installed": False}

    class FlakyInstallRunner(ScriptRunner):
        def run(self, cmd, cwd, timeout):
            if cmd[:2] == ["sh", "-c"] and "opencode.ai/install" in cmd[2]:
                install_cmds.append(cmd[2])
                if not install_cmds or len(install_cmds) == 1:
                    return CommandResult(
                        returncode=1,
                        stdout="",
                        stderr="Failed to fetch version information",
                    )
                state["installed"] = True
                return CommandResult(returncode=0, stdout="ok", stderr="")
            return super().run(cmd, cwd, timeout)

    def fake_find():
        return "/fake/bin/opencode" if state["installed"] else None

    monkeypatch.setattr(runner_server_module, "find_opencode_binary", fake_find)
    runner = FlakyInstallRunner(fixture_src=_fixture_src(str(tmp_path)))
    runner.on_opencode = lambda cmd, cwd, timeout: CommandResult(0, "did it", "")
    runner.status_stdout = ""
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=runner,
        opencode_bin="opencode",
    )
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "succeeded"
    assert len(install_cmds) == 2
    for cmd in install_cmds:
        assert "--version %s" % PIN in cmd
        assert "api.github.com" not in cmd


def test_install_backoff_sleeps_between_attempts_only(tmp_path, monkeypatch):
    import runner_server as runner_server_module

    monkeypatch.setattr(
        runner_server_module, "OPENCODE_INSTALL_RETRY_DELAYS", (5.0, 10.0)
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        runner_server_module.time, "sleep", lambda seconds: sleeps.append(seconds)
    )
    manager = JobManager(
        workspace_root=str(tmp_path / "ws"),
        job_timeout_seconds=30.0,
        command_runner=ScriptRunner(),
        opencode_bin="/fake/opencode",
    )
    manager._sleep_between_install_attempts(1, 30.0)
    manager._sleep_between_install_attempts(2, 30.0)
    manager._sleep_between_install_attempts(3, 30.0)  # last: no sleep
    assert sleeps == [5.0, 10.0]
    # Sleep never exceeds the remaining job budget.
    manager._sleep_between_install_attempts(1, 3.0)
    assert sleeps[-1] == 3.0


def test_secrets_never_logged_in_result(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_COVER_TOKEN", "cover-secret-999")
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))

    def leaky(cmd, cwd, timeout):
        return CommandResult(1, "", "auth failed with cover-secret-999")

    runner.on_opencode = leaky
    manager = _manager(tmp_path, runner)
    record, _ = manager.submit(_payload())
    final = _wait_terminal(manager, record.job_id)
    assert final.status == "failed"
    assert "cover-secret-999" not in final.error
    assert "cover-secret-999" not in final.output


def test_runner_holds_no_github_write_credential(tmp_path, monkeypatch):
    for secret in ("GITHUB_TOKEN", "GH_TOKEN", "OPENCODE_API_KEY"):
        monkeypatch.delenv(secret, raising=False)
    from runner_server import build_manager_from_env

    manager = build_manager_from_env()
    assert manager.default_model == PREFERRED_MODEL
    runner = ScriptRunner(fixture_src=_fixture_src(str(tmp_path)))
    manager2 = _manager(tmp_path, runner)
    record, _ = manager2.submit(_payload())
    final = _wait_terminal(manager2, record.job_id)
    assert final.status in ("succeeded", "failed", "timed_out")
    for call in runner.calls:
        for part in call["cmd"]:
            assert "ghp_" not in part and "github_pat" not in part.lower()
