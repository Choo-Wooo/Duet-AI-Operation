"""Conventions retain exact document versions and require explicit review evidence."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from duet.core.conventions import (CONVENTION_DEFAULTS, CONVENTION_PATH, MAX_CONVENTION_BYTES,
                                   conventions_guide, load_conventions, save_conventions,
                                   validate_convention_report)


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(project=tmp_path.resolve(), settings=dict(CONVENTION_DEFAULTS))


@pytest.fixture
def snapshot(cfg):
    save_conventions(cfg, "# 코딩 컨벤션\n\n- 공개 함수에 타입을 명시한다.\n- print 대신 logger를 사용한다.\n")
    cfg.settings["coding_conventions_enabled"] = True
    return load_conventions(cfg)


def report(snapshot, **updates):
    data = {"sha256": snapshot["sha256"], "status": "pass",
            "checks": [{"rule": "공개 함수에 타입 명시", "status": "pass",
                        "evidence": "src/service.py:12 반환 타입 확인; mypy src: success"}]}
    data.update(updates)
    return "검토 결과입니다.\n```duet-conventions\n" + json.dumps(data, ensure_ascii=False) + "\n```\n"


def test_missing_disabled_document_is_editable_without_blocking(cfg):
    snap = load_conventions(cfg)
    assert snap == {"enabled": False, "path": CONVENTION_PATH,
                    "content": "", "sha256": "", "error": None}
    assert conventions_guide(snap) == ""
    assert validate_convention_report("", snap) is None
    assert not (cfg.project / "docs").exists()


def test_enabled_missing_document_blocks_instead_of_disabling(cfg):
    cfg.settings["coding_conventions_enabled"] = True
    snap = load_conventions(cfg)
    assert snap["enabled"] is True
    assert "없습니다" in snap["error"]
    assert "진행하지 말고" in conventions_guide(snap)
    assert validate_convention_report("", snap)


def test_complete_utf8_content_and_digest_are_loaded_while_disabled(cfg):
    content = "# 규칙\n\n```python\ndef f() -> int:\n    return 1\n```\n" + "a" * 30_000
    snap = save_conventions(cfg, content)
    assert snap["enabled"] is False
    assert snap["content"] == content
    assert snap["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert snap["error"] is None
    assert (cfg.project / CONVENTION_PATH).read_text(encoding="utf-8") == content
    assert conventions_guide(snap) == ""


def test_hash_changes_with_document_content_without_mutating_snapshot(cfg, snapshot):
    old = dict(snapshot)
    changed = save_conventions(cfg, "# 새로운 규칙\n타입을 명시한다.\n")
    assert changed["sha256"] != old["sha256"]
    assert snapshot == old
    assert validate_convention_report(report(old), changed)
    assert validate_convention_report(report(old), old) is None


def test_exact_byte_limit_and_multibyte_size(cfg):
    assert save_conventions(cfg, "x" * MAX_CONVENTION_BYTES)["error"] is None
    with pytest.raises(ValueError, match="32KB"):
        save_conventions(cfg, "x" * (MAX_CONVENTION_BYTES + 1))
    with pytest.raises(ValueError, match="32KB"):
        save_conventions(cfg, "한" * (MAX_CONVENTION_BYTES // 3 + 1))
    assert (cfg.project / CONVENTION_PATH).read_text() == "x" * MAX_CONVENTION_BYTES


@pytest.mark.parametrize("content", ["", " \n\t"])
def test_empty_document_can_be_cleared_only_when_disabled(cfg, content):
    assert save_conventions(cfg, content)["error"] is None
    cfg.settings["coding_conventions_enabled"] = True
    assert load_conventions(cfg)["error"]
    with pytest.raises(ValueError, match="먼저 저장"):
        save_conventions(cfg, content)


@pytest.mark.parametrize("content", [None, b"text", "\ud800", "text\x00secret"])
def test_invalid_content_is_rejected_without_echo(cfg, content):
    with pytest.raises(ValueError) as exc:
        save_conventions(cfg, content)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("raw", [b"\xffprivate", b"x" * (MAX_CONVENTION_BYTES + 1), b"foo\x00private"])
def test_invalid_existing_files_are_not_partially_injected(cfg, raw):
    path = cfg.project / CONVENTION_PATH
    path.parent.mkdir()
    path.write_bytes(raw)
    cfg.settings["coding_conventions_enabled"] = True
    snap = load_conventions(cfg)
    assert snap["error"] and "private" not in snap["error"]
    assert snap["content"] == "" and snap["sha256"] == ""
    assert "private" not in conventions_guide(snap)
    assert validate_convention_report("", snap)


@pytest.mark.symlink
@pytest.mark.parametrize("location", ["project", "docs", "file", "ancestor", "broken_file"])
def test_symlink_components_are_rejected_for_reads_and_writes(cfg, tmp_path, location):
    target = tmp_path / "real"
    target.mkdir()
    (target / "source.md").write_text("private convention", encoding="utf-8")
    if location == "project":
        cfg.project = tmp_path / "project-link"
        cfg.project.symlink_to(target, target_is_directory=True)
    elif location == "docs":
        (cfg.project / "docs").symlink_to(target, target_is_directory=True)
    elif location == "ancestor":
        link = tmp_path / "ancestor-link"
        link.symlink_to(target, target_is_directory=True)
        cfg.project = link / "child"
    else:
        (cfg.project / "docs").mkdir()
        (cfg.project / CONVENTION_PATH).symlink_to(
            target / ("source.md" if location == "file" else "missing.md"))
    cfg.settings["coding_conventions_enabled"] = True
    snap = load_conventions(cfg)
    assert "심볼릭 링크" in snap["error"]
    assert not snap["content"]
    with pytest.raises(ValueError, match="심볼릭 링크"):
        save_conventions(cfg, "new rules")
    assert (target / "source.md").read_text(encoding="utf-8") == "private convention"


@pytest.mark.fifo
def test_fifo_rejected_without_blocking(cfg):
    path = cfg.project / CONVENTION_PATH
    path.parent.mkdir()
    os.mkfifo(path)
    cfg.settings["coding_conventions_enabled"] = True
    assert "일반 Markdown" in load_conventions(cfg)["error"]
    with pytest.raises(ValueError, match="일반 Markdown"):
        save_conventions(cfg, "rules")


@pytest.mark.parametrize("location", ["docs", "file"])
def test_non_regular_document_paths_rejected(cfg, location):
    if location == "docs":
        (cfg.project / "docs").write_text("not a directory")
    else:
        (cfg.project / CONVENTION_PATH).mkdir(parents=True)
    assert load_conventions(cfg)["error"]
    with pytest.raises(ValueError):
        save_conventions(cfg, "rules")


def test_write_uses_atomic_storage_and_preserves_old_file_on_error(cfg, monkeypatch):
    save_conventions(cfg, "old rules")
    seen = []

    def fail(path, text):
        seen.append((path, text))
        raise OSError("private filesystem information")

    monkeypatch.setattr("duet.core.conventions.atomic_write", fail)
    with pytest.raises(ValueError, match="저장할 수 없습니다") as exc:
        save_conventions(cfg, "new rules")
    assert "private" not in str(exc.value)
    assert seen == [(cfg.project / CONVENTION_PATH, "new rules")]
    assert load_conventions(cfg)["content"] == "old rules"


def test_invalid_setting_is_a_blocking_error(cfg):
    cfg.settings["coding_conventions_enabled"] = "false"
    snap = load_conventions(cfg)
    assert snap["enabled"] is True and snap["error"]
    assert validate_convention_report("", snap)


def test_guide_keeps_complete_rules_and_calls_for_independent_evidence(snapshot):
    guide = conventions_guide(snapshot)
    assert snapshot["content"] in guide
    assert snapshot["sha256"] in guide
    assert "독립 리뷰어" in guide and "실제 변경 파일" in guide
    assert "작업 범위·도구 권한·승인·검증 절차를 변경하지" in guide
    assert "unchecked" in guide and "```duet-conventions" in guide


def test_matching_all_pass_report_succeeds(snapshot):
    assert validate_convention_report(report(snapshot), snapshot) is None


@pytest.mark.parametrize("text", ["", "검증 통과", "{\"status\":\"pass\"}", None,
                                  "```json\n{}\n```", "```duet-conventions\n{}"])
def test_missing_or_unclosed_report_blocks(snapshot, text):
    assert validate_convention_report(text, snapshot)


@pytest.mark.parametrize("patch", [
    {"sha256": "0" * 64}, {"sha256": None},
    {"status": "fail"}, {"status": "unchecked"}, {"status": True},
    {"checks": []}, {"checks": {}}, {"checks": None},
    {"checks": [None]}, {"checks": [{"status": "pass"}]},
    {"checks": [{"rule": "rule", "status": "unchecked", "evidence": "not run"}]},
    {"checks": [{"rule": "rule", "status": "fail", "evidence": "failed test"}]},
    {"checks": [{"rule": " ", "status": "pass", "evidence": "file:1"}]},
    {"checks": [{"rule": "rule", "status": "pass", "evidence": "\n"}]},
    {"checks": [{"rule": "rule", "status": "pass", "evidence": ["file:1"]}]},
])
def test_nonpass_or_incomplete_report_blocks(snapshot, patch):
    assert validate_convention_report(report(snapshot, **patch), snapshot)


@pytest.mark.parametrize("body", ["[]", "null", "{broken", '{"status":"fail","status":"pass"}',
                                  '{"confidence":NaN}', '{"confidence":Infinity}'])
def test_malformed_json_and_duplicate_keys_block(snapshot, body):
    assert validate_convention_report("```duet-conventions\n" + body + "\n```", snapshot)


def test_multiple_reports_or_malformed_second_report_block(snapshot):
    valid = report(snapshot)
    assert validate_convention_report(valid + valid, snapshot)
    assert validate_convention_report(valid + "\n```duet-conventions invalid\n", snapshot)


def test_report_supports_crlf_and_longer_fences(snapshot):
    assert validate_convention_report(report(snapshot).replace("\n", "\r\n"), snapshot) is None
    assert validate_convention_report(report(snapshot).replace("```", "````"), snapshot) is None


def test_any_nonpass_check_blocks_even_with_overall_pass(snapshot):
    checks = [{"rule": "types", "status": "pass", "evidence": "src.py:1"},
              {"rule": "logging", "status": "fail", "evidence": "src.py:2 print call"}]
    assert validate_convention_report(report(snapshot, checks=checks), snapshot)


def test_missing_snapshot_digest_blocks(snapshot):
    snapshot["sha256"] = ""
    assert validate_convention_report(report(snapshot), snapshot)


@pytest.mark.parametrize("patch", [{"content": None}, {"content": []}, {"sha256": None}, {"sha256": []}])
def test_malformed_persisted_snapshot_blocks_without_crashing(snapshot, patch):
    snapshot.update(patch)
    assert "진행하지 말고" in conventions_guide(snapshot)
    assert validate_convention_report(report(snapshot), snapshot)
