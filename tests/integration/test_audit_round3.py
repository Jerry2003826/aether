"""Round-3 regressions on real PostgreSQL.

No remote provider is called. Assertions keep the audit's required behavior.
"""
from __future__ import annotations
import json
import os
import threading
import uuid
import pytest
from sqlalchemy import text

from aether_domain.security import sha256_text
from aether_worker.goal_effect import _bind_task_artifact, _ensure_contract, _set_status

from aether_storage.artifacts import ArtifactError, ArtifactStore
from aether_storage.db import Database
from aether_worker.goal_effect import (
    WorkerEndpoints, materialize_goal_effect, prepare_task_effect,
    execute_task_effect, _ensure_contract, _ensure_verification,
)
from aether_worker.outbox import enqueue_task
from tests.integration.goal_approval import record_owner_approval
from tests.integration.test_artifacts import _project

FACT = '实验设备每九十天完成一次保养。'

@pytest.fixture
def ctx(client, api_db, role_urls):
    owner, project, headers = _project(client)
    worker = Database(role_urls['aether_worker'])
    root = client.app.state.settings.artifact_root
    store = ArtifactStore(root, worker)
    endpoints = WorkerEndpoints(
        worker_url=role_urls['aether_worker'], verifier_url=role_urls['aether_verifier'],
        dispatcher_url=role_urls['aether_dispatcher'], artifact_root=root,
        temporal_address='127.0.0.1:9', task_queue='round3-direct-functions',
        research_provider='scripted',
    )
    return owner, project, worker, store, endpoints, client, headers


def scope(ctx):
    return dict(owner_id=ctx[0], project_id=ctx[1])


def task(ctx, goal):
    created = enqueue_task(ctx[2], **scope(ctx), goal=goal, command_key=str(uuid.uuid4()))
    record_owner_approval(ctx[2], owner_id=ctx[0], project_id=ctx[1], task_id=created['task_id'], workflow_id=created['workflow_id'])
    return dict(project_id=ctx[1], task_id=created['task_id'], command_id=created['command_key'])


def read_one(ctx, sql, **params):
    with ctx[2].transaction(**scope(ctx)) as conn:
        return dict(conn.execute(text(sql), params).mappings().one())


@pytest.mark.parametrize('mutation', ['corrupt_answer', 'delete_source'])
def test_round3_cached_pass_is_not_current_validation(ctx, mutation):
    _owner, _project_id, worker, store, endpoints = ctx[:5]
    source = store.put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    goal = '请摘录设备保养周期。'
    command = task(ctx, goal)
    result = materialize_goal_effect(endpoints, command)
    assert result['status'] == 'SUCCEEDED'
    contract_id, digest = _ensure_contract(worker, **scope(ctx), task_id=command['task_id'], goal=goal)
    aid = result['artifact_id']
    row = read_one(ctx, 'SELECT sha256 FROM artifacts WHERE project_id=:project AND id=:id', project=ctx[1], id=aid)
    if mutation == 'corrupt_answer':
        (store.root / ctx[1] / row['sha256'][:2] / row['sha256']).write_bytes(b'damaged after first PASS')
    else:
        store.tombstone(**scope(ctx), artifact_id=str(source['artifact_id']))
    vid = _ensure_verification(endpoints, **scope(ctx), task_id=command['task_id'],
        contract_id=contract_id, contract_sha=digest, artifact_id=aid, goal=goal)
    evidence = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=vid)
    assert evidence['verdict'] != 'PASS', 'historical PASS overrode failed current observation'


def test_round3_succeeded_answer_deletion_is_refused(ctx):
    _owner, project_id, worker, store, endpoints = ctx[:5]
    store.put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    goal = '请摘录设备保养周期。'
    command = task(ctx, goal)
    result = materialize_goal_effect(endpoints, command)
    assert result['status'] == 'SUCCEEDED'
    aid = result['artifact_id']
    with pytest.raises(ArtifactError) as caught:
        store.tombstone(**scope(ctx), artifact_id=aid)
    assert caught.value.code == 'evidence_retained'
    state = read_one(ctx, 'SELECT status FROM tasks WHERE project_id=:project AND id=:id', project=project_id, id=command['task_id'])
    assert state['status'] == 'SUCCEEDED'
    contract_id, digest = _ensure_contract(worker, **scope(ctx), task_id=command['task_id'], goal=goal)
    vid = _ensure_verification(endpoints, **scope(ctx), task_id=command['task_id'],
        contract_id=contract_id, contract_sha=digest, artifact_id=aid, goal=goal)
    evidence = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=project_id, id=vid)
    assert evidence['verdict'] == 'PASS'


