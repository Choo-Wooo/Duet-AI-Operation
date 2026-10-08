"""Optional, paid API evaluation of the real JEV rubrics using synthetic cases.

No agent adapters, commands, or project state writes are performed. Run from
Duet's parent directory: python -m duet.core.jev_eval --project /path/to/project
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

import yaml

from .config import Config
from .events import EventBus
from .jev import JevClient
from .jev_control import JevControl, RUBRIC_VERSION
from .jev_settings import JEV_DEFAULTS, get_api_key, validate_settings
from .policy import ApprovalRequest, Policy

CASES_PATH = Path(__file__).with_name("jev_cases.json")


def load_cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def source_configuration(project: Path, model: str | None = None):
    """Read only JEV configuration. Config.load can repair corrupt user state."""
    cfg = Config(project.resolve())
    if cfg.roles_file.exists():
        raw = yaml.safe_load(cfg.roles_file.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict) or not isinstance(raw.get("settings", {}), dict):
            raise ValueError("invalid settings")
        cfg.settings.update({k: v for k, v in raw.get("settings", {}).items() if k in JEV_DEFAULTS})
    if model:
        cfg.settings["jev_model"] = model
    validate_settings(cfg.settings)
    return cfg


async def evaluate_cases(cases, source: Config, *, client_factory=JevClient):
    """Return outcomes and API telemetry; actual execution actions are never invoked."""
    key = get_api_key(source)
    if not key:
        raise ValueError("missing_api_key")
    rows = []
    with tempfile.TemporaryDirectory(prefix="duet-jev-eval-") as directory:
        cfg = Config(Path(directory))
        cfg.settings.update({k: source.settings[k] for k in JEV_DEFAULTS})
        cfg.settings.update(jev_mode="on", jev_choices=True, jev_approvals=True, jev_flow=True)
        ready = asyncio.Event()
        ready.set()
        orch = SimpleNamespace(cfg=cfg, current_task={}, work=SimpleNamespace(items={}),
                               run_turns=0, full_auto=False, stop_requested=False,
                               not_paused=ready, inbox=asyncio.Queue(), activity={},
                               budget_usd=None, max_hours=None, cost_usd=0.0, tokens=0,
                               started=time.time(), bus=EventBus(), notice=lambda *args: None,
                               emit_status=lambda: None)
        for case in cases:
            control = JevControl(orch)
            # Credentials never enter synthetic state or project files.
            control.client_factory = lambda ignored, **kwargs: client_factory(key, **kwargs)
            role = case.get("role", "implementer")
            cfg.settings["design_questions"] = bool(case.get("design_questions", False))
            cfg.state.task = {"id": case["id"], "instruction": case["instruction"],
                              "phase": case.get("phase", "implement"),
                              "waiting": False, "agreed_files": case.get("agreed_files", []), "test_command": ""}
            for report in case.get("reports", []):
                control.record_turn(role, case.get("turn_kind", "implement"), report)
            decisions = []
            orch.bus = EventBus()
            orch.bus.subscribe(lambda ev: decisions.append(dict(ev.data)) if ev.kind == "jev_decision" else None)
            if case["kind"] == "flow":
                result = await control.flow("implementer", case.get("signal"))
                actual = result or "refer"
            elif case["kind"] == "handoff":
                result = await control.handoff("implementer", case["report"], cfg.state.task)
                actual = result or "review"  # Existing handoff fallback always reaches review.
            elif case["kind"] == "approval":
                request = ApprovalRequest(role="implementer", **case["request"])
                policy = Policy(cfg.project, cfg.policy, cfg.main)
                policy.task = cfg.state.task
                result = await control.approval(request, policy, "Synthetic approval review")
                actual = "refer" if result is None else "allow" if result.allow else "deny"
            elif case["kind"] == "route":
                result = await control.route_turn(
                    role, case["report"], cfg.state.task, kind=case.get("turn_kind", "implement"),
                    directives=[tuple(d) for d in case.get("directives", [])],
                    issue=case.get("issue", ""), allowed=tuple(case["allowed"]))
                # Abstention is visible as a miss, not silently relabeled as the expected action.
                actual = result or "refer"
            else:
                raise ValueError("unknown case kind")
            error = next((d["error"] for d in decisions if d.get("error")), None)
            rows.append({"id": case["id"], "kind": case["kind"], "expected": case["expected"],
                         "actual": actual, "matched": not error and actual in case["expected"],
                         "fallback": result is None, "calls": control.calls,
                         "input_tokens": control.input_tokens, "output_tokens": control.output_tokens,
                         "estimated_cost_usd": control.cost_usd, "error": error,
                         "decisions": decisions})
            if error:
                break  # A transport failure is not model-quality evidence; avoid repeated charges.
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate Duet's JEV questions using synthetic cases (paid API calls).")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Read the saved JEV key/model from this project")
    parser.add_argument("--model", help="Optional JEV model ID override; does not save settings")
    parser.add_argument("--case", action="append", default=[], help="Only evaluate this case ID (repeatable)")
    parser.add_argument("--list", action="store_true", help="List synthetic cases without reading keys or calling the API")
    parser.add_argument("--json", action="store_true", help="Print machine-readable outcomes and distributions")
    args = parser.parse_args(argv)
    cases = load_cases()
    if args.case:
        if set(args.case) - {c["id"] for c in cases}:
            parser.error("unknown --case; use --list")
        cases = [c for c in cases if c["id"] in args.case]
    if args.list:
        for case in cases:
            print(f"{case['id']}: {case['kind']} → {'|'.join(case['expected'])}")
        return 0
    try:
        source = source_configuration(args.project, args.model)
        rows = asyncio.run(evaluate_cases(cases, source))
    except (OSError, ValueError, yaml.YAMLError):
        print("JEV 평가를 시작할 수 없습니다. 프로젝트 설정과 저장된 API 키 또는 TYPESAFE_API_KEY를 확인하세요.")
        return 2
    summary = {"rubric_version": RUBRIC_VERSION, "requested_model": source.settings["jev_model"],
               "completed": len(rows), "requested": len(cases), "matched": sum(r["matched"] for r in rows),
               "calls": sum(r["calls"] for r in rows), "input_tokens": sum(r["input_tokens"] for r in rows),
               "output_tokens": sum(r["output_tokens"] for r in rows),
               "estimated_cost_usd": sum(r["estimated_cost_usd"] for r in rows), "cases": rows}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"JEV {summary['requested_model']} · {RUBRIC_VERSION} · synthetic cases (no commands executed)")
        for row in rows:
            status = "ERROR" if row["error"] else "OK" if row["matched"] else "MISMATCH"
            print(f"{status:8} {row['id']}: expected={'|'.join(row['expected'])} actual={row['actual']}"
                  + (" (fallback)" if row["fallback"] else ""))
            for decision in row["decisions"]:
                judgments = ", ".join(f"{name}={a.get('choice')} confidence={a.get('confidence', 0):.3f}"
                                      for name, a in decision["answers"].items())
                print(f"         {decision['purpose']} · {decision['model']} · {judgments or decision['error']}")
        print(f"Matched {summary['matched']}/{len(cases)} · API {summary['calls']} calls · "
              f"input {summary['input_tokens']} / output {summary['output_tokens']} tokens · "
              f"estimated ${summary['estimated_cost_usd']:.6f}")
    if any(r["error"] for r in rows):
        return 2
    return 0 if summary["matched"] == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
