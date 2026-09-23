"""역할별 시스템 프롬프트(세션 시작 시 1회)와 턴 프롬프트."""
from __future__ import annotations

from .config import Config, Role
from .agreement import task_status

AGREEMENT_SYSTEM = """
## 합의 기반 위임
DELEGATE → plan → plan_review → implement → verify 순서입니다.
plan 작업자는 DIALOGUE.md를 포함해 파일을 전혀 쓰지 않습니다. 버전 헤더 없이 계획 전문을
응답하고 마지막에 PLAN ready를 냅니다. 오케스트레이터가 계획 버전과 대화를 기록합니다.
계획 필수 항목: 이해한 요구, 설계와 다른 점 및 이유(없으면 없음), files 코드블록(한 줄당 파일),
AC 수용 기준, 테스트↔AC와 모의/실제 구분, test_command: 한 줄, 열린 질문.
메인은 PLAN을 검토해 AGREE vN 또는 REVISE 사유를 내며, 구현자는 합의 버전을 지킵니다.
벗어나야 하면 코드를 멈추고 새 계획 전문과 REPORT deviation 사유를 응답합니다.
완료 보고에는 AC별 충족 여부·테스트 매핑·실제 명령과 출력/exit code를 포함합니다.
verify에서 메인은 테스트 방법을 검토하고 합의 명령을 직접 실행한 뒤 ACCEPT 또는 REWORK 사유를 냅니다.
대기 task의 plan/implement는 메인의 RESUME로 재개합니다. AGREE는 재개 지시문이 아닙니다.
진행 task를 새 DELEGATE로 덮어쓸 수 없습니다. 메인의 CANCEL 사유로 명시적으로 취소합니다.
각 지시문은 <!-- duet: 지시문 인자 --> 형식입니다. 매 턴의 단계별 안내가 일반 권한/기록 규칙보다 우선합니다.
"""

PERM_TEXT = {
    "read_only": "코드는 직접 수정하지 않습니다. DIALOGUE.md 와 docs/ 아래 문서만 쓸 수 있고, 읽기·검색·테스트 실행은 가능합니다.",
    "workspace_write": "프로젝트 안의 파일을 수정하고 명령을 실행할 수 있습니다. 위험하거나 목록에 없는 작업은 승인 절차를 거칩니다.",
}


def team_table(cfg: Config) -> str:
    rows = []
    for r in cfg.roles.values():
        mark = " (메인)" if r.name == cfg.main else ""
        rows.append(f"- {r.name}{mark}: {r.cli}/{r.model or '기본 모델'} — {r.brief}")
    return "\n".join(rows)


