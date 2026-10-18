from conftest import run_job, wait_job


def test_missing_preflight_counts_inventory_once_and_distinguishes_local_copies(system):
    engine, api, _ = system
    for index in range(55):
        api.file(f'disk:/file-{index:03}.bin', b'kept', preview=False)
    first = run_job(system, mode='fallback', reports=['json'])
    for index in range(55, 80):
        api.file(f'disk:/file-{index:03}.bin', b'new', preview=False)
    api.media_calls.clear()
    job_id = engine.create({'operation':'missing', 'destination':first['root'], 'reports':['json']})
    ready = wait_job(engine, job_id)
    assert ready['phase'] == 'ready'
    assert ready['files'] == 80 and ready['existing'] == 55
    check = engine.controls[job_id].snapshot()['verification']
    assert check == {'checked':80, 'total':80, 'originals':55, 'previews':0, 'missing':25, 'complete':True,'cached':55,'hashed':0}
    assert api.media_calls == []
    engine.action(job_id, 'start')
    completed = wait_job(engine, job_id)
    assert completed['phase'] == 'completed' and completed['originals'] == 80
    assert sorted(api.media_calls) == [(f'disk:/file-{index:03}.bin', 'original') for index in range(55,80)]
