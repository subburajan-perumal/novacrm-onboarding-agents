"""Append-only audit log: every agent action with timestamp, input, output, decision and rationale."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from rich.console import Console

_console = Console()
_COLORS = {"intake": "cyan", "communication": "magenta", "orchestrator": "yellow", "guardrail": "red"}


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class AuditLog:
    def __init__(self, path: Path, echo: bool = True):
        self.path = path
        self.echo = echo
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(
        self,
        run_id: str,
        agent: str,
        step: str,
        *,
        input: Any = None,
        output: Any = None,
        decision: str = "",
        rationale: str = "",
    ) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": run_id,
            "agent": agent,
            "step": step,
            "input": _jsonable(input),
            "output": _jsonable(output),
            "decision": decision,
            "rationale": rationale,
        }
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.echo:
            color = _COLORS.get(agent, "white")
            tail = f" -> [bold]{decision}[/bold]" if decision else ""
            why = f"  [dim]{rationale}[/dim]" if rationale else ""
            _console.print(f"[{color}]{entry['ts'][11:19]} {agent:>13}[/{color}] {step}{tail}{why}")
        return entry

    def entries(self, run_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if run_id is None or r["run_id"] == run_id]
