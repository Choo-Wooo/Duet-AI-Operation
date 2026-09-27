"""문서 탭 폴더 트리: 폴더별 정리, 접기/펼치기 상태, 기본 펼침 규칙, 검색 시 펼침, 최근 수정 묶음."""
import json

from duet.tests.test_markdown import node, STATIC

ROWS = ([{"path": "DIALOGUE.md", "mtime": 100}, {"path": "SERVER_PLAN.md", "mtime": 90}]
        + [{"path": f"docs/analysis/survey/s{i}.md", "mtime": i} for i in range(1, 13)]
        + [{"path": "docs/analysis/a.md", "mtime": 50}, {"path": "docs/plans/task-2.md", "mtime": 60},
           {"path": "docs/plans/task-10.md", "mtime": 61}, {"path": "docs/readme.md", "mtime": 40},
           {"path": ".duet/memory/architect.md", "mtime": 70}])


def build(**opts):
    out = node("const t=require(process.argv[1]);process.stdout.write(JSON.stringify(t.build(JSON.parse(process.argv[2]),JSON.parse(process.argv[3]))));",
               str(STATIC / "doctree.js"), json.dumps(ROWS), json.dumps(opts))
    return json.loads(out)


def keys(entries):
    return [e["key"] for e in entries]


def test_default_tree_groups_by_folder():
    e = build(recent=0)
    k = keys(e)
    assert k[:2] == ["f:DIALOGUE.md", "f:SERVER_PLAN.md"]  # 루트 문서가 먼저, DIALOGUE.md 맨 위
    assert k.index("d:docs") < k.index("d:.duet")  # docs 가 .duet 보다 먼저
    docs = next(x for x in e if x["key"] == "d:docs")
    assert docs["open"] and docs["count"] == 16
    # 두 번째 단계 폴더는 기본 접힘 → 안의 문서는 보이지 않음
    assert "d:docs/analysis" in k and "f:docs/analysis/a.md" not in k
    assert next(x for x in e if x["key"] == "d:docs/analysis")["count"] == 13
    assert "f:docs/readme.md" in k and k.index("d:docs/plans") < k.index("f:docs/readme.md")  # 폴더 먼저, 문서 나중


def test_toggle_state_and_numeric_sort():
    e = build(recent=0, state={"docs/plans": True, "docs": True})
    k = keys(e)
    assert k.index("f:docs/plans/task-2.md") < k.index("f:docs/plans/task-10.md")  # 숫자 순
    e = build(recent=0, state={"docs": False})
    assert "d:docs/analysis" not in keys(e) and next(x for x in e if x["key"] == "d:docs")["open"] is False


def test_selected_doc_folder_opens_unless_explicitly_closed():
    e = build(recent=0, selected="docs/analysis/survey/s3.md")
    k = keys(e)
    assert "f:docs/analysis/survey/s3.md" in k
    e = build(recent=0, selected="docs/analysis/survey/s3.md", state={"docs/analysis": False})
    assert "f:docs/analysis/survey/s3.md" not in keys(e)


def test_query_expands_matches_only():
    e = build(query="s1")
    k = keys(e)
    assert "f:docs/analysis/survey/s1.md" in k and "f:docs/analysis/survey/s11.md" in k
    assert "f:DIALOGUE.md" not in k and not any(x.startswith("r:") for x in k)
    assert all(x["open"] for x in e if x["kind"] == "folder")


def test_recent_group_collapsible():
    e = build()
    assert e[0]["key"] == "d:@recent" and e[0]["open"]
    recent = [x for x in e if x["key"].startswith("r:")]
    assert len(recent) == 8 and recent[0]["path"] == "DIALOGUE.md" and recent[0]["name"] == "DIALOGUE.md"
    assert any(x["dir"] == ".duet/memory" for x in recent)
    e = build(state={"@recent": False})
    assert not any(x["key"].startswith("r:") for x in e)


def test_folders_listing():
    out = node("const t=require(process.argv[1]);process.stdout.write(JSON.stringify(t.folders(JSON.parse(process.argv[2])).sort()));",
               str(STATIC / "doctree.js"), json.dumps(ROWS))
    assert json.loads(out) == sorted([".duet", ".duet/memory", "docs", "docs/analysis", "docs/analysis/survey", "docs/plans"])
