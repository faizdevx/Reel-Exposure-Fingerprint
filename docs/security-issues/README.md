# Security issues to file

**All four are fixed (2026-10-09); file them, if at all, as closed issues that link the fix.** These are the issues for the HIGH findings of `docs/security-audit.md` (there are no CRITICAL findings). They could not be created on GitHub when the audit was run: no GitHub CLI, no token and no git remote in the environment, and the repository has no commits.

Each file is one issue: the first line is the title, the rest is the body. Once the repository has a remote and `gh` is signed in, from the repository root:

```
gh issue create --title "$(head -n 1 docs/security-issues/H-1-no-rate-limiting.md)" --body-file <(tail -n +3 docs/security-issues/H-1-no-rate-limiting.md) --label security
gh issue create --title "$(head -n 1 docs/security-issues/H-2-unbounded-request-bodies.md)" --body-file <(tail -n +3 docs/security-issues/H-2-unbounded-request-bodies.md) --label security
gh issue create --title "$(head -n 1 docs/security-issues/H-3-shared-cache-poisoning.md)" --body-file <(tail -n +3 docs/security-issues/H-3-shared-cache-poisoning.md) --label security
gh issue create --title "$(head -n 1 docs/security-issues/H-4-no-hosted-deployment-tls.md)" --body-file <(tail -n +3 docs/security-issues/H-4-no-hosted-deployment-tls.md) --label security
```

Create the `security` label first (`gh label create security`) if it does not exist. After filing, put the issue numbers in the table of `docs/security-audit.md`.

| File | Finding |
|---|---|
| `H-1-no-rate-limiting.md` | H-1 |
| `H-2-unbounded-request-bodies.md` | H-2 |
| `H-3-shared-cache-poisoning.md` | H-3 |
| `H-4-no-hosted-deployment-tls.md` | H-4 |
