"""An artifact id that is not a UUID (a missing id rendered as "None", an empty string, None itself) is
a typed not_found from ArtifactStore.get, decided before any query (r11c, triage category D: the
large_token_rounds flake surfaced as psycopg DataError 'invalid input syntax for type uuid: "None"')."""

from __future__ import annotations

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
