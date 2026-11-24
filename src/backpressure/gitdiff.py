"""Thin git wrapper: merge base, changed paths, file contents at a ref."""

from __future__ import annotations

import subprocess

from backpressure.tamper import FileChange


class GitError(RuntimeError):
    pass


def _git(*args: str, cwd: str | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def rev_parse(ref: str, cwd: str | None = None) -> str:
    return _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cwd).strip()


def merge_base(base: str, head: str, cwd: str | None = None) -> str:
    return _git("merge-base", base, head, cwd=cwd).strip()


def show(ref: str, path: str, cwd: str | None = None) -> str | None:
    proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=cwd, capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def changes(base_sha: str, head_sha: str, cwd: str | None = None) -> list[FileChange]:
    """Changed paths between two commits, with renames detected and contents on both sides."""
    fields = _git("diff", "--name-status", "-M", "-z", base_sha, head_sha, cwd=cwd).split("\0")
    out: list[FileChange] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        if status[0] in "RC":
            old, new = fields[i + 1], fields[i + 2]
            i += 3
        else:
            old = new = fields[i + 1]
            i += 2
        if status[0] == "C":
            old = None  # a copy leaves the original in place; compare only the new file
        base_src = show(base_sha, old, cwd) if old and status[0] != "A" else None
        head_src = show(head_sha, new, cwd) if status[0] != "D" else None
        out.append(FileChange(path=new, base_src=base_src, head_src=head_src, old_path=old if old != new else None))
    return out
