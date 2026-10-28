"""Exercise database publish leases against a local bare Git repository."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/data_branch.sh"
GIT = shutil.which("git")
if os.name == "nt" and GIT:
    BASH = Path(GIT).resolve().parents[1] / "bin/bash.exe"
else:
    BASH = Path(shutil.which("bash") or "/nonexistent")


@unittest.skipUnless(GIT and BASH.is_file(), "Git and Bash are required")
class DatabasePublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.git(self.root, "init", "--bare", str(self.remote))
        self.seed = self.new_publisher("seed", "initial")
        self.git(self.seed, "push", "origin", "HEAD:refs/heads/main")

    def git(self, cwd, *args):
        result = subprocess.run([GIT, *args], cwd=cwd, env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def new_publisher(self, name, content):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, "init", "-b", "data")
        self.git(repo, "remote", "add", "origin", str(self.remote))
        (repo / "snapshot").write_text(content)
        self.git(repo, "add", "snapshot")
        self.git(repo, "commit", "-m", content)
        return repo

    def helper(self, repo, command, *, base=None, expect_success=True):
        env = dict(self.env, GITHUB_ENV=str(repo / "github-env"))
        if base is not None:
            env["DATA_BASE_SHA"] = base
        else:
            env.pop("DATA_BASE_SHA", None)
        result = subprocess.run([str(BASH), SCRIPT.as_posix(), command],
                                cwd=repo, env=env, text=True, capture_output=True)
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def fetch(self, repo):
        self.helper(repo, "fetch")
        lines = (repo / "github-env").read_text().splitlines()
        return lines[-1].removeprefix("DATA_BASE_SHA=")

    def remote_sha(self):
        return self.git(self.root, "--git-dir", str(self.remote),
                        "rev-parse", "refs/heads/data")

    def test_normal_snapshot_replacement(self):
        self.git(self.seed, "push", "origin", "data")
        repo = self.new_publisher("writer", "updated")
        base = self.fetch(repo)
        self.helper(repo, "push", base=base)
        self.assertEqual(self.remote_sha(), self.git(repo, "rev-parse", "HEAD"))

    def test_stale_writer_cannot_overwrite_newer_database(self):
        self.git(self.seed, "push", "origin", "data")
        first = self.new_publisher("first", "newer database")
        second = self.new_publisher("second", "stale database")
        base = self.fetch(first)
        self.assertEqual(self.fetch(second), base)
        self.helper(first, "push", base=base)
        # Even a background fetch must not silently change our expected SHA.
        self.git(second, "fetch", "origin", "+refs/heads/data:refs/remotes/origin/data")
        self.helper(second, "push", base=base, expect_success=False)
        self.assertEqual(self.remote_sha(), self.git(first, "rev-parse", "HEAD"))

    def test_two_first_runs_cannot_overwrite_each_other(self):
        first = self.new_publisher("first", "first database")
        second = self.new_publisher("second", "second database")
        self.assertEqual(self.fetch(first), "")
        self.assertEqual(self.fetch(second), "")
        self.helper(first, "push", base="")
        self.helper(second, "push", base="", expect_success=False)
        self.assertEqual(self.remote_sha(), self.git(first, "rev-parse", "HEAD"))

    def test_missing_remote_is_not_treated_as_missing_branch(self):
        repo = self.new_publisher("writer", "database")
        self.git(repo, "remote", "set-url", "origin", str(self.root / "missing.git"))
        self.helper(repo, "fetch", expect_success=False)
        self.assertFalse((repo / "github-env").exists())

    def test_publish_without_snapshot_is_rejected(self):
        self.helper(self.seed, "push", expect_success=False)


if __name__ == "__main__":
    unittest.main()