def test_round3_success_requires_actual_goal_satisfaction(ctx):
    ctx[3].put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    command = task(ctx, '计算 17 × 23，只返回整数。')
    result = materialize_goal_effect(ctx[4], command)
    state = read_one(ctx, 'SELECT status FROM tasks WHERE project_id=:project AND id=:id', project=ctx[1], id=command['task_id'])
    if state['status'] == 'SUCCEEDED':
        data, _ = ctx[3].get(**scope(ctx), artifact_id=result['artifact_id'])
        assert data.decode().strip() == '391', 'unrelated excerpt is not a completed arithmetic task'


def test_round3_verifier_rejects_wrong_contract_hash(ctx):
    ctx[3].put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    goal = '请摘录设备保养周期。'
    command = task(ctx, goal)
    result = materialize_goal_effect(ctx[4], command)
    cid, digest = _ensure_contract(ctx[2], **scope(ctx), task_id=command['task_id'], goal=goal)
    wrong = 'f' * 64
    assert wrong != digest
    vid = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'],
        contract_id=cid, contract_sha=wrong, artifact_id=result['artifact_id'], goal=goal)
    evidence = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=vid)
    assert evidence['verdict'] != 'PASS', 'unobserved caller hash accepted as contract evidence'


def test_round3_repeated_tasks_have_own_answer_reference(ctx):
    ctx[3].put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    first = task(ctx, '请摘录设备保养周期。')
    second = task(ctx, '再告诉我一次设备保养周期。')
    a = materialize_goal_effect(ctx[4], first)
    b = materialize_goal_effect(ctx[4], second)
    assert a['status'] == b['status'] == 'SUCCEEDED'
    assert a['artifact_id'] == b['artifact_id']
    with ctx[2].transaction(**scope(ctx)) as conn:
        owner_task = conn.execute(text("SELECT task_id::text FROM artifacts WHERE project_id=:project AND id=:id"), dict(project=ctx[1], id=a['artifact_id'])).scalar_one()
        updated = conn.execute(text("SELECT count(*) FROM artifacts WHERE project_id=:project AND id=:id AND task_id=:task"), dict(project=ctx[1], id=b['artifact_id'], task=second['task_id'])).scalar_one()
        links = conn.execute(text("SELECT task_id::text FROM task_artifacts WHERE project_id=:project AND artifact_id=:id AND classification='task_answer' AND deleted_at IS NULL ORDER BY created_at"), dict(project=ctx[1], id=a['artifact_id'])).scalars().all()
    assert owner_task == first['task_id']
    assert updated == 0
    assert links == [first['task_id'], second['task_id']] or set(links) == {first['task_id'], second['task_id']} and len(links) == 2
    detail = ctx[5].get(f"/v1/projects/{ctx[1]}/tasks/{second['task_id']}", headers=ctx[6])
    assert detail.status_code == 200, detail.text
    assert b['artifact_id'] in detail.json()['artifact_ids']


def test_round3_paused_task_does_not_execute(ctx):
    ctx[3].put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    command = task(ctx, '请摘录设备保养周期。')
    prepare_task_effect(ctx[4], command)
    with ctx[2].transaction(**scope(ctx)) as conn:
        conn.execute(text("UPDATE tasks SET status='PAUSED' WHERE project_id=:project AND id=:id"), dict(project=ctx[1],id=command['task_id']))
    result = execute_task_effect(ctx[4], command)
    with ctx[2].transaction(**scope(ctx)) as conn:
        count = conn.execute(text("SELECT count(*) FROM artifacts WHERE project_id=:project AND task_id=:task AND classification='task_answer'"), dict(project=ctx[1],task=command['task_id'])).scalar_one()
    assert count == 0 and result['status'] == 'PAUSED'


