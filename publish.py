"""Publish the rendered board to the Streamlit site.

The site (streamlit_app.py on Streamlit Cloud) just embeds
dashboard/dashboard.html from the GitHub repo, and Streamlit Cloud redeploys
on every push -- so publishing is: commit that one file, push. Only the
dashboard is staged here; code changes are committed by hand as usual.

Never raises: a failed publish (offline, no credentials, git missing) must not
fail the weekly run, whose local dashboard is already written by this point.
"""
from __future__ import annotations

import shutil
import subprocess

from common import APP_DIR

DASHBOARD_REL = "dashboard/dashboard.html"

# The scheduled task's PATH is not the desktop's; for two days in September
# every hourly publish died with WinError 2 (git not found) and nobody saw it.
GIT = shutil.which("git") or next(
    (p for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe") if __import__("os").path.exists(p)),
    "git",
)


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [GIT, *args], cwd=APP_DIR, capture_output=True, text=True, timeout=timeout,
    )


def publish_dashboard(season: int, week: int) -> bool:
    """Commit + push dashboard.html. Returns True if the site is up to date
    with the local render afterwards."""
    try:
        if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
            print("Publish skipped: this folder isn't a git repo")
            return False
        _git("add", "--", DASHBOARD_REL)
        if _git("diff", "--cached", "--quiet", "--", DASHBOARD_REL).returncode != 0:
            commit = _git("commit", "-m", f"Publish {season} week {week} board", "--", DASHBOARD_REL)
            if commit.returncode != 0:
                print(f"Publish failed at commit: {(commit.stderr or commit.stdout).strip()}")
                return False
        # Push even when there was nothing new to commit, so a run that
        # committed while offline gets delivered by the next one.
        push = _git("push", "origin", "HEAD", timeout=120)
        if push.returncode != 0 and "rejected" in (push.stderr or ""):
            # The remote moved (a code commit pushed from elsewhere). This
            # process only ever touches dashboard.html, so replay our board
            # commits on top of the remote, keeping our copy of that one file,
            # and push once more. Without this the hourly run failed every
            # hour for a week in September while the site sat stale.
            print("Publish: remote has moved, rebasing the board commits onto it")
            rebase = _git("pull", "--rebase", "-X", "theirs", "origin", "main", timeout=120)
            if rebase.returncode != 0:
                _git("rebase", "--abort")
                print(f"Publish failed at rebase: {(rebase.stderr or rebase.stdout).strip()}")
                return False
            push = _git("push", "origin", "HEAD", timeout=120)
        if push.returncode != 0:
            print(f"Publish failed at push: {push.stderr.strip()}")
            return False
        print("Published to the Streamlit site (redeploys in about a minute)")
        return True
    except (OSError, subprocess.TimeoutExpired) as ex:
        print(f"Publish failed: {ex}")
        return False
