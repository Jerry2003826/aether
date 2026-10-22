"""An artifact id that is not a UUID (a missing id rendered as "None", an empty string, None itself) is
a typed not_found from ArtifactStore.get, decided before any query (r11c, triage category D: the
large_token_rounds flake surfaced as psycopg DataError 'invalid input syntax for type uuid: "None"')."""

from __future__ import annotations

import uuid

import pytest
from aether_storage.artifacts import ArtifactError, ArtifactStore


class _NoDatabase:
    def transaction(self, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("an invalid artifact id must not reach the database")


@pytest.mark.parametrize("artifact_id", ["None", "", "not-a-uuid", None])
def test_a_non_uuid_artifact_id_is_not_found_without_a_query(tmp_path, artifact_id) -> None:
    store = ArtifactStore(tmp_path / "artifacts", _NoDatabase())  # type: ignore[arg-type]
    with pytest.raises(ArtifactError) as failure:
        store.get(owner_id="o", project_id="p", artifact_id=artifact_id)  # type: ignore[arg-type]
    assert failure.value.code == "not_found"


def test_a_uuid_object_is_queried_instead_of_rejected_as_missing(tmp_path) -> None:
    seen: list[object] = []

    class _Result:
        def mappings(self) -> _Result:
            return self

        def first(self) -> None:
            return None

    class _Conn:
        def execute(self, _statement: object, params: dict[str, object]) -> _Result:
            seen.append(params["id"])
            return _Result()

    class _Database:
        def transaction(self, **_kwargs: object) -> _Database:
            return self

        def __enter__(self) -> _Conn:
            return _Conn()

        def __exit__(self, *_args: object) -> bool:
            return False

    artifact_id = uuid.uuid4()
    store = ArtifactStore(tmp_path / "artifacts", _Database())  # type: ignore[arg-type]
    with pytest.raises(ArtifactError) as failure:
        store.get(owner_id="o", project_id="p", artifact_id=artifact_id)
    assert failure.value.code == "not_found"
    assert seen == [artifact_id]
