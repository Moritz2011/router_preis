#!/usr/bin/env python3
"""Publish one observation on top of the latest main, retrying concurrent pushes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import crawler


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check, capture_output=True, text=True, timeout=60,
    )


def merge_observation(
    remote: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    """Keep the remote history; for the measured day the newest timestamp wins."""
    observations = {item["date"]: item for item in remote["observations"]}
    previous = observations.get(observation["date"])
    if previous is None or datetime.fromisoformat(observation["timestamp"]) > datetime.fromisoformat(previous["timestamp"]):
        observations[observation["date"]] = observation
    return {**remote, "observations": [observations[day] for day in sorted(observations)]}


def fetch_main(repo: Path) -> str:
    git(repo, "fetch", "--no-tags", "origin", "refs/heads/main")
    return git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()


def publish(repo: Path = crawler.ROOT, max_attempts: int = 3) -> None:
    local = crawler.load_history(repo / "price_data.json")
    if not local["observations"]:
        raise RuntimeError("Keine Messung zum Speichern vorhanden.")
    observation = max(local["observations"], key=lambda item: item["date"])
    base = fetch_main(repo)

    for attempt in range(1, max_attempts + 1):
        print(f"Speichern: Versuch {attempt}/{max_attempts}", flush=True)
        # Jeder Versuch erhält einen separaten Checkout des neuesten Remote-Stands.
        # Dadurch müssen generierte JSON-/HTML-Dateien nicht textuell rebasiert werden.
        with tempfile.TemporaryDirectory(prefix="router-price-publish-") as directory:
            worktree = Path(directory) / "checkout"
            git(repo, "worktree", "add", "--detach", str(worktree), base)
            try:
                remote = crawler.load_history(worktree / "price_data.json")
                if remote["product"]["sku"] != local["product"]["sku"]:
                    raise RuntimeError("Lokale und entfernte Produkt-SKU unterscheiden sich.")
                merged = merge_observation(remote, observation)
                crawler.save_history(merged, worktree / "price_data.json")
                crawler.generate_dashboard(merged, worktree / "index.html")
                git(worktree, "add", "price_data.json", "index.html")
                diff = git(worktree, "diff", "--cached", "--quiet", check=False)
                if diff.returncode == 0:
                    print("Diese oder eine neuere Messung ist bereits gespeichert.")
                    return
                if diff.returncode != 1:
                    diff.check_returncode()
                git(worktree, "commit", "-m", f"Preisupdate {observation['date']}")
                pushed = git(worktree, "push", "origin", "HEAD:refs/heads/main", check=False)
                if pushed.returncode == 0:
                    print("Preisverlauf und Dashboard erfolgreich gespeichert.")
                    return
            finally:
                # Ausschließlich den gerade erstellten temporären Checkout aufräumen.
                git(repo, "worktree", "remove", "--force", str(worktree))

        latest = fetch_main(repo)
        if latest == base:
            # Beispielsweise fehlende Schreibrechte: kein konkurrierender Commit.
            raise RuntimeError(f"Push fehlgeschlagen:\n{pushed.stderr.strip()}")
        base = latest
        print("main wurde inzwischen aktualisiert; führe Messung erneut zusammen.")

    raise RuntimeError(f"main hat sich bei allen {max_attempts} Versuchen geändert.")


if __name__ == "__main__":
    try:
        publish()
    except subprocess.CalledProcessError as exc:
        print(f"FEHLER: {exc.stderr or exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
