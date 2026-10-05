"""Cortex Cloud API client for GitLab repository and RBAC synchronization."""

import datetime
import hashlib
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

    ASSET_GROUP_TYPE_FIELD = "xdm.asset.type.id"
    ASSET_GROUP_REPOSITORY_NAME_FIELD = "xdm.asset.name"

    READ_ONLY_POST_ENDPOINTS = {
        "/public_api/v1/rbac/get_users",
        "/public_api/v1/rbac/get_roles",
        "/public_api/v1/asset-groups",
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
        component_permissions: Optional[List[str]] = None,
        asset_group_repository_type: str = "GITLAB_REPOSITORY",
        asset_group_name_prefix: str = "gitlab-repo-",
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
        self.component_permissions = list(component_permissions or [])
        self.asset_group_repository_type = asset_group_repository_type
        self.asset_group_name_prefix = asset_group_name_prefix
        self._roles_cache: Optional[list] = None
        self._users_cache: Optional[list] = None
        self._asset_groups_cache: Optional[list] = None

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
            response = None
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
                error_response = getattr(exc, "response", None) or response
                status_code = getattr(error_response, "status_code", None)
                if attempt < 2 and (
                    status_code is None or status_code in transient_statuses
                ):
                    retry_after = error_response.headers.get("Retry-After") if error_response else None
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                    time.sleep(delay)
                    continue
                response_body = ""
                if error_response is not None:
                    response_body = error_response.text[:2000]
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
            repository_aliases = {
                name,
                repository.get("repository"),
                repository.get("url"),
                repository.get("repositoryUrl"),
            }
            repository_aliases.discard(None)
            if sources and repository_aliases.intersection(selected_names):
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
        """Ensure manual repository selection and append new repos one at a time."""
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
        data_source = current.get("data_source", {})
        selection_type = data_source.get("selectionType")
        data_source_endpoint = (
            f"/public_api/appsec/v1/data_source_instances/{quote(cortex_intg_id, safe='')}"
        )
        state = sorted(current.get("selected_state", set()))
        if selection_type != "MANUAL_SELECTION":
            logger.warning(
                "Switching Cortex data source %s from selectionType=%s to MANUAL_SELECTION.",
                cortex_intg_id,
                selection_type,
            )
            self._request("PUT", data_source_endpoint, {
                "selectionType": "MANUAL_SELECTION",
                "state": state.copy(),
            })
        for repository_path in missing:
            if repository_path in state:
                continue
            state.append(repository_path)
            logger.info("Adding repository to manual Cortex selection: %s", repository_path)
            self._request("PUT", data_source_endpoint, {
                "selectionType": "MANUAL_SELECTION",
                "state": state.copy(),
            })
        if missing and not self.dry_run:
            selected = list(self.get_repos(cortex_intg_id).get("integration_repos", []))
        lookup = {}
        for repository in selected:
            name = self._repository_name(repository)
            repository_id = repository.get("id") or repository.get("assetId")
            if name and repository_id:
                lookup[name] = {"id": repository_id, "is_new": name in missing}
        selected_state_after_update = set(state)
        for project_name in project_names:
            if project_name in selected_state_after_update and project_name not in lookup:
                lookup[project_name] = {
                    "id": None,
                    "is_new": project_name in missing,
                }
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
        roles = self._find_role_records(data)
        self._roles_cache = roles if isinstance(roles, list) else []
        return self._roles_cache

    @staticmethod
    def _find_role_records(value: Any) -> list:
        if isinstance(value, list):
            return value if all(isinstance(item, dict) for item in value) else []
        if not isinstance(value, dict):
            return []
        for key in ("roles", "data", "reply"):
            if key in value:
                records = CortexClient._find_role_records(value[key])
                if records:
                    return records
        return []

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
        if not self.component_permissions:
            raise CortexClientError(
                "No Cortex component permissions configured; set them in config/cortex_rbac_config.json"
            )
        payload = {
            "request_data": {
                "pretty_name": role_name,
                "description": description,
                "component_permissions": self.component_permissions,
            }
        }
        response = self._request("POST", "/platform/iam/v1/role", payload)
        data = self._response_data(response)
        self._roles_cache = None
        return self.find_role_id_by_name(role_name) or (data.get("role_id") if isinstance(data, dict) else None)

    def set_user_role(self, user_email: str, role_name: str) -> bool:
        payload = {"request_data": {"user_emails": [user_email], "role_name": role_name}}
        self._request("POST", "/public_api/v1/rbac/set_user_role", payload)
        return True

    def get_user_scope(self, user_email: str) -> dict:
        response = self._request(
            "GET",
            f"/platform/iam/v1/scope/user/{quote(user_email, safe='')}"
        )
        data = self._response_data(response)
        return data if isinstance(data, dict) else {}

    def update_user_asset_scope(self, user_email: str, asset_group_ids: List[int]) -> bool:
        """Restrict a Cortex user to configured SBAC asset groups."""
        if not asset_group_ids:
            logger.warning("No SBAC asset groups configured for %s; scope unchanged.", user_email)
            return False
        current_scope = self.get_user_scope(user_email)
        if isinstance(current_scope.get("scope"), dict):
            current_scope = current_scope["scope"]
        endpoints_scope = current_scope.get("endpoints") or {
            "endpoint_groups": {"mode": "no_scope", "names": []},
            "endpoint_tags": {"mode": "no_scope", "names": []},
        }
        cases_issues_scope = current_scope.get("cases_issues") or {
            "mode": "no_scope",
            "include_cases_issues_empty_entities": False,
            "names": [],
        }
        request_data = {
            "assets": {
                "mode": "scope",
                "asset_group_ids": sorted(set(asset_group_ids)),
            },
            "endpoints": endpoints_scope,
            "cases_issues": cases_issues_scope,
        }
        if "datasets_rows" in current_scope:
            request_data["datasets_rows"] = current_scope["datasets_rows"]
        payload = {
            "request_data": request_data
        }
        self._request(
            "PUT",
            f"/platform/iam/v1/scope/user/{quote(user_email, safe='')}",
            payload,
        )
        return True

    @staticmethod
    def _is_admin_user(user: dict) -> bool:
        role_names = [
            user.get("role_name"),
            user.get("role_pretty_name"),
            user.get("user_role_name"),
            user.get("role") if isinstance(user.get("role"), str) else None,
        ]
        if isinstance(user.get("role"), dict):
            role = user["role"]
            role_names.append(role.get("pretty_name") or role.get("role_name") or role.get("name"))
        roles = user.get("roles", [])
        if isinstance(roles, list):
            role_names.extend(
                role.get("pretty_name") or role.get("role_name") or role.get("name")
                for role in roles if isinstance(role, dict)
            )
            role_names.extend(role for role in roles if isinstance(role, str))
        normalized_names = {
            str(name).strip().lower().replace("_", " ").replace("-", " ")
            for name in role_names if name
        }
        return bool(normalized_names.intersection({"instance administrator", "instance admin"}))

    @staticmethod
    def _asset_group_records(value: Any) -> List[dict]:
        if isinstance(value, list):
            records = []
            for item in value:
                if isinstance(item, dict) and any(
                    key in item for key in (
                        "group_name", "asset_group_name", "name",
                    )
                ):
                    records.append(item)
                else:
                    records.extend(CortexClient._asset_group_records(item))
            return records
        if isinstance(value, dict):
            if any(value.get(key) for key in ("group_name", "asset_group_name")):
                return [value]
            records = []
            for key, nested in value.items():
                if key not in {"metadata", "pagination"}:
                    records.extend(CortexClient._asset_group_records(nested))
            return records
        return []

    def get_asset_groups(self) -> List[dict]:
        if self._asset_groups_cache is not None:
            return self._asset_groups_cache
        all_groups = []
        page_size = 1000
        seen_pages = set()
        for page_number in range(100):
            search_from = page_number * page_size
            response = self._request(
                "POST",
                "/public_api/v1/asset-groups",
                {"request_data": {
                    "search_from": search_from,
                    "search_to": search_from + page_size - 1,
                }},
            )
            body = response.json() if response.text.strip() else {}
            groups = self._asset_group_records(body)
            page_signature = tuple(sorted(
                (
                    str(group.get("group_name") or group.get("asset_group_name") or group.get("name") or ""),
                    str(group.get("group_id") or group.get("asset_group_id") or group.get("id") or ""),
                )
                for group in groups
            ))
            if page_signature and page_signature in seen_pages:
                logger.warning("Cortex asset-group pagination repeated a page; stopping lookup")
                break
            if page_signature:
                seen_pages.add(page_signature)
            all_groups.extend(groups)
            if len(groups) < page_size:
                break
        else:
            logger.warning("Cortex asset-group lookup reached the 100-page safety limit")
        self._asset_groups_cache = all_groups
        return all_groups

    def _repository_asset_group_name(self, repository_path: str) -> str:
        digest = hashlib.sha256(
            f"{self.gitlab_key}:{repository_path}".encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.asset_group_name_prefix}{digest}"

    @staticmethod
    def _existing_asset_group_id(groups: List[dict], group_name: str) -> Optional[int]:
        for group in groups:
            existing_name = (
                group.get("group_name")
                or group.get("asset_group_name")
                or group.get("name")
            )
            if existing_name == group_name:
                group_id = (
                    group.get("group_id")
                    or group.get("asset_group_id")
                    or group.get("id")
                )
                if group_id is not None:
                    return int(group_id)
                raise CortexClientError(
                    f"Cortex asset group {group_name} has no group ID"
                )
        return None

    def ensure_repository_asset_group(self, repository_path: str) -> Optional[int]:
        """Find or create the dynamic SBAC group for one GitLab repository."""
        group_name = self._repository_asset_group_name(repository_path)
        groups = self.get_asset_groups()
        existing_group_id = self._existing_asset_group_id(groups, group_name)
        if existing_group_id is not None:
            return existing_group_id

        if len(groups) >= 1950:
            raise CortexClientError(
                "Cortex asset-group count is near the documented limit; "
                "refusing to create another per-repository group"
            )

        if self.dry_run:
            logger.info(
                "DRY RUN: would create dynamic Cortex asset group %s for repo %s",
                group_name,
                repository_path,
            )
            return None

        payload = {
            "request_data": {
                "asset_group": {
                    "group_name": group_name,
                    "group_type": "Dynamic",
                    "group_description": (
                        f"Managed by GitLab Cortex RBAC sync for {self.gitlab_key}: {repository_path}"
                    ),
                    "membership_predicate": {
                        "AND": [
                            {
                                "SEARCH_FIELD": self.ASSET_GROUP_TYPE_FIELD,
                                "SEARCH_TYPE": "EQ",
                                "SEARCH_VALUE": self.asset_group_repository_type,
                            },
                            {
                                "SEARCH_FIELD": self.ASSET_GROUP_REPOSITORY_NAME_FIELD,
                                "SEARCH_TYPE": "EQ",
                                "SEARCH_VALUE": repository_path,
                            },
                        ]
                    },
                }
            }
        }
        try:
            response = self._request("POST", "/public_api/v1/asset-groups/create", payload)
        except CortexClientError as exc:
            if "already exists" not in str(exc).lower():
                raise
            self._asset_groups_cache = None
            existing_group_id = self._existing_asset_group_id(
                self.get_asset_groups(), group_name
            )
            if existing_group_id is None:
                raise CortexClientError(
                    f"Cortex reported that Asset Group {group_name} already exists, "
                    "but the list API did not return its group ID; check API-key "
                    "permissions and the Asset Groups list response"
                ) from exc
            logger.info("Reusing existing Cortex asset group %s", group_name)
            return existing_group_id
        body = response.json() if response.text.strip() else {}
        group_id = self._find_asset_group_id(body)
        if group_id is None:
            raise CortexClientError(
                f"Cortex did not return an asset group ID for {repository_path}"
            )
        if self._asset_groups_cache is None:
            self._asset_groups_cache = []
        self._asset_groups_cache.append({
            "group_name": group_name,
            "group_id": group_id,
        })
        return group_id

    @staticmethod
    def _find_asset_group_id(value: Any) -> Optional[int]:
        if isinstance(value, dict):
            group_id = value.get("asset_group_id") or value.get("group_id")
            if group_id is not None:
                return int(group_id)
            for nested in value.values():
                found = CortexClient._find_asset_group_id(nested)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = CortexClient._find_asset_group_id(item)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _role_result(reason: str) -> dict:
        return {"success": False, "reason": reason, "role_id": None, "assigned": False}

    def _asset_groups_for_user_repos(self, user_data: dict) -> tuple[List[int], List[str]]:
        group_ids: Set[int] = set()
        unmapped_repos = []
        for repository in user_data.get("repositories", []):
            repo_path = repository.get("repo")
            if not repo_path:
                continue
            group_id = self.ensure_repository_asset_group(repo_path)
            if group_id is None:
                unmapped_repos.append(repo_path)
                continue
            group_ids.add(group_id)
        return sorted(group_ids), sorted(set(unmapped_repos))

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
            result = self._role_result("")
            if not email or email.lower() not in users:
                result["reason"] = "User not found in Cortex"
                results[username] = result
                continue
            if self._is_admin_user(users[email.lower()]):
                result["reason"] = "Administrator users cannot receive automated SBAC scopes"
                results[username] = result
                logger.warning("Skipping %s: %s", username, result["reason"])
                continue
            asset_group_ids, unmapped_repos = self._asset_groups_for_user_repos(user_data)
            if unmapped_repos:
                result["reason"] = (
                    "No Cortex SBAC asset group was resolved for GitLab repositories: "
                    + ", ".join(unmapped_repos)
                )
                results[username] = result
                logger.error("Skipping %s: %s", username, result["reason"])
                continue
            if not asset_group_ids:
                result["reason"] = "No Cortex SBAC asset groups resolved; refusing unscoped role assignment"
                results[username] = result
                logger.error("Skipping %s: %s", username, result["reason"])
                continue
            role_name = self._generate_role_name(email)
            role_id = self.find_role_id_by_name(role_name, roles)
            try:
                if not self.update_user_asset_scope(email, asset_group_ids):
                    result["reason"] = "Cortex user scope was not updated; role assignment skipped"
                    results[username] = result
                    continue
                if not role_id:
                    role_id = self.create_custom_role(role_name, f"GitLab repository access for {email}")
                if not role_id and self.dry_run:
                    role_id = role_name
                self.set_user_role(email, role_name)
                result.update(success=True, role_id=role_id, assigned=True)
            except CortexClientError as exc:
                if "scope cannot be updated for admin entity" in str(exc).lower():
                    result["reason"] = "Administrator users cannot receive automated SBAC scopes"
                    logger.warning("Skipping %s: %s", username, result["reason"])
                else:
                    result["reason"] = str(exc)
            results[username] = result
        return results

    def health_check(self) -> bool:
        response = self._request("GET", "/public_api/v1/healthcheck")
        return response.ok

    def update_scope(self, entity_type: str, entity_id: str, payload: dict) -> None:
        """Update an SBAC scope; payload is tenant-specific and supplied by the caller."""
        self._request("PUT", f"/platform/iam/v1/scope/{quote(entity_type, safe='')}/{quote(entity_id, safe='')}", payload)


