"""One readable decision dialogue preserves parallel request identities."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from duet.core.jev import JevResult
from duet.core.agreement import fingerprint
from duet.core.jev_log import append_outcome, migrate_legacy, write_decision
from duet.tests.test_jev_control import api, control, judgment
from duet.web.server import _doc_path, _docs


def records(control):
    return sorted((control.orch.cfg.project / '.duet/logs/jev').rglob('*.md'))


def record(name='alpha'):
    return {'purpose': 'choice', 'mode': 'on', 'model': 'jev-test', 'requested_model': 'jev-test',
            'threshold': .8, 'rubric_version': 'test-1', 'state_hash': 'test-state',
            'input_tokens': 20, 'output_tokens': 3, 'latency_ms': 2, 'stale': False, 'error': None,
            'questions': {'answer': {'instructions': name, 'criteria': {'yes': 'Proceed', 'no': 'Wait'}}},
            'answers': {'answer': {'type': 'choice', 'choice': 'yes', 'confidence': .9}}}


def test_questions_choices_answer_and_outcome_are_readable(control):
    before = fingerprint(control.orch.cfg.project)
    api(control, judgment(progress='progress'))
    control.record_turn('implementer', 'implement', 'New parser evidence.')
    control.record_turn('implementer', 'implement', 'Investigated the empty-input case.')
    assert asyncio.run(control.flow('implementer', None)) == 'continue'
    path, = records(control)
    assert path == control.orch.cfg.project / '.duet/logs/jev/DIALOGUE.md'
    text = path.read_text()
    assert 'Do the recent completed turns show useful progress' in text
    assert all(option in text for option in ('progress', 'repeated_failure', 'blocked', 'unclear'))
    assert '"choice": "progress"' in text and '"관제 적용": true' in text
    assert '확신 기준: 0.8' in text
    assert 'New parser evidence.' not in text  # No duplicate raw task/turn state.
    relative = path.relative_to(control.orch.cfg.project).as_posix()
    assert _doc_path(control.orch.cfg.project, relative) == path
    assert relative in {row['path'] for row in _docs(control.orch.cfg.project)}
    assert fingerprint(control.orch.cfg.project) == before


def test_log_redacts_credentials_and_preserves_markdown_boundaries(control):
    api(control, judgment(answer='yes'))
    questions = {'answer': {'instructions': 'Check test-private-key Bearer other-key ```\n# injected',
                            'criteria': {'yes': {'api_key': 'hidden-key'}, 'no': 'No'}}}
    asyncio.run(control._evaluate('choice', {}, questions))
    text = records(control)[0].read_text()
    assert all(secret not in text for secret in ('test-private-key', 'other-key', 'hidden-key'))
    assert '[redacted]' in text and '````' in text


def test_parallel_outcomes_stay_with_their_own_questions(control):
    async def respond(state, questions):
        await asyncio.sleep(0)
        return judgment(answer=state['id'])
    api(control, side_effect=respond)

    async def one(name):
        await control._evaluate('choice', {'id': name}, {'answer': {'instructions': name, 'criteria': {name: 'chosen'}}})
        await asyncio.sleep(0)
        control._outcome('choice', name, True)

    async def run():
        await asyncio.gather(one('alpha'), one('beta'))
    written = {}

    def capture(project, decision):
        entry = write_decision(project, decision)
        written[decision['questions']['answer']['instructions']] = entry
        return entry

    with patch('duet.core.jev_control.write_decision', side_effect=capture), \
            patch('duet.core.jev_control.append_outcome', wraps=append_outcome) as outcomes:
        asyncio.run(run())
    path, = records(control)
    text = path.read_text()
    assert written['alpha'].path == written['beta'].path == path
    assert written['alpha'].decision_id != written['beta'].decision_id
    assert outcomes.call_count == 2
    for call in outcomes.call_args_list:
        entry, outcome = call.args
        assert entry == written[outcome['행동']]
        # The ID is in both the question entry and its independently appended outcome.
        assert text.count(entry.decision_id) >= 2
        action = '"행동": "' + outcome['행동'] + '"'
        outcome_block, = [block for block in text.split('<!-- duet-jev-entry -->') if action in block]
        assert '- 판단 ID: ' + entry.decision_id in outcome_block


def test_shadow_and_stale_results_do_not_claim_application(control):
    control.orch.cfg.settings['jev_mode'] = 'shadow'
    api(control, judgment(progress='progress'))
    assert asyncio.run(control.flow('implementer', 'No changes')) is None
    assert '"관제 적용": false' in records(control)[0].read_text()

    async def change(state, questions):
        control.orch.cfg.state.last_n += 1
        return judgment(progress='progress')
    api(control, side_effect=change)
    assert asyncio.run(control.flow('implementer', 'No changes')) is None
    path, = records(control)
    text = path.read_text()
    assert '상태 변경으로 적용 제외' in text and '"관제 적용": true' not in text


def test_api_errors_are_logged_and_filesystem_errors_do_not_stop_decisions(control):
    api(control, JevResult(error='timeout'))
    asyncio.run(control._evaluate('flow', {}, {'progress': {'instructions': 'Continue?', 'criteria': {'yes': 'yes', 'no': 'no'}}}))
    assert 'timeout' in records(control)[0].read_text()
    assert '응답 없음' in records(control)[0].read_text()
    control.cooldown_until = 0
    folder = control.orch.cfg.project / '.duet/logs/jev'
    folder.rename(folder.with_name('old-decisions'))
    folder.write_text('occupied')
    api(control, judgment(progress='progress'))
    assert asyncio.run(control.flow('implementer', 'No changes')) == 'continue'


def test_repeated_decisions_and_multiple_outcomes_append_to_one_dialogue(tmp_path):
    first = write_decision(tmp_path, record('first decision'))
    append_outcome(first, {'행동': 'candidate_allow', '관제 적용': False})
    second = write_decision(tmp_path, record('second decision'))
    append_outcome(first, {'행동': 'allow_once', '관제 적용': True})
    append_outcome(second, {'행동': 'defer', '관제 적용': False})

    assert first.path == second.path == tmp_path / '.duet/logs/jev/DIALOGUE.md'
    assert first.decision_id != second.decision_id
    assert list((tmp_path / '.duet/logs/jev').rglob('*.md')) == [first.path]
    text = first.path.read_text()
    assert text.index('first decision') < text.index('second decision')
    assert text.index('candidate_allow') < text.index('second decision') < text.index('allow_once')
    assert text.count(first.decision_id) >= 3
    assert text.count(second.decision_id) >= 2


def test_concurrent_writers_keep_complete_entries_and_outcomes(tmp_path):
    def one(number):
        message = f'question-{number}-begin\n' + (f'payload-{number}\n' * 6000) + f'question-{number}-end'
        entry = write_decision(tmp_path, record(message))
        append_outcome(entry, {'행동': f'action-{number}', '관제 적용': True})
        return entry, message

    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(one, range(12)))
    paths = {entry.path for entry, _ in entries}
    assert len(paths) == 1
    assert len({entry.decision_id for entry, _ in entries}) == len(entries)
    path = paths.pop()
    text = path.read_text()
    for number, (entry, message) in enumerate(entries):
        assert text.count(message) == 1
        assert text.count(entry.decision_id) >= 2
        assert text.count(f'"행동": "action-{number}"') == 1


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_log_rejects_symlinks_without_touching_destination(tmp_path, kind):
    folder = tmp_path / '.duet/logs/jev'
    folder.mkdir(parents=True)
    outside = tmp_path / 'outside'
    if kind == 'directory':
        outside.mkdir()
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)
    else:
        outside.write_text('untouched')
        (folder / 'DIALOGUE.md').symlink_to(outside)
    with pytest.raises(OSError):
        write_decision(tmp_path, record())
    if kind == 'directory':
        assert list(outside.iterdir()) == []
    else:
        assert outside.read_text() == 'untouched'


def legacy_files(project):
    folder = project / '.duet/logs/jev/2026-10-08'
    folder.mkdir(parents=True)
    rows = []
    for index, ident in enumerate(('aaaaaaaa', 'bbbbbbbb'), 1):
        path = folder / f'101500-00000{index}-{ident}.md'
        content = f'# JEV 판단 기록\n\n## 질문\n\nlegacy question {index}\n\n## 관제 후속 판단\n\nlegacy outcome {index}\n'
        path.write_text(content)
        rows.append((path, content))
    return rows


def test_first_write_migrates_legacy_files_once_in_order(tmp_path):
    old = legacy_files(tmp_path)
    first = write_decision(tmp_path, record('new decision'))
    second = write_decision(tmp_path, record('next decision'))
    assert first.path == second.path
    assert list((tmp_path / '.duet/logs/jev').rglob('*.md')) == [first.path]
    text = first.path.read_text()
    for path, content in old:
        assert not path.exists()
        assert text.count(content) == 1
    assert text.index(old[0][1]) < text.index(old[1][1]) < text.index('new decision') < text.index('next decision')


def test_migration_retry_after_unlink_failure_does_not_duplicate(tmp_path, monkeypatch):
    old = legacy_files(tmp_path)
    unlink = Path.unlink

    def fail_one(path, *args, **kwargs):
        if path == old[0][0]:
            raise PermissionError('legacy file temporarily busy')
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(Path, 'unlink', fail_one)
        try:
            migrate_legacy(tmp_path)
        except OSError:
            pass  # Cleanup may report failure; the persisted copy must remain recoverable.
    assert old[0][0].exists()
    migrate_legacy(tmp_path)
    path = tmp_path / '.duet/logs/jev/DIALOGUE.md'
    text = path.read_text()
    for source, content in old:
        assert not source.exists()
        assert text.count(content) == 1


def test_failed_migration_replacement_preserves_originals(tmp_path, monkeypatch):
    old = legacy_files(tmp_path)
    with patch('duet.core.jev_log.os.replace', side_effect=OSError('temporary write failure')):
        with pytest.raises(OSError):
            migrate_legacy(tmp_path)
    for source, content in old:
        assert source.read_text() == content
    migrate_legacy(tmp_path)
    text = (tmp_path / '.duet/logs/jev/DIALOGUE.md').read_text()
    assert all(text.count(content) == 1 for _, content in old)


def test_controller_startup_migrates_even_when_jev_is_disabled(control):
    old = legacy_files(control.orch.cfg.project)
    control.orch.cfg.settings['jev_mode'] = 'off'
    reloaded = type(control)(control.orch)
    assert reloaded.mode == 'off' and reloaded.calls == 0
    path, = records(control)
    assert path.name == 'DIALOGUE.md'
    text = path.read_text()
    assert all(not source.exists() and text.count(content) == 1 for source, content in old)