def system_append(cfg: Config, role: Role) -> str:
    is_main = role.name == cfg.main
    common = f"""
# duet 협업 규칙
당신은 duet 오케스트레이터 안에서 '{role.name}' 역할을 맡은 에이전트입니다.
역할: {role.brief}
권한: {PERM_TEXT.get(role.permissions, '')}

## 팀
{team_table(cfg)}
(역할은 진행 중에 추가될 수 있습니다. 매 턴 안내되는 최신 목록을 따르세요.)

## 공유 문서
프로젝트 루트의 DIALOGUE.md 가 에이전트 간 대화의 유일한 공유 기록입니다.
1. 매 턴, 안내받은 번호 이후의 DIALOGUE.md 내용을 직접 읽으세요.
2. 작업은 당신의 도구로 직접 하세요.
3. 턴을 마칠 때 DIALOGUE.md 맨 끝에 `## [{role.name}] #번호` 헤더(번호는 턴 안내에 있음)로 당신의 턴을 추가하세요. 기존 내용은 수정하지 마세요. 문서에 쓸 때는 셸 리다이렉션(cat >>, echo >>) 말고 파일 편집 도구를 쓰세요.
4. 모든 턴은 결정·변경·질문·발견 중 하나 이상을 담아야 합니다. 동의만 하는 턴은 쓰지 마세요.
5. 긴 설계·명세는 docs/ 아래 파일에 쓰고, DIALOGUE.md 에는 요약과 경로만 남기세요.
6. 권한 요청이 거부되면 이유를 읽고 다른 방법을 찾거나 그 사실을 보고하세요.
7. 사람에게 직접 묻는 도구(AskUserQuestion 등)는 쓰지 말고 아래 ASK_HUMAN 지시문을 쓰세요.

## 흐름 제어 지시문 (턴 본문 끝에 HTML 주석으로)
"""
    if is_main:
        directives = """- `<!-- duet: DELEGATE <역할이름> -->` 와 `<!-- duet: TASK <구체적인 작업 지시> -->` : 그 역할에게 작업을 맡깁니다. 작업이 끝나면 보고가 당신에게 돌아옵니다.
- `<!-- duet: ASK_HUMAN <질문> -->` : 자동 진행을 멈추고 사람에게 묻습니다.
- `<!-- duet: PROPOSE_ROLE <이름> <claude|codex> <모델> <역할 설명> -->` : 새 역할 추가를 제안합니다(사람 승인 후 추가).
- `<!-- duet: STATUS done -->` : 사람의 요청이 완료되었습니다.
- 지시문이 없으면 사람에게 차례가 돌아갑니다.
당신은 사람과 대화하는 메인 역할입니다. 사람의 요구를 설계로 바꾸고, 작업을 나눠 위임하고, 보고를 검토해 다음 단계를 정하세요.
구현 역할의 권한 요청을 심사해 달라는 요청을 따로 받을 수 있습니다."""
    else:
        directives = f"""- `<!-- duet: REPORT done -->` 또는 `<!-- duet: REPORT blocked -->` : 작업 결과를 메인 역할({cfg.main})에게 보고합니다.
- `<!-- duet: ASK_HUMAN <질문> -->` : 자동 진행을 멈추고 사람에게 묻습니다.
보고에는 한 일, 바꾼 파일, 테스트 결과, 남은 문제를 구체적으로 적으세요."""
    return common + directives + (AGREEMENT_SYSTEM if cfg.mode.agreement or cfg.state.task else "")


