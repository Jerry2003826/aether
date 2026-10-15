from sqlalchemy import text

from aether_storage.db import Database
from aether_worker.recommendations import POLICY_VERSION, clear_profile, rank_candidates, record_feedback
from tests.integration.test_auth import ORIGIN, _bootstrap


def _session(client, key: str):
    session = client.get("/v1/auth/session")
    if session.status_code != 200:
        _bootstrap(client)
        session = client.get("/v1/auth/session")
    body = session.json()
    headers = {**ORIGIN, "X-CSRF-Token": body["csrf_token"], "Idempotency-Key": key}
    project = client.post("/v1/projects", headers=headers, json={"name": "推荐"})
    assert project.status_code == 201, project.text
    return body["owner_id"], project.json()["id"]


def _source(database: Database, owner_id: str, project_id: str, name: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return str(
            conn.execute(
                text(
                    "INSERT INTO sources (project_id, kind, locator, canonical_locator_hash, approved_config, status) "
                    "VALUES (:project_id, 'note', :locator, :digest, '{}'::jsonb, 'active') RETURNING id::text"
                ),
                {"project_id": project_id, "locator": f"note://{name}", "digest": name},
            ).scalar_one()
        )


def test_feedback_is_deduped_and_exploration_records_probability(client, role_urls) -> None:
    owner_id, project_id = _session(client, "recommend-project")
    database = Database(role_urls["aether_worker"])
    first = _source(database, owner_id, project_id, "alpha")
    second = _source(database, owner_id, project_id, "beta")
    candidates = [
        {"source_id": first, "goal_match": 0.2, "deleted": False},
        {"source_id": second, "goal_match": 0.9, "deleted": False},
    ]
    quiet = rank_candidates(database, owner_id=owner_id, project_id=project_id, candidates=candidates)
    assert quiet["impression_id"]
    assert quiet["policy_version"] == POLICY_VERSION
    assert quiet["explore"] is False
    assert quiet["probabilities"] == {}
    assert quiet["collaborative_filtering"] is False
    assert [item["source_id"] for item in quiet["items"]] == [second, first]
    explored = rank_candidates(database, owner_id=owner_id, project_id=project_id, candidates=candidates, explore=True)
    assert abs(explored["probabilities"][second] - ((1 - 0.05) + (0.05 / 2))) < 1e-12
    assert abs(explored["probabilities"][first] - (0.05 / 2)) < 1e-12
    assert abs(sum(explored["probabilities"].values()) - 1) < 1e-9
    stored = record_feedback(
        database,
        owner_id=owner_id,
        project_id=project_id,
        impression_id=quiet["impression_id"],
        source_id=second,
        kind="helpful",
        event_key="event-1",
    )
    assert stored["recorded"] is True
    assert stored["feedback_id"]
    again = record_feedback(
        database,
        owner_id=owner_id,
        project_id=project_id,
        impression_id=quiet["impression_id"],
        source_id=second,
        kind="helpful",
        event_key="event-1",
    )
    assert again["recorded"] is False
    assert again["reason"] == "duplicate"
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        count = int(
            conn.execute(
                text("SELECT count(*) FROM feedback_events WHERE project_id = :project_id AND event_key = 'event-1'"),
                {"project_id": project_id},
            ).scalar_one()
        )
    assert count == 1
    record_feedback(
        database,
        owner_id=owner_id,
        project_id=project_id,
        impression_id=explored["impression_id"],
        source_id=second,
        kind="hide",
        event_key="hide-1",
    )
    hidden = rank_candidates(
        database,
        owner_id=owner_id,
        project_id=project_id,
        candidates=[{"source_id": first, "goal_match": 0.2, "deleted": False}, {"source_id": second, "goal_match": 0.9, "deleted": True}],
    )
    assert [item["source_id"] for item in hidden["items"]] == [first]
    cleared = clear_profile(database, owner_id=owner_id, project_id=project_id)
    assert cleared["rebuildable"] is True
    rebuilt = rank_candidates(database, owner_id=owner_id, project_id=project_id, candidates=[{"source_id": first, "goal_match": 0.4, "deleted": False}])
    assert rebuilt["items"][0]["reason"] == "当前目标匹配"
    assert rebuilt["collaborative_filtering"] is False
