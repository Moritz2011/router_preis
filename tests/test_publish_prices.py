"""Exercise competing pushes against real, local Git repositories (no network)."""

import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import crawler
import publish_prices


def measurement(day, hour="14:00:00", price=169.99):
    return {
        "date": day,
        "timestamp": f"{day}T{hour}+02:00",
        "price": price,
        "compare_at_price": None,
        "available": True,
    }


class PublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-price-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.worker = self.root / "worker"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.seed))
        self.configure(self.seed)
        self.history = crawler.load_history(self.seed / "price_data.json")
        self.history["observations"] = [measurement("2026-09-16")]
        self.write_history(self.seed, self.history)
        self.commit_and_push(self.seed)
        self.git(self.root, "clone", str(self.remote), str(self.worker))
        self.configure(self.worker)
        pending = deepcopy(self.history)
        pending["observations"].append(measurement("2026-09-18"))
        self.write_history(self.worker, pending)

    def git(self, repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def configure(self, repo):
        self.git(repo, "config", "user.name", "Test Bot")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")

    def write_history(self, repo, history):
        crawler.save_history(history, repo / "price_data.json")
        crawler.generate_dashboard(history, repo / "index.html")

    def commit_and_push(self, repo):
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "Test update")
        self.git(repo, "push", "origin", "main")

    def remote_history(self):
        return json.loads(self.git(self.remote, "show", "main:price_data.json"))

    def competing_measurement(self, observation):
        self.history["observations"].append(observation)
        self.write_history(self.seed, self.history)
        (self.seed / "README.md").write_text("Concurrent edit\n", encoding="utf-8")
        self.commit_and_push(self.seed)

    def test_stale_checkout_preserves_remote_days_and_other_files(self):
        self.competing_measurement(measurement("2026-09-17", price=180))
        publish_prices.publish(self.worker)
        self.assertEqual(
            [row["date"] for row in self.remote_history()["observations"]],
            ["2026-09-16", "2026-09-17", "2026-09-18"],
        )
        self.assertEqual(self.git(self.remote, "show", "main:README.md"), "Concurrent edit")

    def test_concurrent_push_retries_and_rebuilds_dashboard(self):
        real_git = publish_prices.git
        pushes = []

        def race(repo, *args, **kwargs):
            if args[0] == "push":
                pushes.append(1)
                if len(pushes) == 1:
                    self.competing_measurement(measurement("2026-09-17", price=180))
            return real_git(repo, *args, **kwargs)

        with patch("publish_prices.git", side_effect=race):
            publish_prices.publish(self.worker)
        self.assertEqual(len(pushes), 2)
        self.assertEqual(len(self.remote_history()["observations"]), 3)
        dashboard = self.git(self.remote, "show", "main:index.html")
        self.assertIn('"2026-09-17"', dashboard)
        self.assertIn("180,00 €", dashboard)
        self.assertEqual(self.git(self.remote, "show", "main:README.md"), "Concurrent edit")
        # Temporary worktrees are cleaned up; local pending measurements remain intact.
        self.assertEqual(self.git(self.worker, "worktree", "list").count(str(self.worker)), 1)
        self.assertNotIn("router-price-publish-", self.git(self.worker, "worktree", "list"))
        self.assertIn("2026-09-18", (self.worker / "price_data.json").read_text())

    def test_newer_same_day_measurement_is_not_overwritten(self):
        self.competing_measurement(measurement("2026-09-18", "15:00:00", price=160))
        before = self.git(self.remote, "rev-parse", "main")
        publish_prices.publish(self.worker)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)
        self.assertEqual(self.remote_history()["observations"][-1]["price"], 160)

    def test_non_race_push_error_fails_without_retries(self):
        real_git = publish_prices.git
        pushes = []

        def deny(repo, *args, **kwargs):
            if args[0] == "push":
                pushes.append(1)
                return subprocess.CompletedProcess(args, 1, "", "Permission denied")
            return real_git(repo, *args, **kwargs)

        with patch("publish_prices.git", side_effect=deny):
            with self.assertRaisesRegex(RuntimeError, "Permission denied"):
                publish_prices.publish(self.worker)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(len(self.remote_history()["observations"]), 1)

    def test_repeated_races_stop_after_three_attempts(self):
        real_git = publish_prices.git
        pushes = []

        def race(repo, *args, **kwargs):
            if args[0] == "push":
                pushes.append(1)
                (self.seed / "README.md").write_text(f"Edit {len(pushes)}\n")
                self.commit_and_push(self.seed)
            return real_git(repo, *args, **kwargs)

        with patch("publish_prices.git", side_effect=race):
            with self.assertRaisesRegex(RuntimeError, "allen 3 Versuchen"):
                publish_prices.publish(self.worker)
        self.assertEqual(len(pushes), 3)
        self.assertEqual(len(self.remote_history()["observations"]), 1)
        self.assertEqual(self.git(self.remote, "show", "main:README.md"), "Edit 3")


if __name__ == "__main__":
    unittest.main()
