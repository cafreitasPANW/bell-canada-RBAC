"""Cortex Cloud API client for GitLab repository and RBAC synchronization."""

import datetime
import json
import os
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

import requests

from common.logger import LoggerFactory

logger = LoggerFactory.get_logger(__name__)


class CortexClientError(Exception):
    """Raised when a Cortex Cloud API operation fails."""


class CortexClient:
    """Client for Cortex AppSec data sources and platform RBAC APIs."""

    READ_ONLY_POST_ENDPOINTS = {
        "/public_api/v1/rbac/get_users",
        "/public_api/v1/rbac/get_roles",
    }

    def __init__(
        self,
        api_url: str,
        access_key: str,
        secret_key: str,
        integration_id: str,
        dry_run: bool = True,
        role_name_prefix: str = "devex_",
        default_role_name: str = "",
        gitlab_key: str = "",
    ) -> None:
        required = {
            "api_url": api_url,
            "access_key": access_key,
            "secret_key": secret_key,
            "integration_id": integration_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                f"Missing required Cortex parameters: {', '.join(missing)}"
            )
        self.api_url = api_url.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.integration_id = integration_id
        self.dry_run = dry_run
        self.role_name_prefix = role_name_prefix
        self.default_role_name = default_role_name
        self.gitlab_key = gitlab_key
        self._roles_cache: Optional[list] = None
        self._users_cache: Optional[list] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self.secret_key,
            "x-xdr-auth-id": self.access_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> requests.Response:
        is_mutating = method in {"PUT", "PATCH", "DELETE"} or (
            method == "POST" and endpoint not in self.READ_ONLY_POST_ENDPOINTS
        )
        if self.dry_run and is_mutating:
            logger.info("DRY RUN: Skipping %s request to %s", method, endpoint)
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

        transient_statuses = {429, 500, 502, 503, 504}
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        for attempt in range(3):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=payload,
                    params=params,
                    timeout=120,
                )
                if response.status_code in transient_statuses and attempt < 2:
                    retry_after = response.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                    logger.warning("Transient Cortex response %s; retrying in %ss", response.status_code, delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as exc:
                if attempt < 2 and (
                    not getattr(exc, "response", None)
                    or exc.response.status_code in transient_statuses
                ):
                    time.sleep(2 ** attempt)
                    continue
                response_body = ""
                if getattr(exc, "response", None) is not None:
                    response_body = exc.response.text[:2000]
                detail = f"; response={response_body}" if response_body else ""
                raise CortexClientError(
                    f"{method} {endpoint} failed: {exc}{detail}"
                ) from exc
        raise CortexClientError(f"{method} {endpoint} failed after retries")

    @staticmethod
    def _response_data(response: requests.Response) -> Any:
        if not response.text.strip():
            return {}
        body = response.json()
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    @staticmethod
    def _repository_name(repository: dict) -> str:
        return (
            repository.get("fullRepositoryName")
            or repository.get("repository")
            or repository.get("name")
            or repository.get("url")
            or repository.get("repositoryUrl")
            or ""
        )

    def get_data_sources(self) -> list:
        response = self._request("GET", "/public_api/appsec/v1/data_source_instances")
        data = self._response_data(response)
        return data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []

    def get_repos(self, cortex_intg_id: Optional[str] = None, verify_repos: Optional[List[str]] = None) -> dict:
        """Return Cortex repository assets and repositories selected by the data source."""
        response = self._request("GET", "/public_api/appsec/v1/repositories")
        data = self._response_data(response)
        repositories = data if isinstance(data, list) else data.get("repositories", []) if isinstance(data, dict) else []
        data_source_id = cortex_intg_id or self.integration_id
        sources = [source for source in self.get_data_sources() if source.get("id") == data_source_id]
        selected_names: Set[str] = set()
        if sources:
            selected_names = set(sources[0].get("state") or [])
        integration_repos = []
        for repository in repositories:
            name = self._repository_name(repository)
            if not selected_names or name in selected_names or repository.get("url") in selected_names:
                integration_repos.append(repository)
        if verify_repos:
            missing = set(verify_repos) - {self._repository_name(repo) for repo in integration_repos}
            if missing:
                logger.warning("%s repositories were not found in Cortex", len(missing))
        return {
            "all_repos": repositories,
            "integration_repos": integration_repos,
            "sources": {"cortex": len(repositories)},
            "data_source": sources[0] if sources else {},
            "selected_state": selected_names,
        }

    def activate_missing_repos_integration(self, projects: List[dict], cortex_intg_id: str) -> dict:
        """Add GitLab project paths to an existing Cortex GitLab data source."""
        current = self.get_repos(cortex_intg_id)
        selected = list(current.get("integration_repos", []))
        selected_names = {
            name for name in current.get("selected_state", set()) if name
        }
        repository_names = {self._repository_name(repo) for repo in selected}
        project_names = [project.get("path_with_namespace") for project in projects]
        project_names = [name for name in project_names if name]
        missing = [
            name for name in project_names
            if name not in selected_names and name not in repository_names
        ]
        logger.info("Cortex data source %s has %s selected repositories", cortex_intg_id, len(selected))
        logger.info("Found %s GitLab repositories not selected in Cortex", len(missing))
        if missing:
            data_source = current.get("data_source", {})
            selection_type = data_source.get("selectionType")
            if selection_type != "MANUAL_SELECTION":
                logger.warning(
                    "Cortex data source %s has selectionType=%s; "
                    "leaving its discovery configuration unchanged and skipping manual repository update.",
                    cortex_intg_id,
                    selection_type,
                )
                missing = []
            else:
                payload = {"selectionType": "MANUAL_SELECTION", "state": sorted(selected_names | set(missing))}
                self._request("PUT", f"/public_api/appsec/v1/data_source_instances/{quote(cortex_intg_id, safe='')}", payload)
                if not self.dry_run:
                    selected = list(self.get_repos(cortex_intg_id).get("integration_repos", []))
        lookup = {}
        for repository in selected:
            name = self._repository_name(repository)
            repository_id = repository.get("id") or repository.get("assetId")
            if name and repository_id:
                lookup[name] = {"id": repository_id, "is_new": name in missing}
        return lookup

    def get_all_users(self) -> list:
        response = self._request("POST", "/public_api/v1/rbac/get_users", {"request_data": {}})
        data = self._response_data(response)
        users = self._find_user_records(data)
        if not users:
            response_keys = sorted(data.keys()) if isinstance(data, dict) else []
            logger.warning(
                "Cortex get_users returned no user records (response keys: %s)",
                response_keys,
            )
        return users

    @staticmethod
    def _find_user_records(value: Any) -> list:
        """Extract user records from known and wrapped Cortex response shapes."""
        if isinstance(value, list):
            return value if all(isinstance(item, dict) for item in value) else []
        if not isinstance(value, dict):
            return []
        for key in ("users", "user_list", "data", "records", "results", "reply"):
            if key in value:
                records = CortexClient._find_user_records(value[key])
                if records:
                    return records
        return []

    def fetch_cortex_users_lookup(self, active_only: bool = True) -> dict:
        """Return Cortex users keyed by normalized email address."""
        users = self.get_all_users()
        total_users = len(users)
        if active_only:
            users = [user for user in users if self._is_active_user(user)]
        users_by_email = {
            user.get("email", user.get("user_email", "")).lower(): user
            for user in users
            if user.get("email", user.get("user_email"))
        }
        logger.info(
            "Cortex users: %s returned, %s active, %s with email addresses",
            total_users,
            len(users) if active_only else total_users,
            len(users_by_email),
        )
        return users_by_email

    @staticmethod
    def _is_active_user(user: dict) -> bool:
        enabled = user.get("enabled")
        if isinstance(enabled, bool):
            return enabled
        if isinstance(enabled, str):
            return enabled.strip().lower() in {"true", "active", "enabled"}
        status = user.get("status") or user.get("user_status") or "ACTIVE"
        return str(status).strip().lower() in {"active", "enabled", "true"}

    def get_custom_roles(self, role_names: Optional[List[str]] = None) -> list:
        if self._roles_cache is not None:
            return self._roles_cache
        payload = {"request_data": {"role_names": role_names or []}}
        response = self._request("POST", "/public_api/v1/rbac/get_roles", payload)
        data = self._response_data(response)
        roles = data.get("roles", data.get("data", [])) if isinstance(data, dict) else data
        self._roles_cache = roles if isinstance(roles, list) else []
        return self._roles_cache

    @staticmethod
    def _role_name(role: dict) -> str:
        return role.get("pretty_name") or role.get("name") or role.get("role_name") or ""

    @staticmethod
    def _role_id(role: dict) -> str:
        return role.get("role_id") or role.get("id") or ""

    def find_role_id_by_name(self, role_name: str, cached_roles: Optional[list] = None) -> Optional[str]:
        for role in cached_roles if cached_roles is not None else self.get_custom_roles([role_name]):
            if self._role_name(role).lower() == role_name.lower():
                return self._role_id(role)
        return None

    def _generate_role_name(self, email: str) -> str:
        return f"{self.role_name_prefix}{email.split('@')[0]}"

    def create_custom_role(self, role_name: str, description: str) -> Optional[str]:
        permissions = [item.strip() for item in os.getenv("CORTEX_ROLE_COMPONENT_PERMISSIONS", "appsec.repositories.view").split(",") if item.strip()]
        payload = {"request_data": {"pretty_name": role_name, "description": description, "component_permissions": permissions}}
        response = self._request("POST", "/platform/iam/v1/role", payload)
        data = self._response_data(response)
        self._roles_cache = None
        return self.find_role_id_by_name(role_name) or (data.get("role_id") if isinstance(data, dict) else None)

    def set_user_role(self, user_email: str, role_name: str) -> bool:
        payload = {"request_data": {"user_emails": [user_email], "role_name": role_name}}
        self._request("POST", "/public_api/v1/rbac/set_user_role", payload)
        return True

    def create_or_update_user_roles(self, user_repos: dict) -> dict:
        results = {}
        if not user_repos:
            logger.info("No GitLab users matched enabled Cortex users; skipping role lookup.")
            return results
        users = self.fetch_cortex_users_lookup()
        role_names = [
            self._generate_role_name(user_data.get("email", ""))
            for user_data in user_repos.values()
            if user_data.get("email")
        ]
        roles = self.get_custom_roles(role_names)
        for username, user_data in user_repos.items():
            email = user_data.get("email", "")
            result = {"success": False, "reason": None, "role_id": None, "assigned": False}
            if not email or email.lower() not in users:
                result["reason"] = "User not found in Cortex"
                results[username] = result
                continue
            role_name = self._generate_role_name(email)
            role_id = self.find_role_id_by_name(role_name, roles)
            try:
                if not role_id:
                    role_id = self.create_custom_role(role_name, f"GitLab repository access for {email}")
                if not role_id and self.dry_run:
                    role_id = role_name
                self.set_user_role(email, role_name)
                result.update(success=True, role_id=role_id, assigned=True)
            except CortexClientError as exc:
                result["reason"] = str(exc)
            results[username] = result
        return results

    def health_check(self) -> bool:
        response = self._request("GET", "/public_api/v1/healthcheck")
        return response.ok

    def update_scope(self, entity_type: str, entity_id: str, payload: dict) -> None:
        """Update an SBAC scope; payload is tenant-specific and supplied by the caller."""
        self._request("PUT", f"/platform/iam/v1/scope/{quote(entity_type, safe='')}/{quote(entity_id, safe='')}", payload)