def torn_tail(ctx):
    store = ctx[3]
    staged = store.stage_without_commit(project_id=ctx[1], data=b'valid recoverable prefix', media_type='text/plain')
    path = store.root / 'journal' / f'{ctx[1]}.jsonl'
    with path.open('ab') as handle:
        handle.write(b'{"state":"prepared","attempt_id":')
        handle.flush(); os.fsync(handle.fileno())
    return staged, path


def test_round3_append_after_crash_preserves_journal_recovery(ctx):
    staged, _ = torn_tail(ctx)
    ctx[3].put(**scope(ctx), data=b'normal next upload', media_type='text/plain')
    assert staged['artifact_id'] in ctx[3].recover(**scope(ctx))


def test_round3_repair_second_crash_preserves_complete_prefix(ctx, monkeypatch):
    staged, _ = torn_tail(ctx)
    original = ctx[3]._write_all
    def crash(fd, payload):
        if bytes(payload).endswith(b'\n') and str(staged['artifact_id']).encode() in bytes(payload):
            raise OSError('audit: second crash during prefix rewrite')
        return original(fd, payload)
    monkeypatch.setattr(ctx[3], '_write_all', crash)
    with pytest.raises(OSError, match='second crash'):
        ctx[3]._journal_rows(ctx[1])
    monkeypatch.setattr(ctx[3], '_write_all', original)
    assert staged['artifact_id'] in ctx[3].recover(**scope(ctx))


def test_round3_requested_limit_is_not_replaced_by_an_unrelated_excerpt(ctx):
    ctx[3].put(**scope(ctx), data=(FACT + '\n接口请求上限为每分钟六十次。\n').encode(), media_type='text/plain')
    goal = '根据资料，接口每分钟最多允许多少次请求？只回答请求限额。'
    command = task(ctx, goal)
    result = materialize_goal_effect(ctx[4], command)
    answer = ''
    if result.get('status') == 'SUCCEEDED':
        data, _media = ctx[3].get(**scope(ctx), artifact_id=result['artifact_id'])
        answer = data.decode()
    assert result['status'] != 'SUCCEEDED' or '六十' in answer or '60' in answer


