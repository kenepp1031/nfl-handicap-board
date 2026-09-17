"""Publish the rendered board to the Streamlit site.

The site (streamlit_app.py on Streamlit Cloud) just embeds
dashboard/dashboard.html from the GitHub repo, and Streamlit Cloud redeploys
on every push -- so publishing is: commit that one file, push. Only the
dashboard is staged here; code changes are committed by hand as usual.

Never raises: a failed publish (offline, no credentials, git missing) must not
fail the weekly run, whose local dashboard is already written by this point.
"""
from __future__ import annotations

import subprocess

from common import APP_DIR

DASHBOARD_REL = "dashboard/dashboard.html"


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=APP_DIR, capture_output=True, text=True, timeout=timeout,
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
        if push.returncode != 0:
            print(f"Publish failed at push: {push.stderr.strip()}")
            return False
        print("Published to the Streamlit site (redeploys in about a minute)")
        return True
    except (OSError, subprocess.TimeoutExpired) as ex:
        print(f"Publish failed: {ex}")
        return False
