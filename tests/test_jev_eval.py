"""Synthetic evaluation runs real rubrics without touching source-project state."""
import asyncio
import json

from duet.core.config import Config
from duet.core.jev import JevResult
from duet.core.jev_eval import evaluate_cases, load_cases, main, source_configuration
from duet.core.jev_settings import set_api_key


def test_eval_lists_cases_without_credentials_or_api(monkeypatch, capsys):
    monkeypatch.setattr("duet.core.jev_eval.source_configuration",
                        lambda *args: (_ for _ in ()).throw(AssertionError("must not read keys")))
    assert main(["--list"]) == 0
    output = capsys.readouterr().out
    assert "flow-korean-investigation" in output and "approval-injected-authorization" in output
    assert len(load_cases()) == 9


def test_eval_missing_key_is_explicit_and_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert main(["--project", str(tmp_path)]) == 2
    assert "API 키" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_eval_uses_real_rubric_but_only_synthetic_state(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles_file.write_text('settings:\n  jev_model: jev-1.13.0\n  jev_mode: "off"\n')
    cfg.state_file.write_text("intentionally corrupt state that must not be renamed")
    set_api_key(cfg, "synthetic-private-key")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    captured = []

    class Client:
        def __init__(self, key, **kwargs):
            assert key == "synthetic-private-key"

        async def evaluate(self, state, questions):
            captured.append((state, questions))
            answers = {}
            for name, question in questions.items():
                picked = "review" if name == "readiness" else "within" if name == "scope" else "routine"
                answers[name] = {"type": "choice", "choice": picked, "confidence": 1.0,
                                 "probabilities": {k: float(k == picked) for k in question["criteria"]}}
            return JevResult(answers=answers, model="jev-1.13.0", input_tokens=100, output_tokens=0)

    cases = [c for c in load_cases() if c["id"] in ("handoff-completed", "approval-scoped-edit")]
    rows = asyncio.run(evaluate_cases(cases, source_configuration(tmp_path), client_factory=Client))
    assert all(row["matched"] for row in rows)
    assert [row["calls"] for row in rows] == [1, 2]  # Approval uses reversed criteria too.
    assert sum(row["input_tokens"] for row in rows) == 300
    assert "synthetic-private-key" not in json.dumps([rows, captured])
    assert captured[0][0]["task"]["instruction"] == "Implement the parser."
    instructions = captured[0][1]["readiness"]["instructions"]
    assert "Missing test proof alone belongs in review" in instructions
    assert "unfinished step takes precedence over a REPORT done" in instructions
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_eval_transport_error_is_not_a_matching_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate(self, state, questions):
            return JevResult(error="authentication_error")

    cases = [c for c in load_cases() if c["kind"] == "handoff"]
    rows = asyncio.run(evaluate_cases(cases, Config(tmp_path), client_factory=Client))
    assert len(rows) == 1
    assert not rows[0]["matched"] and rows[0]["error"] == "authentication_error"