def test_round3_repeat_and_restored_observation_keep_history(ctx):
    ctx[3].put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    goal = '请摘录设备保养周期。'
    command = task(ctx, goal)
    result = materialize_goal_effect(ctx[4], command)
    assert result['status'] == 'SUCCEEDED'
    contract_id, digest = _ensure_contract(ctx[2], **scope(ctx), task_id=command['task_id'], goal=goal)
    original, _media = ctx[3].get(**scope(ctx), artifact_id=result['artifact_id'])
    again = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'], contract_id=contract_id, contract_sha=digest, artifact_id=result['artifact_id'], goal=goal)
    first = read_one(ctx, "SELECT id::text AS id FROM verification_results WHERE project_id=:project AND task_id=:task AND verdict='PASS'", project=ctx[1], task=command['task_id'])
    assert again == first['id']
    row = read_one(ctx, 'SELECT sha256 FROM artifacts WHERE project_id=:project AND id=:id', project=ctx[1], id=result['artifact_id'])
    path = ctx[3].root / ctx[1] / row['sha256'][:2] / row['sha256']
    path.write_bytes(b'damaged after pass')
    failed = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'], contract_id=contract_id, contract_sha=digest, artifact_id=result['artifact_id'], goal=goal)
    failed_row = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=failed)
    assert failed != first['id'] and failed_row['verdict'] != 'PASS'
    path.write_bytes(original)
    restored = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'], contract_id=contract_id, contract_sha=digest, artifact_id=result['artifact_id'], goal=goal)
    restored_row = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=restored)
    assert restored == first['id'] and restored_row['verdict'] == 'PASS'
    kept = read_one(ctx, 'SELECT count(*) AS count FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=failed)
    assert kept['count'] == 1


def test_round3_current_contract_is_not_the_earliest_version(ctx):
    ctx[3].put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    goal = '请摘录设备保养周期。'
    command = task(ctx, goal)
    result = materialize_goal_effect(ctx[4], command)
    assert result['status'] == 'SUCCEEDED'
    current_id, current_digest = _ensure_contract(ctx[2], **scope(ctx), task_id=command['task_id'], goal=goal)
    newer = '{"criterion_id":"goal_artifact","revision":2}'
    newer_digest = sha256_text(newer)
    with ctx[2].transaction(**scope(ctx)) as conn:
        conn.execute(text("UPDATE tasks SET current_contract_id = NULL WHERE project_id=:project AND id=:id"), dict(project=ctx[1], id=command['task_id']))
        newer_id = conn.execute(
            text(
                "INSERT INTO goal_contracts (project_id, task_id, contract_version, content, contract_sha256, approved_by, approved_at) "
                "VALUES (:project, :task, 2, CAST(:content AS jsonb), :digest, :owner, now()) RETURNING id::text"
            ),
            dict(project=ctx[1], task=command['task_id'], content=newer, digest=newer_digest, owner=ctx[0]),
        ).scalar_one()
    stale = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'], contract_id=current_id, contract_sha=current_digest, artifact_id=result['artifact_id'], goal=goal)
    stale_row = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=stale)
    assert stale_row['verdict'] != 'PASS'
    fresh = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'], contract_id=str(newer_id), contract_sha=newer_digest, artifact_id=result['artifact_id'], goal=goal)
    fresh_row = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=fresh)
    assert fresh_row['verdict'] == 'PASS'
    with ctx[2].transaction(**scope(ctx)) as conn:
        conn.execute(text("UPDATE tasks SET current_contract_id=:contract WHERE project_id=:project AND id=:id"), dict(contract=current_id, project=ctx[1], id=command['task_id']))
    ignored = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'], contract_id=str(newer_id), contract_sha=newer_digest, artifact_id=result['artifact_id'], goal=goal)
    ignored_row = read_one(ctx, 'SELECT verdict FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=ignored)
    assert ignored_row['verdict'] != 'PASS'


def test_round3_wrong_fact_report_separates_integrity_from_goal(ctx):
    ctx[3].put(**scope(ctx), data=(FACT + '\n接口请求上限为每分钟六十次。\n').encode(), media_type='text/plain')
    goal = '根据资料，接口每分钟最多允许多少次请求？只回答请求限额。'
    command = task(ctx, goal)
    contract_id, digest = _ensure_contract(ctx[2], **scope(ctx), task_id=command['task_id'], goal=goal, approved_by=ctx[0])
    wrong = ctx[3].put(**scope(ctx), data=f'资料记载：{FACT}'.encode(), media_type='text/plain', classification='task_answer')
    vid = _ensure_verification(ctx[4], **scope(ctx), task_id=command['task_id'], contract_id=contract_id, contract_sha=digest, artifact_id=str(wrong['artifact_id']), goal=goal)
    row = read_one(ctx, 'SELECT verdict, report_artifact_id::text AS report_id FROM verification_results WHERE project_id=:project AND id=:id', project=ctx[1], id=vid)
    assert row['verdict'] != 'PASS'
    report, _media = ctx[3].get(**scope(ctx), artifact_id=row['report_id'])
    body = json.loads(report.decode())
    assert body['pipeline'] == 'pass'
    assert body['byte_integrity'] == 'pass'
    assert body['goal_completion'] != 'pass'
    assert body['offline'] is True
    assert body['model_mode'] == 'scripted'


def test_round3_links_do_not_overwrite_or_cross_projects(ctx, client):
    blob = ctx[3].put(**scope(ctx), data=b'shared-classification-bytes', media_type='text/plain')
    first = task(ctx, '第一处引用')
    second = task(ctx, '第二处引用')
    owned, inserted = _bind_task_artifact(ctx[2], owner_id=ctx[0], project_id=ctx[1], task_id=first['task_id'], artifact_id=str(blob['artifact_id']), classification='task_answer')
    again, linked = _bind_task_artifact(ctx[2], owner_id=ctx[0], project_id=ctx[1], task_id=second['task_id'], artifact_id=str(blob['artifact_id']), classification='task_answer')
    other_class, other_insert = _bind_task_artifact(ctx[2], owner_id=ctx[0], project_id=ctx[1], task_id=first['task_id'], artifact_id=str(blob['artifact_id']), classification='task_excerpt')
    assert (owned, inserted) == (1, 1)
    assert (again, linked) == (0, 1)
    assert (other_class, other_insert) == (0, 1)
    stored = read_one(ctx, 'SELECT classification FROM artifacts WHERE project_id=:project AND id=:id', project=ctx[1], id=blob['artifact_id'])
    assert stored['classification'] == 'project_private'
    other = client.post('/v1/projects', headers={**ctx[6], 'Idempotency-Key': 'round3-other-project'}, json={'name': '另一个项目'})
    assert other.status_code == 201, other.text
    other_id = other.json()['id']
    other_task = enqueue_task(ctx[2], owner_id=ctx[0], project_id=other_id, goal='隔离', command_key=str(uuid.uuid4()))
    other_blob = ctx[3].put(owner_id=ctx[0], project_id=other_id, data=b'shared-classification-bytes', media_type='text/plain')
    assert other_blob['artifact_id'] != blob['artifact_id']
    _bind_task_artifact(ctx[2], owner_id=ctx[0], project_id=other_id, task_id=other_task['task_id'], artifact_id=str(other_blob['artifact_id']), classification='task_answer')
    with ctx[2].transaction(owner_id=ctx[0], project_id=ctx[1]) as conn:
        visible = conn.execute(text('SELECT artifact_id::text FROM task_artifacts WHERE deleted_at IS NULL')).scalars().all()
    assert str(other_blob['artifact_id']) not in visible
    assert str(blob['artifact_id']) in visible
    with ctx[2].transaction(**scope(ctx)) as conn:
        conn.execute(text("UPDATE task_artifacts SET deleted_at = now() WHERE project_id=:project AND task_id=:task AND classification='task_answer'"), dict(project=ctx[1], task=second['task_id']))
    hidden = ctx[5].get(f"/v1/projects/{ctx[1]}/tasks/{second['task_id']}", headers=ctx[6])
    shown = ctx[5].get(f"/v1/projects/{ctx[1]}/tasks/{first['task_id']}", headers=ctx[6])
    assert hidden.status_code == 200 and shown.status_code == 200
    assert str(blob['artifact_id']) not in hidden.json()['artifact_ids']
    assert str(blob['artifact_id']) in shown.json()['artifact_ids']
    errors: list[BaseException] = []

    def bind_again() -> None:
        try:
            _bind_task_artifact(ctx[2], owner_id=ctx[0], project_id=ctx[1], task_id=first['task_id'], artifact_id=str(blob['artifact_id']), classification='task_answer')
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    threads = [threading.Thread(target=bind_again) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with ctx[2].transaction(**scope(ctx)) as conn:
        count = conn.execute(text("SELECT count(*) FROM task_artifacts WHERE project_id=:project AND task_id=:task AND artifact_id=:id AND classification='task_answer' AND deleted_at IS NULL"), dict(project=ctx[1], task=first['task_id'], id=blob['artifact_id'])).scalar_one()
    assert count == 1


def test_round3_status_update_returns_the_database_row(ctx):
    ctx[3].put(**scope(ctx), data=FACT.encode(), media_type='text/plain')
    command = task(ctx, '请摘录设备保养周期。')
    prepare_task_effect(ctx[4], command)
    with ctx[2].transaction(**scope(ctx)) as conn:
        conn.execute(text("UPDATE tasks SET status='PAUSED' WHERE project_id=:project AND id=:id"), dict(project=ctx[1], id=command['task_id']))
    actual = _set_status(ctx[2], owner_id=ctx[0], project_id=ctx[1], task_id=command['task_id'], status='SUCCEEDED', allowed=('VERIFYING',))
    state = read_one(ctx, 'SELECT status FROM tasks WHERE project_id=:project AND id=:id', project=ctx[1], id=command['task_id'])
    assert actual == 'PAUSED' and state['status'] == 'PAUSED'
