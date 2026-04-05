---
name: pr-merge
description: This skill should be used when the user asks to "merge a PR", "review and merge pull requests", "integrate external contributions", "handle PR conflicts", or needs to merge GitHub PRs while maximizing contributor attribution.
---

# PR Merge — Contributor-First Pull Request Integration

Merge external pull requests while maximizing original author attribution. The core principle: **merge first, resolve conflicts after** — never rewrite a contributor's work from scratch.

## When to Use

- Merging open PRs from external contributors
- Handling PRs that conflict with local changes
- Selectively merging parts of a PR (e.g., skip auto-compact but keep --resume)
- Batch-merging multiple PRs in dependency order

## Core Principles

1. **Preserve authorship** — use `gh pr merge --squash` for clean PRs, or `git merge` for conflict resolution. Never `git apply` + commit as yourself.
2. **Merge first, fix after** — accept the PR's approach even if it differs from local style. Fix conflicts in a separate commit.
3. **Selective merge is OK** — exclude specific files with `--exclude` when a PR contains features already implemented locally. Document what was excluded and why.
4. **Author attribution** — when committing manually, use `--author="Name <email>"`. For squash merges via GitHub, the original author is preserved automatically.

## Workflow

### Step 1: Triage Open PRs

```bash
gh pr list --repo OWNER/REPO --state open \
  --json number,title,author,additions,deletions,mergeable
```

Classify each PR:
- **Merge directly**: Clean, no conflicts, tests pass
- **Merge with conflict resolution**: Good feature but conflicts with local changes
- **Selective merge**: PR contains multiple features, some already implemented locally
- **Close**: Duplicate of another PR, or non-code contribution

### Step 2: Check for Conflicts

```bash
# For each PR, check mergeable status
gh pr view NUMBER --repo OWNER/REPO \
  --json mergeable,mergeStateStatus,files
```

### Step 3: Merge Clean PRs via GitHub

Prefer `gh pr merge` — it preserves the author automatically:

```bash
# Squash merge (preferred for single-feature PRs)
gh pr merge NUMBER --repo OWNER/REPO --squash \
  --subject "feat: description (#NUMBER)"

# Merge commit (for multi-commit PRs where history matters)
gh pr merge NUMBER --repo OWNER/REPO --merge
```

### Step 4: Handle Conflicting PRs

When a PR has conflicts, fetch and merge locally:

```bash
# Fetch the PR branch
git fetch origin pull/NUMBER/head:pr-NUMBER

# Merge into main
git merge pr-NUMBER --no-edit

# If conflicts:
# 1. Resolve in favor of BOTH changes where possible
# 2. Keep the PR author's approach for their feature
# 3. Keep local approach only for genuinely conflicting features
git add -A && git commit --no-edit
```

### Step 5: Selective Merge

When a PR contains features already implemented locally:

```bash
# Apply PR diff excluding specific files
gh pr diff NUMBER --repo OWNER/REPO | \
  git apply --exclude='path/to/skip.py' --exclude='CHANGELOG.md'

# Commit with original author
git add -A
git commit --author="Author Name <author@email>" \
  -m "feat: description from PR (#NUMBER)

Cherry-picked from PR #NUMBER by AUTHOR. Excluded: file.py (already
implemented locally with different approach)."
```

### Step 6: Post-Merge

```bash
# Verify tests pass
python -m pytest tests/ -q

# Verify lint passes
python -m ruff check src/ tests/

# Push
git push origin main

# If PR was manually merged, close it with a comment
gh pr close NUMBER --repo OWNER/REPO \
  --comment "Merged selectively in commit HASH. Excluded X because Y. Thank you!"
```

## Handling Common Scenarios

### Duplicate PRs (Same Bug, Different Fix)

Merge the cleaner/smaller PR. Close the other with acknowledgment:

```bash
gh pr close NUMBER --comment \
  "This issue was fixed via PR #OTHER (a smaller patch targeting the same bug). \
Thank you for the contribution — your investigation helped confirm the root cause!"
```

### PR Contains Both Wanted and Unwanted Features

Example: PR has auto-compact (unwanted, already implemented) + --resume (wanted):

```bash
# Apply excluding the unwanted file
gh pr diff NUMBER | git apply \
  --exclude='src/engine/query_engine.py' \
  --exclude='CHANGELOG.md'

# Handle conflicting UI files manually
# ... edit files to add only the wanted changes ...

# Commit with original author
git commit --author="Original Author <email>" \
  -m "feat: wire --resume/--continue CLI flags (#NUMBER)

Cherry-picked from PR #NUMBER. Excluded auto-compact (already implemented
with LLM-based approach from reference source)."
```

### PR From Fork (No Direct Branch Access)

```bash
# Fetch via PR ref
git fetch origin pull/NUMBER/head:pr-NUMBER
git merge pr-NUMBER
# Resolve conflicts, push
```

## Test After Merge

After merging PRs, always run the harness-eval skill to verify nothing broke:

1. Run `python -m ruff check src/ tests/` — lint must pass
2. Run `python -m pytest tests/ -q` — unit tests must pass
3. Run real agent loop tests on an unfamiliar codebase (see `harness-eval` skill)
4. Verify CI is green after pushing

## Attribution Checklist

Before pushing a merged PR:

- [ ] Original author appears in `git log` (via `--author` or GitHub squash merge)
- [ ] Commit message references the PR number (`#NUMBER`)
- [ ] If selectively merged, commit body explains what was excluded and why
- [ ] Closed PRs have a comment thanking the contributor and explaining the decision
- [ ] Duplicate PRs acknowledge the contributor's investigation

## Common Mistakes

- **`git apply` + self-commit**: Loses author attribution. Use `--author` flag or `gh pr merge`.
- **Rewriting from scratch**: Even if the contributor's code style differs, merge their code and fix style after. Their commit should be in the history.
- **Force-pushing after merge**: Rewrites history and may remove contributor commits. Use `--force-with-lease` only on feature branches, never on main after a merge.
- **Forgetting CHANGELOG conflicts**: Always exclude CHANGELOG.md from apply and handle it manually — it conflicts on every PR.
- **Not testing after merge**: A clean merge doesn't mean working code. Always run tests.
