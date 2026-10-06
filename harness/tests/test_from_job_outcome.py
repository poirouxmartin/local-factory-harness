"""from_job_outcome(): what one finished job teaches.

The factory could only ever learn process hygiene -- three hardcoded patterns
about wasted calls and tool errors. Nothing connected a job's outcome to a
durable lesson, so months of runs produced two generic lessons. These pin the
four patterns that carry content: which budget bound, which format failed,
which spec was short a fact, and -- the one that is not a failure -- what
landed first try.
"""
import lessons

NOW = 1000.0


def _att(n, done='stop', evalc=100, result=None, failing=None, cap=16384):
    return {'attempt': n, 'done_reason': done, 'eval_count': evalc,
            'num_predict': cap, 'result': result,
            'failing': [] if failing is None else failing}


def _out(**kw):
    base = {'success': False, 'failure_reason': None, 'edit_mode': 'whole',
            'final_model': 'qwen3.6:35b', 'attempts': []}
    base.update(kw)
    return base


def _by_pattern(found):
    return {l['pattern']: l for l in found}


def test_no_attempts_teaches_nothing():
    assert lessons.from_job_outcome(_out(attempts=[]), NOW) == []
    assert lessons.from_job_outcome(_out(success=True, attempts=[]), NOW) == []


def test_output_wall_counts_truncated_attempts_and_names_the_worst():
    out = _out(attempts=[_att(1, done='length', evalc=4096),
                         _att(2, done='stop', evalc=900),
                         _att(3, done='length', evalc=16384)])
    found = _by_pattern(lessons.from_job_outcome(out, NOW))
    assert 'output_wall' in found
    wall = found['output_wall']
    assert wall['count'] == 2
    assert 'cap' in wall['suggestion']
    assert '16384' in wall['suggestion']
    assert wall['last_seen'] == NOW
    assert wall['scope'] == {'model': 'qwen3.6:35b', 'edit_mode': 'whole'}


def test_a_solved_job_can_still_report_an_output_wall():
    out = _out(success=True, edit_mode='diff',
               attempts=[_att(1, done='length', evalc=4096), _att(2)])
    found = _by_pattern(lessons.from_job_outcome(out, NOW))
    assert 'output_wall' in found
    assert 'no_code_in_diff' not in found
    assert 'underspecified_spec' not in found


def test_every_lesson_has_exactly_the_five_keys():
    out = _out(attempts=[_att(1, done='length')])
    for lesson in lessons.from_job_outcome(out, NOW):
        assert set(lesson) == {'pattern', 'count', 'suggestion', 'last_seen', 'scope'}


def test_no_code_in_diff_only_fires_in_diff_mode():
    diff = _out(failure_reason='no_code', edit_mode='diff',
                final_model='qwen2.5-coder:7b',
                attempts=[_att(1, result='NoCode'), _att(2, result='NoCode')])
    found = _by_pattern(lessons.from_job_outcome(diff, NOW))
    assert found['no_code_in_diff']['count'] == 2
    assert 'SEARCH/REPLACE' in found['no_code_in_diff']['suggestion']
    assert found['no_code_in_diff']['scope'] == {
        'model': 'qwen2.5-coder:7b', 'edit_mode': 'diff'}

    whole = _out(failure_reason='no_code', edit_mode='whole',
                 attempts=[_att(1, result='NoCode')])
    assert 'no_code_in_diff' not in _by_pattern(lessons.from_job_outcome(whole, NOW))


def test_underspecified_spec_fires_on_an_identical_repeated_failure_set():
    out = _out(failure_reason='spec_tests',
               attempts=[_att(1, failing=['a', 'b', 'c']),
                         _att(2, failing=['b', 'a']),
                         _att(3, failing=['a', 'b'])])
    found = _by_pattern(lessons.from_job_outcome(out, NOW))
    assert found['underspecified_spec']['count'] == 1
    assert 'the prompt never carried' in found['underspecified_spec']['suggestion']
    assert found['underspecified_spec']['scope'] == {'model': 'qwen3.6:35b'}


def test_a_shrinking_failure_set_is_progress_not_an_underspecified_spec():
    out = _out(failure_reason='spec_tests',
               attempts=[_att(1, failing=['a', 'b', 'c']),
                         _att(2, failing=['a', 'b']),
                         _att(3, failing=['a'])])
    assert 'underspecified_spec' not in _by_pattern(
        lessons.from_job_outcome(out, NOW))


def test_underspecified_spec_needs_two_attempts_and_a_real_failure_set():
    one = _out(failure_reason='spec_tests', attempts=[_att(1, failing=['a'])])
    assert 'underspecified_spec' not in _by_pattern(
        lessons.from_job_outcome(one, NOW))
    empty = _out(failure_reason='spec_tests',
                 attempts=[_att(1, failing=[]), _att(2, failing=[])])
    assert 'underspecified_spec' not in _by_pattern(
        lessons.from_job_outcome(empty, NOW))


def test_first_attempt_solve_is_recorded_as_a_positive_lesson():
    out = _out(success=True, edit_mode='diff', attempts=[_att(1)])
    found = _by_pattern(lessons.from_job_outcome(out, NOW))
    assert found['first_attempt_solve']['count'] == 1
    assert 'first attempt' in found['first_attempt_solve']['suggestion']
    assert found['first_attempt_solve']['scope'] == {
        'model': 'qwen3.6:35b', 'edit_mode': 'diff'}

    two = _out(success=True, attempts=[_att(1), _att(2)])
    assert 'first_attempt_solve' not in _by_pattern(
        lessons.from_job_outcome(two, NOW))


def test_missing_fields_inside_an_attempt_never_raise():
    out = _out(failure_reason='spec_tests', attempts=[{'attempt': 1}, {'attempt': 2}])
    assert isinstance(lessons.from_job_outcome(out, NOW), list)


def test_at_most_one_lesson_per_pattern():
    out = _out(failure_reason='no_code', edit_mode='diff',
               attempts=[_att(1, done='length', result='NoCode'),
                         _att(2, done='length', result='NoCode')])
    found = lessons.from_job_outcome(out, NOW)
    assert len(found) == len({l['pattern'] for l in found})


def test_the_existing_api_is_untouched():
    assert lessons.MAX_LESSONS == 10
    assert lessons.BUDGET_CHARS == 1500
    for name in ('merge', 'from_audit', 'incident', 'render'):
        assert callable(getattr(lessons, name))


def test_an_identical_failure_set_inside_one_rung_is_not_an_underspecified_spec():
    # The run that shipped this function: the 30b repeated 11/12 twice, then
    # the 35b closed it from the identical prompt. Same set, same rung = that
    # rung is stuck, which is what escalation is for -- not a spec defect.
    out = _out(failure_reason='spec_tests',
               attempts=[dict(_att(1, failing=['a']), stage=2),
                         dict(_att(2, failing=['a']), stage=2)])
    assert 'underspecified_spec' not in _by_pattern(
        lessons.from_job_outcome(out, NOW))


def test_a_failure_set_surviving_an_escalation_is_an_underspecified_spec():
    # A stronger model, clean slate, same prompt, same failures: the fact the
    # tests turn on was never in the prompt.
    out = _out(failure_reason='spec_tests',
               attempts=[dict(_att(1, failing=['a']), stage=2),
                         dict(_att(2, failing=['a']), stage=3)])
    found = _by_pattern(lessons.from_job_outcome(out, NOW))
    assert found['underspecified_spec']['count'] == 1
