"""JEV chooses semantic turn destinations; phase validation remains in Duet."""
from __future__ import annotations

import re

from .dialogue import control_text, extract_directives, without_directives

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
        blockers = [v.partition(' ')[2].strip() for k, v in turn.directives
                    if k == 'REPORT' and v.split()[:1] in (['blocked'], ['deviation'])]
        blockers = [v for v in blockers if v and v not in body and v not in questions]
        return ('\n\n'.join(part for part in (body, *questions, *blockers) if part)
                or '질문 내용이 비어 있습니다. 원래 작성자의 질문 보완이 필요합니다.')

    def _wait(self, role, text):
        o = self.orch
        o._wait_task(text)
        if o.design.enabled:
            o.design.defer(role, text)
        else:
            o.bus.emit('ask', role, text=text)
        return True, None

    def _technical_wait(self, role, text):
        """A failed protocol repair is not a new user-owned design decision."""
        o = self.orch
        if not o.cfg.state.task or not o.cfg.state.task['waiting']:
            o._wait_task(text)
        o.notice(text, 'warn')
        return True, None

    @staticmethod
    def _source_issue(tr, turn):
        # full_text can contain intermediate assistant messages or superseded
        # controls. Only the final response is compared with the recorded turn.
        response = tr.text or ''
        if not response.strip() or response == turn.body:
            return ''
        response_controls = extract_directives(response)
        if response_controls and response_controls != turn.directives:
            return ('최종 응답과 DIALOGUE의 실행 지시문이 일치하지 않습니다. '
                    f'응답: {response_controls!r}; DIALOGUE: {turn.directives!r}. '
                    '같은 턴의 의도한 지시문을 두 출력에 일치시켜 다시 제출하세요.')
        # Compare the parsed work specification, so harmless YAML formatting
        # changes do not conflict. Match the executor's parser for both sources.
        from .work import WORK_BLOCK, parse_work_block

        def work(text):
            blocks = list(WORK_BLOCK.finditer(text))
            if not blocks:
                return None
            try:
                return ('parsed', parse_work_block(text))
            except ValueError:
                return ('invalid', blocks[-1].group('body').strip())

        response_work = work(response)
        if response_work is not None and response_work != work(turn.body):
            return ('최종 응답과 DIALOGUE의 duet-work 작업 목록이 일치하지 않습니다. '
                    '실행할 작업 목록 하나를 두 출력에 일치시켜 다시 제출하세요.')
        # A document-only control plus a plain "recorded" acknowledgement is
        # the normal direct-writing protocol and needs no repair.
        return ''

    def _repair(self, role, kind, task, issue):
        phase = task['phase'] if task else 'conversation'
        repair_kind = kind if kind in ('design_questions', 'jev_consult') else phase if task else 'system'
        return True, (role, repair_kind, '[JEV 관제 · 응답 보완] 방금 응답의 질문 또는 제어 형식만 바로잡으세요. '
                      '구현·검토 결과를 임의로 바꾸거나 사용자 결정을 대신하지 마세요. 질문은 결정 대상·필요한 맥락·'
                      '선택지가 있다면 각 영향까지 적으세요. 제어 지시문은 코드 블록 밖 독립 줄의 '
                      '<!-- duet: ... --> 형식으로 제출하세요. 계획 제출은 전문과 PLAN ready를 다시 내세요. '
                      'DIALOGUE를 직접 기록한다면 최종 응답과 실행 지시문·작업 목록이 같아야 합니다.\n'
                      + (issue or '질문·지시문 또는 실행 선언을 확인하세요. 작업을 등록·재개·취소했다고 말했지만 '
                         '실행 명령이 없다면 실제 보드를 확인하고 필요한 지시문을 제출하세요. 이미 실행 중인 '
                         '작업을 다시 명령하지 말고, 단순 상태 요약이라면 새 명령을 내지 않았음을 명확히 쓰세요.'))

    def _pending_user_input(self):
        check = getattr(self.orch, 'has_pending_user_input', None)
        return check() if callable(check) else not self.orch.inbox.empty()

    def _discard(self, role, kind, turn, reason):
        defer = getattr(self.orch, 'defer_control_turn', None)
        if callable(defer):
            defer(role, kind, turn, reason)
        return True, None

    async def route(self, role, kind, tr, turn):
        o = self.orch
        task = o.cfg.state.task
        # Route the same canonical document that the executor reads. full_text
        # can contain an earlier draft later replaced by the final response.
        text = turn.body or tr.text or ''
        source_issue = self._source_issue(tr, turn) if o.jev.mode == 'on' and o.jev.enabled('flow') else ''
        # In active JEV mode, source disagreement is a protocol error even when
        # the classifier is unavailable. Never merge or execute either side.
        if source_issue:
            turn.__dict__['jev_route_source'] = 'source_validation'
            turn.__dict__['jev_source_issue'] = source_issue
            if o.stop_requested or not o.not_paused.is_set() or self._pending_user_input():
                return self._discard(role, kind, turn, '출력 불일치 보완 전에 중단 또는 새 사용자 입력이 도착했습니다.')
            phase = task['phase'] if task else 'conversation'
            key = (role, task['id'] if task else o.current_task.get(role, ''), phase, 'source_repair')
            count = self.retries.get(key, 0)
            if count >= MAX_ROUTE_RETRIES:
                turn.__dict__['jev_route_action'] = 'protocol_wait'
                return self._technical_wait(role, f'응답·DIALOGUE 불일치 보완 {MAX_ROUTE_RETRIES}회 후에도 해결되지 않았습니다.\n' + source_issue)
            self.retries[key] = count + 1
            turn.__dict__['jev_route_action'] = 'repair'
            return self._repair(role, kind, task, source_issue)
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
        revision = o.jev._revision(role)
        blocker_route = getattr(o.jev, 'route_blocker', None)
        is_blocker = (o.jev.mode == 'on' and worker and not special and not issue
                      and (turn.directive('REPORT') or '').split()[:1] == ['blocked']
                      and turn.directive('ASK_HUMAN') is None and turn.directive('CONSULT') is None
                      and callable(blocker_route))
        if is_blocker:
            allowed.append('dependency')
            action = await blocker_route(role, text, state, kind=kind, directives=turn.directives)
            if action is None:
                # An uncertain owner does not imply a user decision. Clarify the
                # blocker at its author with the same bounded repair budget.
                action = 'repair'
                issue = '진행을 막는 구체적인 선행 조건과 필요한 결정, 이미 가능한 다음 단계를 명시하세요. 사람의 허가를 추정하지 마세요.'
        else:
            action = await o.jev.route_turn(role, text, state, kind=kind, directives=turn.directives, issue=issue, allowed=allowed,
                                            evidence={'response_text': tr.text or '', 'dialogue_text': turn.body,
                                                      'source_control_issue': source_issue})
        if (o.stop_requested or not o.not_paused.is_set() or self._pending_user_input()
                or o.cfg.state.task is not task or o.jev._revision(role) != revision or o.jev._blocked()):
            return self._discard(role, kind, turn, 'JEV 판단 중 사용자 입력·실행 상태가 변경되어 이전 판단을 적용하지 않았습니다.')
        clarify = getattr(o.jev, 'needs_control_clarification', None)
        if action in (None, 'follow') and not is_blocker and callable(clarify) and clarify():
            # No execution route cleared the threshold. Ask for evidence only;
            # do not treat an uncertain missing-control finding as established.
            action = 'repair'
            issue = ('[판단 근거 보완] 실행 명령이 없는 현재 응답이 작업 상태 요약인지, 이번 턴에 새 '
                     '작업 등록·재개·취소를 요청하는 것인지 확실하지 않습니다. 실제 보드를 확인하고 '
                     '의도를 명확히 쓰세요. 새 명령이 필요할 때만 해당 지시문을 제출하세요.')
        if action is None or action not in allowed:
            return self._consult_answer(role, kind, text, turn, task)
        turn.__dict__['jev_routed'] = True
        turn.__dict__['jev_route_action'] = action
        key = (role, task['id'] if task else o.current_task.get(role, ''), phase, action)
        if action in ('repair', 'continue', 'architect'):
            count = self.retries.get(key, 0)
            if count >= MAX_ROUTE_RETRIES:
                return self._technical_wait(role, f'JEV {action} 보완 {MAX_ROUTE_RETRIES}회 후에도 해결되지 않았습니다.\n' + self._question(text, turn))
            self.retries[key] = count + 1
        if action == 'repair':
            return self._repair(role, kind, task, issue)
        if action == 'human':
            if kind == 'design_questions':
                o.design.initial_answer(text)
                return True, None
            self.consult = None
            return self._wait(role, self._question(text, turn))
        if action == 'dependency':
            return self._technical_wait(role, '이미 합의한 선행 조건을 기다립니다.\n' + self._question(text, turn))
        if action == 'architect':
            self.consult = dict(role=role, phase=phase, task=task, kind=kind)
            return True, (o.cfg.main, 'jev_consult', '[JEV 관제 · 내부 협의] 다음 작업자의 기술 질문·진행 장애에 기존 요구와 설계에 근거해 답하세요. '
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
            return self._technical_wait(role, '내부 협의 응답에 단계 변경 지시문이 섞여 있습니다. 답변을 확인해 주세요.\n' + text)
        if not text.strip():
            return self._technical_wait(role, '내부 협의 답변이 비어 있습니다.')
        next_kind = pending['phase'] if task else 'system'
        return True, (pending['role'], next_kind, '[JEV 관제 · 설계자 답변]\n' + text + '\n\n기존 범위와 합의 단계에서 이 답변을 반영하세요.')
