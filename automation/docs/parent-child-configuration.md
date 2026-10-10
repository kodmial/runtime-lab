# Parent/child configuration

The authoritative inventory is stored in **GitHub Actions repository variables**, never in tracked workflows, scripts, or source files.

The parent sets `CONTINUUM_ROLE=parent` and `CONTINUUM_CHILDREN` (a JSON array of opaque IDs). Each child sets `CONTINUUM_ROLE=child`, `CONTINUUM_CHILD_ID` (matching one parent ID), and `CONTINUUM_PARENT` (the parent repository's current full name). Review selection is independent: set `CONTINUUM_REVIEW_PROVIDER` to the desired provider in the relevant repository, with `pr-agent` for the PR-Agent delegated path. Configure `CONTINUUM_VALIDATION_SCRIPT` per child to an existing, trusted base-branch script when deterministic delegated validation is required.

## Enrollment and audit

1. Use authorized repository settings or configuration management to provision the parent/child variables; do not commit mappings, numeric repository IDs, opaque child IDs, or access tokens. Preserve existing IDs when adding children.
2. Grant the parent's `TAP_PAT` the necessary permission to discover repositories and read Actions variables. GitHub Actions `GITHUB_TOKEN` alone may not be able to inspect another repository's variables.
3. Run **Verify configured provider children** manually from the parent Actions tab. It reads all variable pages, discovers eligible repositories without visibility filtering, verifies each allowed child uniquely, and fails closed on API outages, missing relationships, and ambiguities.
4. Run a real delegated issue/PR flow with exact-HEAD validation and review. A successful audit proves the bindings, **not** that task execution or PR-Agent credentials are healthy.

The verifier is intentionally read-only. Neither saving this file nor running it can overwrite `CONTINUUM_CHILDREN`, relabel a child, or select a different review provider. First-time enrollment is a GitHub Actions configuration operation, not a source-code change. No additional bootstrap mapping is introduced.

The old workflow containing a fixed inventory has been retired. Its prior commits remain in Git history; removing them from history would require a disruptive history rewrite and is unnecessary for runtime behavior. See the generic Continuum parent/child delegation contract for discovery rules.