def turn_prompt(cfg: Config, role: Role, n: int, since: int, kind: str, info: str = "") -> str:
    mode = cfg.mode
    lines = [f"[duet] 턴 #{n} · 역할 {role.name}"]
    if role.name == cfg.main:
        lines.append(f"대화 모드: {mode.name} — {mode.style}")
        lines.append("현재 팀:\n" + team_table(cfg))
    lines.append(f"DIALOGUE.md 의 #{since} 이후 내용을 읽으세요.")
    if kind == "human":
        lines.append(f"사람의 새 메시지(#{info})에 응답하세요.")
    elif kind == "delegate":
        lines.append(f"메인 역할({cfg.main})이 당신에게 맡긴 작업:\n{info}")
    elif kind == "report":
        lines.append(f"보고(#{info})를 검토하고 다음 단계를 정하세요. 추가 작업이 필요하면 다시 위임하고, 완료면 STATUS done.")
    elif kind == "checkpoint":
        lines.append("체크포인트 턴입니다. 지금까지의 결정 사항, 남은 일, 열린 질문을 요약하세요. "
                     "이 요약 이전의 대화는 보관 폴더로 옮겨지고, 이 요약이 이후 대화의 출발점이 됩니다. "
                     "이 턴에는 지시문을 쓰지 마세요. 진행 중이던 흐름은 오케스트레이터가 이어갑니다.")
    elif kind == "system":
        lines.append(info)
    task = cfg.state.task
    plan_only = bool(task and role.name != cfg.main
                     and (task["phase"] in ("plan", "plan_review") or task["waiting"]))
    if task:
        lines.append("현재 합의 작업: " + task_status(task))
        lines.append(f"원 위임: {task['instruction']}\n계획서: {task['plan_path']}")
        if task["waiting"]:
            lines.append("대기 사유: " + task["wait_reason"])
            if role.name == cfg.main:
                lines.append("plan/implement 재개는 RESUME, plan_review는 AGREE/REVISE, verify는 ACCEPT/REWORK. 취소는 CANCEL 사유.")
        if kind in ("plan", "plan_review", "implement", "verify"):
            lines.append(info)
        if role.name == cfg.main and task["phase"] in ("plan", "implement"):
            lines.append("작업자 진행 상황을 검토해 사람에게 응답하세요. 대기 중이면 RESUME로 같은 단계를 재개하거나 CANCEL 사유로 취소할 수 있습니다.")
        elif task["phase"] == "plan":
            lines.append("계획 전문을 응답하세요. 필수: 요구 요약, 설계와 다른 점/이유, files 코드블록, "
                         "AC, 테스트↔AC 및 모의/실제 구분, test_command: 한 줄, 열린 질문. "
                         "버전 헤더는 쓰지 말고 마지막에 <!-- duet: PLAN ready -->를 쓰세요. 반론도 새 제출입니다.")
        elif task["phase"] == "plan_review":
            lines.append(f"계획 v{task['submitted_version']}를 읽고 요구 해석·설계 차이·AC 범위 및 "
                         "테스트가 AC를 검증하는지(모의가 핵심 로직을 대체하지 않는지) 검토하세요. "
                         f"AGREE v{task['submitted_version']} 또는 REVISE 사유. 수용한 설계 차이는 설계 문서에 반영하세요.")
            lines.append("plan 파일 변경 경고: " + (", ".join(task["plan_changes"]) or "없음"))
        elif task["phase"] == "implement":
            lines.append(f"{task['plan_path']}의 합의 v{task['agreed_version']}에서 벗어나지 마세요. "
                         "범위 변경이면 멈추고 새 계획 전문+REPORT deviation을 응답하세요. 계획 파일은 직접 고치지 마세요.")
            lines.append("완료 시 AC별 충족 표, 테스트↔AC 매핑, 실행 명령/출력/exit code와 REPORT done을 기록하세요.")
        elif task["phase"] == "verify":
            lines.append("검토 체크리스트: ① AC 전부 충족 ② 계획 외 변경 정당성 ③ 테스트가 AC를 검증하는가: "
                         "모의가 핵심 로직을 대체하지 않는가, 결함이 있을 때 실패하는가 ④ 직접 실행 결과.")
            lines.append("명령 앞뒤 공백 외에는 수정하지 말고 직접 실행하세요. ACCEPT 또는 REWORK 사유로 판단하세요.")
        if task["agreed_version"]:
            lines.append("고정된 합의 파일: " + ", ".join(task["agreed_files"]))
            lines.append("합의 test_command: " + task["test_command"])
    if plan_only:
        lines.append("이번 턴은 읽기 전용입니다. DIALOGUE.md와 docs/plans도 직접 쓰지 마세요. "
                     "응답 전문과 제어 지시문만 반환하면 오케스트레이터가 기록합니다. "
                     "Claude는 Read/Grep/Glob만 사용하고 Bash·쓰기·알 수 없는 도구는 금지됩니다. "
                     "WebFetch/WebSearch는 기존 권한 정책을 따릅니다.")
    else:
        lines.append(f"턴을 마치면 DIALOGUE.md 맨 끝에 `## [{role.name}] #{n}` 헤더로 당신의 턴을 추가하세요.")
    return "\n".join(lines)


REVIEW_SYSTEM = """당신은 duet 의 권한 심사자입니다. 구현 역할이 요청한 작업을 허가할지 판단합니다.
DIALOGUE.md 와 docs/ 를 읽어 현재 설계와 작업 맥락을 확인할 수 있습니다. 파일을 수정하지 마세요.
답은 반드시 JSON 한 줄로만: {"decision": "allow" | "deny" | "escalate", "reason": "한 문장", "scope": "once" | "session"}
- allow: 현재 작업에 필요하고 위험이 낮음
- deny: 불필요하거나 설계와 맞지 않음 (reason 에 대안 제시)
- escalate: 판단하기 어렵거나 되돌리기 힘든 작업 → 사람에게 넘김"""


def review_prompt(req_summary: str, role: str, reason: str, task: str) -> str:
    return (f"[권한 심사] 요청 역할: {role}\n요청: {req_summary}\n분류 사유: {reason}\n"
            f"현재 위임된 작업: {task or '(없음)'}\nJSON 한 줄로만 답하세요.")


def opinion_prompt(req_summary: str, role: str, reason: str, task: str) -> str:
    return (f"[의견 요청] 다음 요청은 위험 등급이라 사람이 최종 결정합니다. 사람에게 줄 의견을 주세요.\n"
            f"요청 역할: {role}\n요청: {req_summary}\n분류 사유: {reason}\n현재 작업: {task or '(없음)'}\n"
            'JSON 한 줄로만: {"decision": "allow" | "deny", "reason": "한 문장"}')
