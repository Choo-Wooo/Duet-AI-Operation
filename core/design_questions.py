"""User-owned design decisions, persisted independently of automatic tool approval."""
from __future__ import annotations

import re

INITIAL = """[설계 질문 모드 · 초기 질문]
사람의 요청을 읽고 구현 전에 확인할 설계 질문을 한 번에 정리하세요.
각 질문에 선택지, 추천안, 선택에 따른 영향을 붙이고 이미 답한 내용은 다시 묻지 마세요.
질문이 없으면 이해한 요구와 계획을 간단히 제시하고 진행 여부를 확인하세요.
이번 턴에는 읽기·검색만 하세요. 파일 수정·구현·위임·병렬 작업을 시작하지 마세요.
질문 묶음을 응답 본문에 쓰고 ASK_HUMAN 지시문으로 마치세요. 자동 승인이 켜져 있어도 실제 사람의 답변을 기다립니다.
"""
GUIDE = """[설계 질문 모드]
사람이 정한 요구·답변을 기준으로 일반 구현과 도구 승인은 기존 자동 진행 정책을 따릅니다.
취향·요구·설계 범위·데이터 손실·호환성 등 사람이 결정해야 할 중요한 미확정 사항은 임의로 결정하지 마세요.
중요한 질문만 ASK_HUMAN 으로 선택지·추천안·영향과 함께 기록하세요. 시스템이 사이클 끝에 묶어 질문합니다.
그 답에 의존하는 변경은 보류하고 독립적인 작업만 계속하세요. 사소한 구현 선택은 스스로 판단하세요.
보류한 작업을 완료했다고 보고하거나 답변 전에 RESUME/RESUME_WORK 하지 마세요.
"""


class DesignQuestions:
    def __init__(self, orch):
        self.orch = orch

    @property
    def enabled(self):
        return bool(self.orch.cfg.settings.get("design_questions"))

    @property
    def state(self):
        return self.orch.cfg.state.design_questions

    def save(self):
        self.orch.cfg.save_state()
        self.orch.emit_status()

    def begin(self, nxt):
        if not self.enabled or not nxt or nxt[1] != "human":
            return nxt
        s = self.state
        if s.get("phase") in ("awaiting_initial", "awaiting_final"):
            questions = s.get("initial", "") if s["phase"] == "awaiting_initial" else self.summary()
            s.update(phase="running", pending=[], initial="")
            self.save()
            return (self.orch.cfg.main, "system",
                    f"사람의 답변은 DIALOGUE.md #{nxt[2]}입니다. 다음 질문과 대조해 답변을 반영하세요. "
                    "미응답·모호한 항목은 결정된 것으로 간주하지 말고 다시 ASK_HUMAN으로 보류하세요.\n" + questions)
        if s.get("phase", "idle") in ("idle", "initial") and not self.orch.cfg.state.task and not self.orch.work.active():
            s.update(phase="initial", pending=[])
            self.save()
            return (self.orch.cfg.main, "design_questions", f"사람의 요청: DIALOGUE.md #{nxt[2]}\n" + INITIAL)
        return nxt

    def initial_answer(self, text):
        from .dialogue import extract_directives
        questions = [v for k, v in extract_directives(text) if k == "ASK_HUMAN"]
        text = re.sub(r"<!--\s*duet:.*?-->", "", text, flags=re.S).strip() or "\n".join(questions)
        self.state.update(phase="awaiting_initial", initial=text or "계획을 검토하고 진행 여부를 알려주세요.")
        self.save()
        self.orch.bus.emit("ask", self.orch.cfg.main, text=self.state["initial"])

    def defer(self, role, question):
        s = self.state
        pending = s.setdefault("pending", [])
        item = {"role": role, "question": question.strip() or "결정이 필요한 내용 확인"}
        if item in pending:
            return False
        pending.append(item)
        if s.get("phase", "idle") == "idle":
            s["phase"] = "running"
        self.save()
        self.orch.notice(f"[설계 질문 보류] {role}: {item['question']}")
        return True

    def blocked(self, role):
        return self.enabled and any(q["role"] == role for q in self.state.get("pending", []))

    def summary(self):
        return "\n\n".join(f"{n}. [{q['role']}] {q['question']}" for n, q in enumerate(self.state.get("pending", []), 1))

    def finish(self):
        if not self.enabled or self.orch.work.busy():
            return
        s = self.state
        if s.get("phase") in ("awaiting_initial", "awaiting_final"):
            return
        if s.get("pending"):
            s["phase"] = "awaiting_final"
            self.save()
            text = "[설계 질문 · 사이클 마무리]\n진행 중 보류한 중요한 질문입니다. 번호별로 답변해 주세요. 관련 작업은 답변 전까지 보류됩니다.\n\n" + self.summary()
            self.orch.dialogue.append_note(text)
            self.orch.bus.emit("ask", self.orch.cfg.main, text=text)
        elif not self.orch.cfg.state.task and not self.orch.work.active() and s.get("phase") == "running":
            s["phase"] = "idle"
            self.save()
