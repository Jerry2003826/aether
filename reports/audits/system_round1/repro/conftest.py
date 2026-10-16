"""System round 1 audit repros. They assert invariants and are expected to fail on d6ed382.

Kept outside tests/ so CI stays on the product suite. Fixtures come from tests/integration.
"""

from tests.integration.conftest import (  # noqa: F401
    admin_url,
    aether_admin,
    api_db,
    clean_db,
    client,
    role_urls,
)
