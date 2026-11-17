#!/usr/bin/env python3
"""Keep open `content-draft` pull requests mergeable.

Every draft PR is cut from the default branch before the others are merged, so each one appends its own rows to the end of
content.csv and they conflict as soon as a sibling lands. This merges the default branch into each open draft branch and, when
the only conflict is content.csv, resolves it by taking the default branch's file and appending the rows that branch added.
Anything else is left alone: the merge is aborted and a comment says so (once).

Run by optional-workflows/sync-drafts.yml (copied to .github/workflows/) after every push to the default branch; needs git and the gh CLI."""
import os
import subprocess
import sys

BASE = os.environ.get("DEFAULT_BRANCH", "master")  # set by main() from the repository's real default branch


def append_only_merge(base, mine, theirs):
    """3-way merge for an append-only file: the default branch's bytes plus whatever `mine` added after `base`.
    Returns bytes, or None when `mine` changed existing content (then a human has to look)."""
    if not mine.startswith(base):
        return None
    added = mine[len(base):]
    if theirs and not theirs.endswith(b"\n") and added:
        theirs += b"\r\n" if b"\r\n" in theirs else b"\n"
    return theirs + added


def run(*cmd, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, **kw)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.decode(errors='replace').strip()}")
    return r


def git_show(rev_path):
    return run("git", "show", rev_path).stdout


def sync_branch(branch):
    """Returns 'current', 'merged', 'resolved' or 'blocked: <why>'."""
    run("git", "fetch", "-q", "origin", branch)
    run("git", "checkout", "-q", "-B", branch, f"origin/{branch}")
    if run("git", "merge-base", "--is-ancestor", f"origin/{BASE}", "HEAD", check=False).returncode == 0:
        return "current"
    m = run("git", "merge", "--no-edit", f"origin/{BASE}", check=False)
    if m.returncode == 0:
        run("git", "push", "-q", "origin", f"HEAD:{branch}")
        return "merged"
    conflicted = run("git", "diff", "--name-only", "--diff-filter=U").stdout.decode().split()
    if conflicted != ["content.csv"]:
        run("git", "merge", "--abort", check=False)
        return f"blocked: conflicts in {', '.join(conflicted) or 'unknown files'}"
    base = git_show(f"{run('git', 'merge-base', f'origin/{BASE}', 'HEAD').stdout.decode().strip()}:content.csv")
    merged = append_only_merge(base, git_show("HEAD:content.csv"), git_show(f"origin/{BASE}:content.csv"))
    if merged is None:
        run("git", "merge", "--abort", check=False)
        return "blocked: content.csv rows were edited, not just appended"
    open("content.csv", "wb").write(merged)
    run("git", "add", "content.csv")
    run("git", "commit", "-q", "-m", f"Merge {BASE} into the draft branch (content.csv: keep both sets of rows)")
    run("git", "push", "-q", "origin", f"HEAD:{branch}")
    return "resolved"


def main():
    global BASE
    if "DEFAULT_BRANCH" not in os.environ:
        BASE = run("gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name").stdout.decode().strip()
    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    prs = run("gh", "pr", "list", "--label", "content-draft", "--state", "open", "--json", "number,headRefName",
              "--jq", '.[] | "\\(.number) \\(.headRefName)"').stdout.decode().split("\n")
    bad = 0
    for line in filter(None, prs):
        number, branch = line.split(" ", 1)
        try:
            result = sync_branch(branch)
        except RuntimeError as e:
            result = f"blocked: {e}"
        print(f"PR #{number} ({branch}): {result}")
        if result.startswith("blocked"):
            bad += 1
            c = run("gh", "pr", "view", number, "--json", "comments", "--jq",
                    '[.comments[].body | select(startswith("Could not update this branch"))] | length').stdout.decode().strip()
            if c == "0":
                run("gh", "pr", "comment", number, "--body",
                    f"Could not update this branch with {BASE} automatically ({result}). "
                    f"Comment `/regenerate` to redraft it from {BASE}, or resolve the conflict by hand.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
