"""Shallow clone at HEAD (spec §3). Public https repos, local git paths, or plain local folders."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

from codecast.errors import IngestError
from codecast.ingest.repomap import IGNORED_DIRS, build_repomap
from codecast.models import RepoMap


def normalize_repo_url(url: str) -> str:
    u = url.strip()
    if not u.startswith("https://"):
        raise IngestError(f"only public https:// repository URLs are supported (got {u!r})")
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u.rstrip("/")


def repo_name(url_or_path: str) -> str:
    name = url_or_path.strip().rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}


def _git(args: list[str], *, timeout: float, cwd: Path | None = None) -> str:
    try:
        res = subprocess.run(["git", *args], cwd=cwd, env=_env(), timeout=timeout, check=True,
                             capture_output=True, text=True)
    except subprocess.TimeoutExpired as e:
        raise IngestError(f"git {args[0]} timed out after {timeout:.0f}s") from e
    except subprocess.CalledProcessError as e:
        raise IngestError(f"git {args[0]} failed: {(e.stderr or '').strip()[-500:]}") from e
    except FileNotFoundError as e:
        raise IngestError("git is not installed") from e
    return (res.stdout or "").strip()


def _local_origin(path: Path) -> str | None:
    try:
        url = _git(["remote", "get-url", "origin"], timeout=10, cwd=path)
        return normalize_repo_url(url)
    except IngestError:
        return None


def _clear_readonly(func, path, exc) -> None:
    """rmtree onexc hook: git marks pack and object files read-only, which Windows refuses to delete."""
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=_clear_readonly)


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            p = Path(dirpath) / f
            if not p.is_symlink():
                total += p.stat().st_size
    return total


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in sorted(filenames):
            p = Path(dirpath) / f
            h.update(p.relative_to(root).as_posix().encode() + b"\x00")
            if not p.is_symlink():
                h.update(p.read_bytes())
    return "local-" + h.hexdigest()[:12]


def _copy_folder(src: Path, dest: Path, *, max_bytes: int, max_file_bytes: int) -> RepoMap:
    """A local folder that is not a git repo: snapshot its current contents (no HEAD to clone)."""
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
    size = _tree_bytes(dest)
    if size > max_bytes:
        raise IngestError(f"folder is {size} bytes, which exceeds the {max_bytes}-byte limit")
    return build_repomap(dest, src.resolve().as_posix(), _tree_digest(dest), max_file_bytes=max_file_bytes)


def ingest(repo: str, dest: Path, *, timeout_s: float = 120, max_bytes: int = 1_000_000_000,
           max_file_bytes: int = 1_000_000) -> RepoMap:
    dest = Path(dest)
    local = Path(repo.strip()).expanduser()
    if local.is_dir() and not (local / ".git").exists():
        if dest.exists():
            remove_tree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return _copy_folder(local, dest, max_bytes=max_bytes, max_file_bytes=max_file_bytes)
    if local.exists():
        src = f"file://{local.resolve()}"
        repo_url = _local_origin(local) or local.resolve().as_posix()
    else:
        repo_url = normalize_repo_url(repo)
        src = repo_url
    if dest.exists():
        remove_tree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--depth", "1", "--no-recurse-submodules", "--quiet", src, str(dest)], timeout=timeout_s)
    size = _tree_bytes(dest)
    if size > max_bytes:
        raise IngestError(f"checkout is {size} bytes, which exceeds the {max_bytes}-byte limit")
    sha = _git(["rev-parse", "HEAD"], timeout=30, cwd=dest)
    return build_repomap(dest, repo_url, sha, max_file_bytes=max_file_bytes)
