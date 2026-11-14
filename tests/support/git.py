"""Throwaway Git repositories for diff and worktree tests."""
from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout


def init_git_repo(root: Path) -> None:
    run_git(root, "init")
    run_git(root, "config", "user.email", "unit@example.com")
    run_git(root, "config", "user.name", "Unit Test")


def commit_all(root: Path, message: str = "init") -> None:
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", message)
