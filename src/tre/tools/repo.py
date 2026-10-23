"""Repository operations: git plumbing, worktree isolation for parallel
developers, repo mapping, and test-command detection. All subprocess-based,
all bounded, no shell interpolation of untrusted strings."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tre",
             ".pytest_cache", ".mypy_cache", "dist", "build", ".idea", ".vscode",
             "wandb", "outputs", "checkpoints", ".ruff_cache", "egg-info"}


def run(root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(
        list(args), cwd=str(root), capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out.strip()


def git(root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    return run(root, "git", *args, timeout=timeout)


def is_git_repo(root: Path) -> bool:
    code, _ = git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0


def current_branch(root: Path) -> str:
    _, out = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out


def status_brief(root: Path) -> str:
    _, out = git(root, "status", "--short", "--branch")
    return out


def diff_stat(root: Path, ref: Optional[str] = None) -> str:
    args = ["diff", "--stat"] + ([ref] if ref else [])
    _, out = git(root, *args)
    return out


def diff_full(root: Path, ref: Optional[str] = None, max_chars: int = 40_000) -> str:
    args = ["diff"] + ([ref] if ref else [])
    _, out = git(root, *args)
    return out[:max_chars]


def log_brief(root: Path, n: int = 10) -> str:
    _, out = git(root, "log", f"-{n}", "--oneline", "--decorate")
    return out


def commit_all(root: Path, message: str) -> tuple[int, str]:
    git(root, "add", "-A")
    return git(root, "commit", "-m", message, "--no-verify")


# -- worktree isolation ------------------------------------------------------

def add_worktree(root: Path, branch: str, base: str = "HEAD") -> Path:
    """Create .tre/worktrees/<branch-slug> on a new branch; parallel
    developers each get an isolated checkout."""
    slug = branch.replace("/", "-")
    path = root / ".tre" / "worktrees" / slug
    path.parent.mkdir(parents=True, exist_ok=True)
    code, out = git(root, "worktree", "add", "-b", branch, str(path), base)
    if code != 0:
        # branch may exist from a previous round: reuse it
        code2, out2 = git(root, "worktree", "add", str(path), branch)
        if code2 != 0:
            raise RuntimeError(f"git worktree add failed: {out} / {out2}")
    return path


def remove_worktree(root: Path, branch: str, *, delete_branch: bool = False) -> None:
    slug = branch.replace("/", "-")
    path = root / ".tre" / "worktrees" / slug
    git(root, "worktree", "remove", "--force", str(path))
    if delete_branch:
        git(root, "branch", "-D", branch)


def merge_branch(root: Path, branch: str, message: str) -> tuple[int, str]:
    return git(root, "merge", "--no-ff", "-m", message, branch)


# -- repo map ----------------------------------------------------------------

def repo_map(root: Path, max_files: int = 400, max_depth: int = 6) -> str:
    """Compact tree of the repository, junk dirs skipped."""
    root = Path(root)
    lines: list[str] = []
    count = 0

    def walk(d: Path, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_files:
            return
        try:
            children = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if count >= max_files:
                lines.append(f"{'  ' * depth}… (truncated at {max_files} entries)")
                return
            if child.name.startswith(".") and child.name not in {".github", ".tre"}:
                continue
            if child.is_dir():
                if child.name in SKIP_DIRS or child.name.endswith(".egg-info"):
                    continue
                lines.append(f"{'  ' * depth}{child.name}/")
                count += 1
                walk(child, depth + 1)
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                suffix = f" ({size // 1024}k)" if size > 4096 else ""
                lines.append(f"{'  ' * depth}{child.name}{suffix}")
                count += 1

    walk(root, 0)
    return "\n".join(lines) or "(empty repository)"


def key_files_excerpt(root: Path, max_chars: int = 6000) -> str:
    """Excerpts of the files that define a repo's conventions."""
    names = ["README.md", "readme.md", "pyproject.toml", "setup.py", "package.json",
             "Makefile", "requirements.txt", "environment.yml", "CONTRIBUTING.md"]
    parts: list[str] = []
    budget = max_chars
    for name in names:
        path = root / name
        if path.exists() and budget > 200:
            text = path.read_text(errors="replace")[: min(2500, budget)]
            parts.append(f"--- {name} ---\n{text}")
            budget -= len(text)
    return "\n\n".join(parts) or "(no conventional key files found)"


def detect_test_command(root: Path) -> str:
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() \
            or (root / "tests").is_dir():
        return "pytest -q -x"
    if (root / "package.json").exists():
        return "npm test --silent"
    if (root / "Makefile").exists():
        text = (root / "Makefile").read_text(errors="replace")
        if "\ntest:" in text or text.startswith("test:"):
            return "make test"
    return "pytest -q -x"


def run_tests(root: Path, command: str, timeout: int = 900) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(root), capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"test command timed out after {timeout}s: {command}"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    out = out.strip()
    if len(out) > 20_000:
        out = out[:8_000] + "\n… (middle truncated) …\n" + out[-10_000:]
    return proc.returncode == 0, out
