"""JEV chooses semantic turn destinations; phase validation remains in Duet."""
from __future__ import annotations

import re

from .dialogue import control_text, without_directives

MAX_ROUTE_RETRIES = 2


class TurnRouter:
    def __init__(self, orch):
        self.orch = orch
        self.retries = {}
        self.consult = None

    def reset(self):
        self.retries.clear()
        self.consult = None

    def _issue(self, role, kind, tr, turn, task):
        if getattr(tr, 'plan_error', None):
            return str(tr.plan_error)
        actions = [(k, v) for k, v in turn.directives if k not in ('TASK', 'STATUS', 'PROPOSE_ROLE')]
        if kind == 'jev_consult':
            return 'Consultation needs an answer or ASK_HUMAN, not a phase-changing directive.' if any(k != 'ASK_HUMAN' for k, _ in actions) else ''
        if kind == 'design_questions':
            return 'Initial design turn must ask clear questions or confirm the stated plan with ASK_HUMAN.' if not actions or any(k != 'ASK_HUMAN' for k, _ in actions) else ''
        if actions and all(k == 'ASK_HUMAN' for k, _ in actions):
            return ''  # JEV evaluates the actual question, including empty content.
        if task and task['phase'] == 'implement' and role == task['role'] and not actions:
            return ''  # A plain completion claim can go to review, never acceptance.
        if not task:
            if not actions and re.search(r'(?m)^\s*(?:AGREE|ACCEPT|REVISE|REWORK|REPORT|DELEGATE|ASK_HUMAN|PLAN|RESUME)\b', control_text(turn.body)):
                return 'A plain-text control instruction was not parsed. Reissue it as a standalone duet HTML comment.'
            return ''
        phase, worker, main = task['phase'], task['role'], self.orch.cfg.main
        if role == main and phase == 'verify' and ('ACCEPT', '') in actions:
            actions = [(k, v) for k, v in actions if k != 'DELEGATE']
        if len(actions) != 1:
            return f'Phase {phase} requires one unambiguous control instruction; received {actions!r}.'
        action, arg = actions[0]
        if (action == 'CANCEL' and role == main and arg) or (action == 'RESUME' and role == main and not arg and task['waiting'] and phase in ('plan', 'implement')):
            return ''
        valid = ((phase == 'plan' and role == worker and action == 'PLAN' and arg == 'ready')
                 or (role == worker and action == 'REPORT' and arg.split()[:1] == ['blocked'])
                 or (phase == 'plan_review' and role == main and ((action == 'AGREE' and arg == f"v{task['submitted_version']}") or (action == 'REVISE' and arg)))
                 or (phase == 'implement' and role == worker and action == 'REPORT' and arg.split()[:1] in (['done'], ['deviation']))
                 or (phase == 'verify' and role == main and ((action == 'ACCEPT' and not arg) or (action == 'REWORK' and arg))))
        return '' if valid else f'Control {action} {arg} is not valid for author {role} in phase {phase}.'

    @staticmethod
    def _question(text, turn):
        body = without_directives(text).strip()
        questions = [v for k, v in turn.directives if k == 'ASK_HUMAN' and v.strip() and v not in body]
        return ('\n\n'.join(part for part in (body, *questions) if part)
                or '질문 내용이 비어 있습니다. 원래 작성자의 질문 보완이 필요합니다.')

    def _wait(self, role, text):
        o = self.orch
        o._wait_task(text)
        if o.design.enabled:
            o.design.defer(role, text)
        else:
            o.bus.emit('ask', role, text=text)
        return True, None

    async def route(self, role, kind, tr, turn):
        o = self.orch
        task = o.cfg.state.task
        text = turn.body if turn.body != tr.text else tr.full_text or tr.text or turn.body
        if not o.jev.enabled('flow'):
            return self._consult_answer(role, kind, text, turn, task)
        # Existing human gates cannot be resumed by an inferred turn transition.
        if task and task['waiting'] or o.design.blocked(role):
            return False, None
        phase = task['phase'] if task else 'conversation'
        issue = self._issue(role, kind, tr, turn, task)
        allowed = ['follow', 'repair', 'human']
        worker = role != o.cfg.main and (not task or role == task['role'])
        special = kind in ('design_questions', 'jev_consult', 'checkpoint')
        if worker and kind != 'human' and not special:
            allowed.append('architect')
            report = turn.directive('REPORT') or ''
            if (not task or phase == 'implement') and turn.directive('ASK_HUMAN') is None and turn.directive('CONSULT') is None and report.split()[:1] not in (['blocked'], ['deviation']):
                allowed += ['continue', 'review']
        state = task or {'instruction': o.current_task.get(role, ''), 'phase': phase}
        revision = o.jev._revision()
        action = await o.jev.route_turn(role, text, state, kind=kind, directives=turn.directives, issue=issue, allowed=allowed)
        if (o.stop_requested or not o.not_paused.is_set() or not o.inbox.empty()
                or o.cfg.state.task is not task or o.jev._revision() != revision or o.jev._blocked()):
            return True, None
        if action is None or action not in allowed:
            return self._consult_answer(role, kind, text, turn, task)
        turn.__dict__['jev_routed'] = True
        key = (role, task['id'] if task else o.current_task.get(role, ''), phase, action)
        if action in ('repair', 'continue', 'architect'):
            count = self.retries.get(key, 0)
            if count >= MAX_ROUTE_RETRIES:
                return self._wait(role, f'JEV {action} 보완 {MAX_ROUTE_RETRIES}회 후에도 해결되지 않았습니다.\n' + self._question(text, turn))
            self.retries[key] = count + 1
        if action == 'repair':
            repair_kind = kind if kind in ('design_questions', 'jev_consult') else phase if task else 'system'
            return True, (role, repair_kind, '[JEV 관제 · 응답 보완] 방금 응답의 질문 또는 제어 형식만 바로잡으세요. '
                          '구현·검토 결과를 임의로 바꾸거나 사용자 결정을 대신하지 마세요. 질문은 결정 대상·필요한 맥락·'
                          '선택지가 있다면 각 영향까지 적으세요. 제어 지시문은 코드 블록 밖 독립 줄의 '
                          '<!-- duet: ... --> 형식으로 제출하세요. 계획 제출은 전문과 PLAN ready를 다시 내세요.\n'
                          + (issue or '질문 내용이 모호하거나 응답 내용과 질문 지시문이 맞지 않습니다.'))
        if action == 'human':
            if kind == 'design_questions':
                o.design.initial_answer(text)
                return True, None
            self.consult = None
            return self._wait(role, self._question(text, turn))
        if action == 'architect':
            self.consult = dict(role=role, phase=phase, task=task, kind=kind)
            return True, (o.cfg.main, 'jev_consult', '[JEV 관제 · 내부 협의] 다음 작업자의 기술 질문에 기존 요구와 설계에 근거해 답하세요. '
                          '새 사용자 결정이 필요하면 명확한 ASK_HUMAN 질문을 제출하세요. '
                          '이번 턴은 읽기 전용이며 AGREE/ACCEPT/DELEGATE 등 단계 변경 지시문은 쓰지 마세요.\n'
                          + self._question(text, turn))
        if action == 'continue':
            return True, (role, 'implement' if task else 'system', '[JEV 관제] 응답에 남아 있는 합의 범위 내 구현을 마치고 '
                          '실제 변경·검사 결과를 보고하세요. 막혔으면 구체적인 질문 또는 REPORT blocked 이유를 남기세요.')
        if action == 'review':
            if task:
                o._phase('verify')
                return True, (o.cfg.main, 'verify', f'JEV 인계 판단: 구현 보고 #{turn.n}. 기존 테스트와 코딩 컨벤션을 검토하세요.')
            return True, (o.cfg.main, 'report', str(turn.n))
        # `follow` uses existing validators. ASK_HUMAN stays a real user wait;
        # do not let autopilot turn a reviewed user question into auto-consent.
        if turn.directive('ASK_HUMAN') is not None and not o.design.enabled:
            self.consult = None
            return self._wait(role, self._question(text, turn))
        return self._consult_answer(role, kind, text, turn, task)

    def _consult_answer(self, role, kind, text, turn, task):
        if kind != 'jev_consult' or not self.consult:
            return False, None
        if turn.directive('ASK_HUMAN') is not None:
            return self._wait(role, self._question(text, turn))
        pending, self.consult = self.consult, None
        if pending['task'] is not task or (task and task['waiting']):
            return True, None
        # No protocol means an ordinary consultation answer; preserve all gates.
        if any(k not in ('STATUS',) for k, _ in turn.directives):
            return self._wait(role, '내부 협의 응답에 단계 변경 지시문이 섞여 있습니다. 답변을 확인해 주세요.\n' + text)
        if not text.strip():
            return self._wait(role, '내부 협의 답변이 비어 있습니다.')
        next_kind = pending['phase'] if task else 'system'
        return True, (pending['role'], next_kind, '[JEV 관제 · 설계자 답변]\n' + text + '\n\n기존 범위와 합의 단계에서 이 답변을 반영하세요.')
