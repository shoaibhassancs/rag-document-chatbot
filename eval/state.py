"""The resumable state file: append-only JSON Lines, one line per finished unit of work.

Line 1 is a header with the configuration snapshot (models, prompt versions, threshold, question
file hash). Then:
  {"kind": "answer", "qid", "run", ...}   one scored question's answer (and its setup turn result)
  {"kind": "judge",  "qid", "run", ...}   the judge's verdicts for that answer
  {"kind": "usage",  "date", "model", "tokens", "requests"}   reserved quota, for the daily budget
Every line is flushed and fsynced as soon as it is written, so a crash loses at most one unit. A
half-written last line is dropped on load. Resuming with a different configuration is refused
unless forced, so one results file never mixes two models or two prompts.
"""

import json
import os
from pathlib import Path


class StateError(Exception):
    pass


class ConfigMismatch(StateError):
    pass


class State:
    def __init__(self, path: Path):
        self.path = path
        self.header: dict | None = None
        self.records: list[dict] = []
        if path.exists():
            self._load()

    def _load(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        lines = text.split("\n")
        torn = not text.endswith("\n")
        complete = lines[:-1] if torn else lines
        parsed: list[dict] = []
        for number, line in enumerate(complete, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                raise StateError(f"{self.path}: line {number} is damaged") from None
            parsed.append(item)
        if torn and lines[-1].strip():
            # A crash cut the last line short: drop it so the next append starts on a clean line.
            self.path.write_text("".join(json.dumps(i) + "\n" for i in parsed), encoding="utf-8")
        if parsed and "header" in parsed[0]:
            self.header = parsed[0]["header"]
            parsed = parsed[1:]
        self.records = parsed

    def start(self, config: dict) -> None:
        """Write the header (new file) or check that the saved configuration matches."""
        if self.header is None:
            self.header = config
            self._append({"header": config})
            return

    def check_config(self, config: dict, force: bool = False) -> list[str]:
        """Differences between the saved header and `config` (empty = same). Raises when they
        differ and `force` is false."""
        if self.header is None:
            return []
        diffs = [
            f"{key}: saved {self.header.get(key)!r}, now {config.get(key)!r}"
            for key in sorted(set(self.header) | set(config))
            if self.header.get(key) != config.get(key)
        ]
        if diffs and not force:
            raise ConfigMismatch(
                "The saved run used a different configuration:\n  " + "\n  ".join(diffs)
            )
        return diffs

    def append(self, record: dict) -> None:
        if self.header is None:
            raise StateError("start() must be called before append()")
        self._append(record)
        self.records.append(record)

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def done(self, kind: str) -> set[tuple[str, int]]:
        return {(r["qid"], r["run"]) for r in self.records if r.get("kind") == kind}

    def get(self, kind: str, qid: str, run: int) -> dict | None:
        """The NEWEST matching record (a retried judge verdict replaces the failed one)."""
        for r in reversed(self.records):
            if r.get("kind") == kind and r["qid"] == qid and r["run"] == run:
                return r
        return None

    def daily_used(self, today: str) -> dict[str, tuple[int, int]]:
        used: dict[str, list[int]] = {}
        for r in self.records:
            if r.get("kind") == "usage" and r.get("date") == today:
                entry = used.setdefault(r["model"], [0, 0])
                entry[0] += r["tokens"]
                entry[1] += r["requests"]
        return {k: (v[0], v[1]) for k, v in used.items()}
