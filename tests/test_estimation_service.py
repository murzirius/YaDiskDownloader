from disk_downloader.estimation import RemainingTime
import threading

import pytest

from conftest import test_timeout, wait_job
from disk_downloader.api import APIError
from disk_downloader.engine import Control


def estimator():
    moment = [0]
    eta = RemainingTime(lambda: moment[0])
    eta.start('downloading')
    return eta, moment


def test_failed_partial_attempts_never_become_a_transfer_speed():
    eta, moment = estimator()
    for second in (1, 2, 3):
        moment[0] = second
        eta.advance('original', 32768)
    moment[0] = 4
    eta.retry('original')
    assert eta.estimate('downloading', 98304, 0, 500 * 1024**3, 1000) is None
    assert 'повторы' in eta.reason
    for second in (20, 40, 60):
        moment[0] = second
        eta.result(False)
    assert eta.estimate('downloading', 98304, 3, 500 * 1024**3, 997) is None
    assert 'нет удачных' in eta.reason


def test_tiny_previews_do_not_divide_the_size_of_cloud_originals():
    eta, moment = estimator()
    for second in (20, 40, 60):
        moment[0] = second
        eta.retry('original')
        eta.advance('preview', 1024)
        eta.result(True, fallback=True)
    assert eta.estimate('downloading', 3072, 3, 500 * 1024**3, 97, original_only=True) == 1940
    assert eta.basis == 'files'


def test_parallel_file_cycles_include_retries_and_preview_fallback():
    eta, moment = estimator()
    moment[0] = 20
    for _ in range(8):
        eta.retry('original')
        eta.result(True, fallback=True)
    assert eta.estimate('downloading', 0, 8, 10**12, 80) == 200
    # No artificial multiplication by worker count: eight actual completions / 20 s.
    assert eta.basis == 'files'


def test_single_burst_does_not_extrapolate_terabytes_over_many_days():
    eta, moment = estimator()
    moment[0] = 1
    eta.advance('original', 256 * 1024)
    moment[0] = 5
    assert eta.estimate('downloading', 256 * 1024, 0, 10**12, 1000) is None
    assert 'мало данных' in eta.reason


def test_stall_hides_old_numeric_estimate():
    eta, moment = estimator()
    for second in (1, 2, 3):
        moment[0] = second
        eta.advance('original', 32768)
    assert eta.estimate('downloading', 98304, 0, 196608, 1) == 6
    moment[0] = 20
    assert eta.estimate('downloading', 98304, 0, 196608, 1) is None
    assert 'нет передачи' in eta.reason


def test_three_recent_failures_invalidate_an_older_success_rate():
    eta, moment = estimator()
    for second, success in enumerate((True, True, True, False, False, False), 1):
        moment[0] = second * 2
        eta.result(success, fallback=True)
    assert eta.estimate('downloading', 0, 6, 0, 100, 100) is None
    assert 'нет удачных' in eta.reason


def test_reused_results_do_not_inflate_completion_speed():
    eta, moment = estimator()
    moment[0] = 5
    for _ in range(1000):
        eta.result(True, reused=True)
    assert eta.estimate('downloading', 0, 1000, 0, 100, 100) is None
    assert eta.finished == 0


def test_stage_change_for_preview_upgrades_relearns_the_rate():
    eta, moment = estimator()
    for second in (1, 2, 3):
        moment[0] = second
        eta.result(True, fallback=True)
    assert eta.estimate('downloading', 0, 3, 0, 30, 30) == 30
    eta.new_stage()
    moment[0] = 4
    assert eta.estimate('downloading', 0, 3, 0, 30, 30) is None


def test_preview_bytes_cannot_estimate_original_transfer_speed():
    eta, moment = estimator()
    for second in (1, 2, 3):
        moment[0] = second
        eta.advance('preview', 1024**2)
    assert eta.estimate('downloading', 3 * 1024**2, 0, 500 * 1024**3, 1000, original_only=True) is None


def test_byte_sampling_remains_useful_after_thousands_of_chunks():
    eta, moment = estimator()
    for tick in range(10000):
        moment[0] = tick / 1000
        eta.advance('original', 1024)
    assert eta.estimate('downloading', 10000 * 1024, 0, 10000 * 1024, 10) in (10, 11)


def test_fast_completion_sampling_does_not_lose_the_window_baseline():
    eta, moment = estimator()
    for tick in range(10000):
        moment[0] = tick / 1000
        eta.result(True, fallback=True)
    assert eta.estimate('downloading', 0, 10000, 0, 10000, 10000) in (10, 11)


@pytest.mark.parametrize('preview_succeeds', [True, False])
def test_engine_estimate_uses_full_attempt_cycles_instead_of_original_gigabytes(system, monkeypatch, preview_succeeds):
    engine, api, tmp = system
    for index in range(4):
        path = f'disk:/{index}.jpg'
        api.file(path)
        api.tree[path]['size'] = 100 * 1024**3
    job_id = engine.create({'destination': str(tmp / 'results'), 'whole_disk': True,
                            'mode': 'fallback', 'workers': 1, 'attempts': 1, 'create_zip': False})
    assert wait_job(engine, job_id)['phase'] == 'ready'
    moment = [0]
    monkeypatch.setattr(Control, 'statistics_clock', lambda self: moment[0])
    reached, release = threading.Event(), threading.Event()
    original_media = api.media

    def media(path, kind, settings, checkpoint):
        if kind == 'original':
            if path == 'disk:/3.jpg':
                reached.set()
                while not release.wait(0.01):
                    checkpoint()
            moment[0] += 20
            raise APIError('Original attempt failed', 503)
        if not preview_succeeds:
            raise APIError('Preview attempt failed', 503)
        return original_media(path, kind, settings, checkpoint)

    monkeypatch.setattr(api, 'media', media)
    engine.action(job_id, 'start')
    try:
        assert reached.wait(test_timeout(10))
        job = engine.status(job_id)
        assert job['phase'] == 'downloading' and job['processed'] == 3
        if preview_succeeds:
            assert job['eta_seconds'] == 20 and job['eta_basis'] == 'files'
        else:
            assert job['eta_seconds'] is None
            assert 'нет удачных' in job['eta_message']
    finally:
        release.set()
    result = wait_job(engine, job_id)
    assert result['phase'] == ('completed' if preview_succeeds else 'completed_with_errors')
    assert result['eta_seconds'] == 0
