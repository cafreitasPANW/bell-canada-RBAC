"""Contract the sync orchestrator expects from a source-control provider client."""

from typing import Any, Dict, List, Optional, Protocol


class SCMClient(Protocol):
    """Implemented by GitLabClient; a future GitHubClient must match it.

    Project dicts need `id` and `path_with_namespace` (`owner/name`).
    Member dicts need `username`, `email` (or `public_email`), `name`, `access_level`.
    """

    def get_active_projects(
        self, top_n: int = 10, last_minutes: Optional[int] = None
    ) -> List[Dict[str, Any]]: ...

    def get_project_members(
        self, project_id: Any, include_inherited: bool = False
    ) -> List[Dict[str, Any]]: ...
