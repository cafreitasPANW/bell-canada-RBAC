"""Prisma Cloud API client for managing cloud security resources.

Provides functionality for managing repositories, users, and roles in Prisma Cloud
Code Security through the Prisma Cloud API. Handles authentication via token-based
authorization with proactive token renewal to prevent expiration during long operations.

Core Features:
- Repository management: Fetch, activate, and manage code repositories in integrations
- User management: Retrieve and update user profiles and role assignments
- Role management: Create, read, update, and delete custom roles with repository access
- Batch processing: Handle large operations through batched API calls (100 repos per batch)
- Error handling: Transient error retry logic with exponential backoff (3 retries)
- Token management: Proactive renewal before expiry to ensure uninterrupted operations

Key Classes:
- PrismaClient: Main API client for all operations
- RoleConfig: Dataclass for role configuration with repository access control
- PrismaClientError: Custom exception for API-related errors

Token Renewal:
- Automatically renews tokens 8 minutes before expiry (configurable)
- Threads safe token caching reduces redundant authentication calls
- Proactive renewal before each batch prevents mid-operation failures

Retry Strategy:
- Handles transient errors: 500, 502, 503, 504, 429 status codes
- Implements exponential backoff: base_delay * (2 ^ attempt)
- Respects Retry-After headers when provided
- Specific exception handling for RequestException, Timeout, ConnectionError
"""

import csv
import os
import sys
from typing import Any, Optional, List, Dict, Set
from dataclasses import dataclass, field

import requests
import datetime
import time
import uuid

from common.logger import LoggerFactory
from common.utils import create_new_repos_list

logger = LoggerFactory.get_logger(__name__)


class PrismaClientError(Exception):
    """Custom exception for PrismaClient errors."""
    pass

@dataclass
class RoleConfig:
    """Configuration for Prisma Cloud role creation and updates."""
    name: str
    description: str
    role_type: str = "DEVELOPER"
    account_group_ids: Optional[List[str]] = None
    code_repository_ids: Optional[List[str]] = None
    resource_list_ids: Optional[List[str]] = None
    restrict_dismissal_access: bool = False
    additional_attributes: Optional[Dict[str, Any]] = None
    repo_id_to_url: Optional[Dict[str, str]] = None


class PrismaClient:
    """Prisma Cloud API client for managing users, roles, and repositories."""
    
    def _build_role_payload(
        self,
        name: str,
        description: str,
        role_type: str,
        account_group_ids: Optional[List[str]],
        code_repository_ids: Optional[List[str]],
        resource_list_ids: Optional[List[str]],
        restrict_dismissal_access: bool,
        additional_attributes: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Helper to build the payload for role creation, including validation and transformation.
        """
        payload = {
            "name": name,
            "description": description,
            "roleType": role_type,
            "restrictDismissalAccess": restrict_dismissal_access
        }
        if account_group_ids:
            payload["accountGroupIds"] = account_group_ids
        if code_repository_ids:
            # Validate that code_repository_ids contains only strings (IDs), not objects
            invalid_items = [item for item in code_repository_ids if not isinstance(item, str)]
            if invalid_items:
                logger.warning(
                    f"code_repository_ids contains {len(invalid_items)} non-string items. "
                    f"Extracting 'id' field from objects: {type(invalid_items[0])}"
                )
                code_repository_ids = [item['id'] if isinstance(item, dict) else item for item in code_repository_ids]
            payload["codeRepositoryIds"] = code_repository_ids
        if resource_list_ids:
            payload["resourceListIds"] = resource_list_ids
        if additional_attributes:
            payload.update(additional_attributes)
        return payload

    def _calculate_retry_delay(self, base_delay: float, attempt: int, retry_after: Optional[str]) -> float:
        """
        Helper to calculate retry delay, using Retry-After header if present and valid,
        otherwise exponential backoff.
        """
        retry_delay = base_delay * (2 ** attempt)
        if retry_after:
            try:
                retry_delay = int(retry_after)
            except Exception:
                pass
        return retry_delay

    def _sanitize_headers_for_logging(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Return headers safe for debug logging by masking auth tokens."""
        sanitized_headers = dict(headers)
        auth_header = sanitized_headers.get("x-redlock-auth")
        if auth_header:
            sanitized_headers["x-redlock-auth"] = "***masked***"
        return sanitized_headers

    def _send_request(
        self,
        method: str,
        url: str,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        verify: Optional[bool] = None,
    ) -> requests.Response:
        """Send an HTTP request with the given method, url, and payload."""
        headers = headers or self._get_headers()
        verify = self._get_verify_cert() if verify is None else verify
        if method == "GET":
            return requests.get(url, headers=headers, verify=verify, timeout=60)
        elif method == "POST":
            return requests.post(url, headers=headers, json=payload, verify=verify, timeout=120)
        elif method == "PUT":
            return requests.put(url, headers=headers, json=payload, verify=verify, timeout=120)
        elif method == "PATCH":
            return requests.patch(url, headers=headers, json=payload, verify=verify, timeout=120)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

    def _mock_success_response(self) -> requests.Response:
        """Return a mock successful response for DRY RUN mode."""
        from requests.models import Response
        mock_response = Response()
        mock_response.status_code = 200
        mock_response._content = b'{}'
        return mock_response

    def __init__(
        self,
        api_url: str,
        access_key: str,
        secret_key: str,
        integration_id: str,
        dry_run: bool = True,
        token_renewal_minutes: int = 8,
        role_name_prefix: str = 'devex_',
        ssdlc_developer_base_role_name: str = 'ssdlc_developer_base',
        appsec_ssdlc_sa_dev_role_name: str = 'appsec-ssdlc-sa-dev',
        gitlab_key: str = '',
    ) -> None:
        """Initialize Prisma Cloud client.
        
        Args:
            api_url: Prisma Cloud API URL (required)
            access_key: Access key for authentication (required)
            secret_key: Secret key for authentication (required)
            integration_id: Integration ID for repository operations (required)
            dry_run: If True, disables POST/PUT updates (default: True)
            token_renewal_minutes: Minutes before token expiry to proactively renew (default: 8)
            role_name_prefix: Prefix used when generating per-user devex roles (default: "devex_")
            ssdlc_developer_base_role_name: Base default role name to replace when assigning user roles
            appsec_ssdlc_sa_dev_role_name: Shared CI/CD role name to update with integration repositories
            gitlab_key: GitLab config key used for naming backup files (default: "")
        
        Raises:
            ValueError: If any required parameter is missing
        """
        if not all([api_url, access_key, secret_key, integration_id]):
            missing = []
            if not api_url:
                missing.append('api_url')
            if not access_key:
                missing.append('access_key')
            if not secret_key:
                missing.append('secret_key')
            if not integration_id:
                missing.append('integration_id')
            error_msg = (
                f"Missing required Prisma Cloud parameters: "
                f"{', '.join(missing)}"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        self.api_url = api_url
        self.access_key = access_key
        self.secret_key = secret_key
        self.integration_id = integration_id
        self.dry_run = dry_run
        self.token_renewal_minutes = token_renewal_minutes
        self.role_name_prefix = role_name_prefix
        self.ssdlc_developer_base_role_name = ssdlc_developer_base_role_name
        self.appsec_ssdlc_sa_dev_role_name = appsec_ssdlc_sa_dev_role_name
        self.gitlab_key = gitlab_key or ""
        self._cached_token = None
        self._token_issue_time = None
        # Integration cache: {integration_id: integration_object}
        self._integration_cache = None
        # Roles cache: list of role objects from /user/role
        self._custom_roles_cache = None
        # Role lookup cache: {lowercase_role_name: role_id}
        self._role_name_to_id_cache = {}
        # Role details cache: {role_id: role_detail_object}
        self._role_details_cache = {}

    def _invalidate_role_caches(self) -> None:
        """Invalidate role list and role-name lookup caches."""
        self._custom_roles_cache = None
        self._role_name_to_id_cache = {}

    def _invalidate_role_detail_cache(self, role_id: str) -> None:
        """Invalidate cached role details for a specific role ID."""
        if role_id in self._role_details_cache:
            del self._role_details_cache[role_id]

    def _rebuild_role_name_cache(self, roles: List[dict]) -> None:
        """Rebuild role-name lookup cache from a roles list."""
        self._role_name_to_id_cache = {
            role.get('name', '').lower(): role.get('id')
            for role in roles
            if role.get('name') and role.get('id')
        }

    def _load_integration_cache(self):
        """Load all integrations into a cache for quick lookup by integration_id."""
        if self._integration_cache is not None:
            return  # Already loaded
        try:
            response = self.make_request("GET", "/code/api/v1/integrations")
            response_data = response.json()
            if isinstance(response_data, list):
                integrations = response_data
            elif isinstance(response_data, dict):
                integrations = response_data.get("integrations") or response_data.get("data") or []
            else:
                integrations = []
            self._integration_cache = {i.get("id"): i for i in integrations if i.get("id")}
            logger.debug(f"Loaded {len(self._integration_cache)} integrations into cache.")
        except Exception as e:
            logger.error(f"Failed to load integration cache: {e}")
            self._integration_cache = {}

    def get_integration_type(self, integration_id: str) -> str:
        """Get the integrationType for a given integration_id using the cache."""
        self._load_integration_cache()
        integration = self._integration_cache.get(integration_id)
        if integration:
            return integration.get("type") or integration.get("integrationType", "")
        logger.warning(f"Integration ID {integration_id} not found in cache.")
        return ""

    # ========================================================================
    # Helper Methods
    # ========================================================================
    def _get_headers(self, request_id: Optional[str] = None) -> dict[str, str]:
        """Get authentication headers for Prisma Cloud API."""
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-redlock-auth': self._get_auth_token_proactive()
        }
        if request_id:
            headers['x-redlock-request-id'] = request_id
        return headers

    def _get_auth_token_proactive(self) -> str:
        """
        Get authentication token, proactively renewing if needed.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        # If no token or token is too old, renew
        if (
            self._cached_token is None or
            self._token_issue_time is None or
            (now - self._token_issue_time).total_seconds() > self.token_renewal_minutes * 60
        ):
            token = self._get_auth_token()
            self._cached_token = token
            self._token_issue_time = now
            logger.debug(f"Proactively renewed Prisma token at {now.isoformat()} (interval: {self.token_renewal_minutes} min)")
        return self._cached_token

    def _get_auth_token(self) -> str:
        """Get authentication token from Prisma Cloud."""
        try:
            url = f"{self.api_url}/login"
            payload = {
                "username": self.access_key,
                "password": self.secret_key
            }
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
            token = response.json().get('token')
            logger.debug("Successfully authenticated with Prisma Cloud")
            return token
        except Exception as e:
            logger.error(f"Failed to authenticate with Prisma Cloud: {e}")
            raise

    def _get_verify_cert(self) -> bool:
        """Get certificate verification setting."""
        # Return True to verify SSL certificates (can be configured later if needed)
        return True

    def _generate_role_name(self, user_email: str) -> str:
        """Generate standardized role name from user email.
        
        Args:
            user_email: User email address
        
        Returns:
            str: Role name in format '{role_name_prefix}<username>'
        
        Example:
            >>> client._generate_role_name("john.doe@example.com")
            'devex_john.doe'
        """
        username = user_email.split("@")[0]
        return f"{self.role_name_prefix}{username}"

    def _build_integration_repos_payload(self, repo_paths: List[str]) -> Dict[str, Any]:
        """Build payload for activating repositories in a Prisma integration.
        
        Args:
            repo_paths: List of repository paths (e.g., ['namespace/project1', 'namespace/project2'])
        
        Returns:
            dict: Formatted payload with params structure
        
        Example:
            >>> client._build_integration_repos_payload(['org/repo1', 'org/repo2'])
            {
                'params': {
                    'reposSelectionType': 'manualRepos',
                    'repositories': ['org/repo1', 'org/repo2']
                }
            }
        """
        return {
            "params": {
                "reposSelectionType": "manualRepos",
                "repositories": repo_paths
            }
        }

    def make_request(self, method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> requests.Response:
        """
        Make an authenticated HTTP request to Prisma Cloud API with retry logic for transient errors.

        Handles authentication, header creation, and request execution for GET, POST, and PUT methods.
        Retries up to 3 times on 504 Gateway Timeout or connection errors.

        Args:
            method (str): HTTP method ('GET', 'POST', 'PUT').
            endpoint (str): API endpoint path (without base URL).
            payload (dict, optional): Request body for POST/PUT requests.

        Returns:
            requests.Response: Response object from the API call if successful.

        Raises:
            PrismaClientError: If request fails after retries or an unsupported method is used.
        """
        max_retries = 3
        base_retry_delay = 2  # seconds
        attempt = 0
        transient_statuses = {500, 502, 503, 504, 429}
        # Construct URL once
        url = (
            self.api_url + endpoint if endpoint.startswith("/")
            else self.api_url + "/" + endpoint
        )
        request_id = str(uuid.uuid4())
        # Early return for DRY RUN
        if self.dry_run and method in ("POST", "PUT", "PATCH"):
            logger.info(f"DRY RUN: Skipping {method} request to {endpoint}. No changes will be made.")
            return self._mock_success_response()
        while attempt < max_retries:
            logger.debug(
                f"Making {method} request to Prisma Cloud API endpoint: {endpoint} (attempt {attempt+1}/{max_retries})"
            )
            try:
                headers = self._get_headers(request_id=request_id)
                verify = self._get_verify_cert()
                logger.debug(
                    f"Request headers ({method} {endpoint}, attempt {attempt+1}/{max_retries}): "
                    f"{self._sanitize_headers_for_logging(headers)}"
                )
                logger.debug(f"Request payload ({method} {endpoint}): {payload}")
                response = self._send_request(method, url, payload, headers=headers, verify=verify)
                logger.debug(f"Response headers ({method} {endpoint}): {dict(response.headers)}")
                logger.debug(f"Response payload ({method} {endpoint}): {response.text}")
                response.raise_for_status()
                return response
            except Exception as e:
                handled = False
                if isinstance(e, requests.exceptions.RequestException):
                    handled = self._handle_transient_error(e, attempt, max_retries, base_retry_delay, transient_statuses)
                if handled:
                    attempt += 1
                    continue
                logger.error(f"{method} {endpoint} Error: {e}")
                raise PrismaClientError(f"{method} {endpoint} Request failed: {e}") from e
        # If we get here, all retries failed
        logger.error(f"{method} {endpoint} Request failed after {max_retries} attempts!")
        raise PrismaClientError(f"{method} {endpoint} Request failed after {max_retries} attempts!")
    
    def _handle_transient_error(
        self,
        exception: Exception,
        attempt: int,
        max_retries: int,
        base_retry_delay: float,
        transient_statuses: Set[int]
    ) -> bool:
        """
        Helper to handle transient errors: logs, sleeps, and returns True if handled (should retry), else False.
        """
        status_code = exception.response.status_code if hasattr(exception, 'response') and exception.response is not None else None
        retry_after = exception.response.headers.get('Retry-After') if hasattr(exception, 'response') and exception.response is not None else None
        if self._is_transient_error(exception, status_code, transient_statuses):
            retry_delay = self._calculate_retry_delay(base_retry_delay, attempt, retry_after)
            status_text = f"HTTP {status_code}" if status_code is not None else "HTTP N/A"
            logger.warning(
                f"Transient error ({status_text}, attempt {attempt+1}/{max_retries}): "
                f"{exception}. Retrying after {retry_delay} seconds..."
            )
            time.sleep(retry_delay)
            return True
        return False

    def _is_transient_error(self, exception: Exception, status_code: Optional[int], transient_statuses: Set[int]) -> bool:
        """
        Helper to determine if an exception or status code is a transient error.
        """
        if status_code in transient_statuses:
            return True
        if isinstance(exception, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        return False

    # ========================================================================
    # User Management Methods
    # ========================================================================
    def get_all_users(self) -> list:
        """Get all users from Prisma Cloud.
        
        Returns:
            list: List of user objects, empty list on error
        """
        logger.debug('Fetching all users...')
        try:
            response = self.make_request("GET", "/v2/user")
        except PrismaClientError as e:
            logger.error(f"Error fetching users: {e}")
            return []
        response.raise_for_status()
        users = response.json()
        logger.debug(f"Found {len(users)} users")
        return users
    
    def get_user_profile(self, email: str) -> Optional[dict]:
        """Get user profile details by email.
        
        API Reference: https://pan.dev/prisma-cloud/api/cspm/get-user-profile-v-2/ 
        
        Args:
            email (str): User email address
        
        Returns:
            dict: User profile details, None if error
        """
        logger.debug(f"Fetching user profile for '{email}'...")
        try:
            response = self.make_request("GET", f"/v2/user/{email}")
        except PrismaClientError as e:
            logger.error(f"Failed to get user profile for '{email}': {e}")
            return None
        response.raise_for_status()
        user_data = response.json()
        logger.debug(
            f"Retrieved user '{email}' with "
            f"{len(user_data.get('roleIds', []))} roles"
        )
        return user_data
    
    def update_user_roles(self, email: str, role_ids: List[str], default_role_id: Optional[str] = None) -> Optional[dict]:
        """Update user profile with assigned roles.
        
        Preserves existing profile data, modifying only role assignments.
        Uses first role_id as default if default_role_id not provided.
        
        Args:
            email: User email address (required)
            role_ids: List of role UUIDs to assign (required)
            default_role_id: Default role UUID (optional, defaults to first in role_ids)
        
        Returns:
            Updated user profile dict, or None on failure
        
        API Reference: https://pan.dev/prisma-cloud/api/cspm/update-profile-v-2/ 
        """
        if not email or not role_ids:
            logger.error("Email and role_ids are required")
            return None
        # Get current user profile to preserve existing data
        user_profile = self.get_user_profile(email)
        if not user_profile:
            logger.error(
                f"Cannot update roles: User '{email}' not found or "
                f"unable to retrieve profile"
            )
            return None
        # Set default role ID
        if not default_role_id:
            default_role_id = role_ids[0] if role_ids else None
        if not default_role_id:
            logger.error("default_role_id is required but not provided")
            return None
        # Build the payload (preserve required fields from existing profile)
        payload = {
            "email": email,
            "firstName": user_profile.get('firstName', 'Unknown'),
            "lastName": user_profile.get('lastName', 'User'),
            "roleIds": role_ids,
            "defaultRoleId": default_role_id,
            "timeZone": user_profile.get('timeZone', 'America/Toronto'),
            "accessKeysAllowed": user_profile.get(
                'accessKeysAllowed', False
            )
        }
        logger.debug(
            f"Update user '{email}' with "
            f"{len(role_ids)} role(s)"
        )
        try:
            response = self.make_request(
                "PUT", f"/v2/user/{email}", payload=payload
            )
        except PrismaClientError as e:
            logger.error(f"Failed to update user '{email}': {e}")
            return None
        response.raise_for_status()
        logger.debug(
            f"Successfully updated user '{email}' with "
            f"{len(role_ids)} role(s)"
        )
        # Handle empty response body gracefully
        if not response.text.strip():
            logger.debug("User update successful, but response body is empty. Returning minimal success info.")
            return {
                "email": email,
                "firstName": payload['firstName'],
                "lastName": payload['lastName'],
                "roleIds": role_ids,
                "defaultRoleId": default_role_id,
                "timeZone": payload['timeZone'],
                "accessKeysAllowed": payload['accessKeysAllowed']
            }
        return response.json()
    
    # ========================================================================
    # Repository Management Methods
    # ========================================================================
    
    def get_repos(self, prisma_intg_id: Optional[str] = None, verify_repos: Optional[List[str]] = None) -> dict:
        """Query all onboarded repositories from Prisma Cloud Code Security.
        
        Fetches all repositories and:
        1. Shows total count across all integrations
        2. Filters for non-CLI repositories only
        3. Breaks down counts by source type (CLI, gitlabEnterprise, etc.)
        4. Optionally verifies that specific repos are present in GitLab
           Enterprise
        
        Args:
            verify_repos (list, optional): List of repository names to verify
                are onboarded. If provided, will check and log results.
        
        Returns:
            dict: Dictionary containing 'all_repos', 'non_cli_repos', and
                'sources' breakdown
        """
        try:
            logger.info("Fetching all repositories from Prisma Cloud...")
            response = self.make_request("GET", "/code/api/v2/repositories")
        except PrismaClientError as e:
            logger.error(f"Error fetching repositories: {e}")
            raise PrismaClientError(f"Error fetching repositories: {e}") from e
        
        data = response.json().get("repositories", [])
        if not data:
            logger.info("No repositories found.")
            return {"all_repos": [], "integration_repos": [], "sources": {}}

        # Filter for repos that belong to the specified integration ID
        integration_repos = []
        if prisma_intg_id:
            integration_repos = [
                item for item in data
                if prisma_intg_id in item.get("integrationIds", [])
            ]

        logger.info(
            f"Total repos found across all integrations: {len(data)}"
        )
        logger.debug(f"Repos for integration {prisma_intg_id}: {len(integration_repos)}")
        # Break down by source to show where all repos are coming from
        sources = {}
        sources_unique_intgids = {}
        for item in data:
            source = item.get("source", "unknown")
            sources[source] = sources.get(source, 0) + 1
            # Track unique integration IDs for each source
            intg_ids = item.get("integrationIds", [])
            if intg_ids:
                if source not in sources_unique_intgids:
                    sources_unique_intgids[source] = set()
                sources_unique_intgids[source].update(intg_ids)

        logger.debug("Breakdown by source:")
        for source in sorted(sources.keys()):
            total = sources[source]
            unique_intgids = sources_unique_intgids.get(source, set())
            logger.debug(f"   {source}: {total} (unique integrationIds: {len(unique_intgids)})")

        # Verify repos if list provided
        if verify_repos:
            repo_name_list = [
                item.get("repository") for item in integration_repos
            ]
            intersection_list = list(
                set(repo_name_list).intersection(set(verify_repos))
            )
            logger.debug(
                f"Repositories to verify found in non-CLI repos: "
                f"{len(intersection_list)} / {len(verify_repos)}"
            )

            if len(verify_repos) == len(intersection_list):
                logger.debug(
                    "All repos successfully found in non-CLI "
                    "integration"
                )
            else:
                missing = len(verify_repos) - len(intersection_list)
                logger.warning(
                    f"{missing} repositories not found in GitLab "
                    f"Enterprise integration"
                )
                missing_repos = set(verify_repos) - set(intersection_list)
                logger.debug(f"Missing repos: {missing_repos}")

        return {
            "all_repos": data,
            "integration_repos": integration_repos,
            "sources": sources
        }

    def _save_repo_fullnames_to_csv(self, intg_repos: List[dict]) -> None:
        """Helper method to save repository full names to a CSV file for backup."""
        output_csv_path = f"prisma_bkup_repos_{self.gitlab_key}.csv"
        try:
            with open(output_csv_path, mode='w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                for repo in intg_repos:
                    repo_name = repo.get('fullRepositoryName') or repo.get('repository')
                    repo_id = repo.get('id', '')
                    repo_email = repo.get('email', '')
                    if repo_name:
                        writer.writerow([repo_name, repo_id, repo_email])
            logger.info(f"Repository paths saved to {output_csv_path}")
        except Exception as e:
            logger.error(f"Failed to save repository paths to CSV: {e}")

    def _update_appsec_ssdlc_sa_dev_role(self, intg_repos: list) -> None:
        """
        Update the appsec-ssdlc-sa-dev role to include all repository IDs from the integration.
        Args:
            intg_repos (list): List of repository dicts from the integration
        """
        appsec_role_id = self.find_role_id_by_name(self.appsec_ssdlc_sa_dev_role_name)
        if appsec_role_id:
            all_repo_ids = [repo['id'] for repo in intg_repos if repo.get('id')]
            appsec_role_details = self.get_role_details(appsec_role_id)
            if appsec_role_details:
                existing_repo_ids = set(appsec_role_details.get('codeRepositoryIds', []))
                desired_repo_ids = set(all_repo_ids)
                if existing_repo_ids == desired_repo_ids:
                    logger.info(
                        f"Skipping update for role '{self.appsec_ssdlc_sa_dev_role_name}' "
                        f"(already associated with {len(existing_repo_ids)} repositories)."
                    )
                    return
                config = RoleConfig(
                    name=appsec_role_details.get('name'),
                    description=appsec_role_details.get('description'),
                    role_type=appsec_role_details.get('roleType', 'DEVELOPER'),
                    code_repository_ids=all_repo_ids,
                    repo_id_to_url=None
                )
                self.update_custom_role(appsec_role_id, config)
            else:
                logger.warning(f"Could not fetch details for '{self.appsec_ssdlc_sa_dev_role_name}' role.")
        else:
            logger.warning(f"Role '{self.appsec_ssdlc_sa_dev_role_name}' not found. Skipping update.")

    def _poll_for_activated_repos(
        self,
        prisma_intg_id: str,
        expected_new_repo_fullnames: List[str],
        poll_interval_seconds: int = 5,
        poll_max_seconds: int = 60,
    ) -> List[dict]:
        """Poll Prisma integration repos until expected repos appear or timeout."""
        poll_elapsed = 0
        expected_new = set(expected_new_repo_fullnames)

        logger.info(
            f"Polling for {len(expected_new)} newly activated repos "
            f"(max {poll_max_seconds}s, every {poll_interval_seconds}s)..."
        )

        intg_repos = []
        while poll_elapsed < poll_max_seconds:
            time.sleep(poll_interval_seconds)
            poll_elapsed += poll_interval_seconds
            prisma_repos_current_data = self.get_repos(prisma_intg_id)
            intg_repos = list(prisma_repos_current_data.get('integration_repos', []))
            activated_paths = {
                repo.get('fullRepositoryName') or repo.get('repository')
                for repo in intg_repos
            }
            still_missing = expected_new - activated_paths
            if not still_missing:
                logger.info(
                    f"All {len(expected_new)} new repos confirmed in Prisma after "
                    f"{poll_elapsed}s. Proceeding with role update."
                )
                break
            logger.debug(
                f"Still waiting ({poll_elapsed}s elapsed): "
                f"{len(still_missing)} repo(s) not yet visible: {still_missing}"
            )
        else:
            logger.warning(
                f"Timed out after {poll_max_seconds}s waiting for repos to appear. "
                f"Proceeding with {len(intg_repos)} repos — role update may be incomplete."
            )

        return intg_repos

    def activate_missing_repos_integration(self, projects: List[dict], prisma_intg_id: str) -> dict:
        """
        Ensure all GitLab projects are onboarded in the specified Prisma Cloud integration.

        - Fetches the current list of onboarded repositories for the integration from Prisma Cloud.
        - Compares the provided GitLab projects to the onboarded repos, identifying any missing ones.
        - Onboards any missing repositories by sending their full names to Prisma Cloud using the correct API payload.
        - Re-fetches the updated list of onboarded repositories after onboarding.
        - Returns a lookup dictionary mapping repository full names (or short names) to their Prisma repo IDs.

        Args:
            projects (list): List of GitLab project dictionaries, each with 'path_with_namespace'.
            prisma_intg_id (str): Prisma Cloud integration ID.

        Returns:
            dict: Mapping of repository full name (or short name) to Prisma repo ID for all onboarded repos in the integration.
        """
        try:
            logger.info(
                f"Activating repositories in Prisma Cloud integration - "
                f"{prisma_intg_id}..."
            )

            prisma_repos_data = self.get_repos(prisma_intg_id)
            intg_repos = list(prisma_repos_data.get('integration_repos', []))
            logger.info(
                f"Prisma integration {prisma_intg_id} currently has {len(intg_repos)} repositories "
                f"activated"
            )

            all_appended_repos = create_new_repos_list(projects, intg_repos)
            logger.info(
                f"Found {len(all_appended_repos) - len(intg_repos)} repositories in GitLab not yet "
                f"onboarded in Prisma Cloud"
            )

            # Add missing repositories to Prisma Cloud integration
            if len(all_appended_repos) > len(intg_repos):
                logger.debug(
                    f"Activating {len(all_appended_repos) - len(intg_repos)} missing repositories in "
                    f"Prisma Cloud integration '{prisma_intg_id}'..."
                )
                #Send only new repos
                repo_fullnames = [
                    repo['fullRepositoryName']
                    for repo in all_appended_repos
                    if repo.get('fullRepositoryName') and repo.get('is_new')
                ]
                # Create a backup file of all the repo paths before updating the integration and save it as an output csv
                self._save_repo_fullnames_to_csv(intg_repos)
                # Update the integration with the new repos
                activation_result = self.update_repos_integration(repo_fullnames, prisma_intg_id)

                # Fail if no repos were successfully activated when there were repos to activate
                if repo_fullnames and not activation_result.get('successful'):
                    failed_repos = activation_result.get('failed', [])
                    error_msg = (
                        f"Failed to activate any repositories in Prisma Cloud integration '{prisma_intg_id}'. "
                        f"Attempted to activate {len(repo_fullnames)} repos, all failed: {failed_repos}"
                    )
                    logger.error(error_msg)
                    raise PrismaClientError(error_msg)

                if self.dry_run:
                    logger.info(
                        "DRY RUN: Skipping repository activation polling because no "
                        "repositories are actually activated."
                    )
                else:
                    # Poll until all newly activated repos appear in GET response
                    # (eventual consistency: POST succeeds but GET may return stale data immediately)
                    intg_repos = self._poll_for_activated_repos(
                        prisma_intg_id=prisma_intg_id,
                        expected_new_repo_fullnames=repo_fullnames,
                    )

                logger.debug(
                    f"Prisma integration {prisma_intg_id} now has {len(intg_repos)} repositories "
                    f"activated"
                )
                # Update appsec-ssdlc-sa-dev role with all repo IDs
                logger.debug(
                    f"About to run _update_appsec_ssdlc_sa_dev_role for integration "
                    f"'{prisma_intg_id}' with {len(intg_repos)} repos."
                )
                self._update_appsec_ssdlc_sa_dev_role(intg_repos)
                logger.debug(
                    f"Completed _update_appsec_ssdlc_sa_dev_role for integration "
                    f"'{prisma_intg_id}'."
                )
            else:
                logger.info("No new repositories to activate in Prisma Cloud integration.")
            # Build a lookup for is_new from all_appended_repos
            is_new_lookup = {
                repo.get('fullRepositoryName') or repo.get('repository'): repo.get('is_new', False)
                for repo in all_appended_repos
                if repo.get('fullRepositoryName') or repo.get('repository')
            }

            # Create lookup: repo_path -> {id, is_new}
            prisma_repo_lookup = {
                (repo.get('fullRepositoryName') or repo.get('repository')): {
                    'id': repo['id'],
                    'is_new': is_new_lookup.get(repo.get('fullRepositoryName') or repo.get('repository'), False)
                }
                for repo in intg_repos
                if (repo.get('fullRepositoryName') or repo.get('repository')) and repo.get('id')
            }
            logger.debug(
                f"Created Prisma repo lookup with {len(prisma_repo_lookup)} "
                f"repository mappings (with is_new flag)"
            )
            return prisma_repo_lookup
        except Exception as e:
            logger.error(f"Exception in activate_missing_repos_integration: {e}")
            raise PrismaClientError(f"Error in activate_missing_repos_integration: {e}") from e

    def update_repos_integration(self, repo_paths: List[str], integration_id: Optional[str] = None) -> dict:
        """Add multiple repositories to a Prisma Cloud integration in one call POST.
        
        Args:
            repo_paths (list): List of repository paths
                (e.g., ['namespace/project1', 'namespace/project2'])
            integration_id (str, optional): Integration ID. Uses
                self.integration_id if not provided.
                {
                    "integrationId": "<id>",
                    "integrationType": "gitlab",
                    "repositoriesNames": ["<names of repositories to integrate>"]
                }       
        Returns:
            dict: Results with 'successful' and 'failed' lists
        """
        integration_id = integration_id or self.integration_id       
        if not repo_paths:
            logger.warning("No repositories to add")
            return {'successful': [], 'failed': []}

        successful = []
        failed = []
        endpoint = "code/api/v2/repositories"
        # Proactive token renewal before each batch
        now = datetime.datetime.now(datetime.timezone.utc)
        if (
            self._token_issue_time is None or
            (now - self._token_issue_time).total_seconds() > self.token_renewal_minutes * 60
            ):
            logger.debug(f"Token age exceeded {self.token_renewal_minutes} min before start, renewing token.")
            self._cached_token = None  # Force renewal on next request
        # Build payload format for POST request to update integration with new repos
        #  gitlab, gitlabEnterprise
        # Use integration cache to get integrationType
        integration_type = self.get_integration_type(integration_id)
        if not integration_type:
            integration_type = "Gitlab"  # fallback for backward compatibility
        payload = {
            "integrationId": integration_id,
            "integrationType": integration_type,
            "repositoriesNames": repo_paths
        }
        try:
            logger.debug(f"Sending request with {len(repo_paths)} repos to {endpoint}: {payload}")
            response = self.make_request("POST", endpoint, payload)
            if response is not None:
                response.raise_for_status()
            logger.debug(f"Successfully added {len(repo_paths)} repositories")
            successful.extend(repo_paths)
        except Exception as e:
            logger.error(f"Failed to add repositories: {e}")
            failed.extend(repo_paths)

        return {'successful': successful, 'failed': failed}

    def fetch_prisma_users_lookup(self, active_only: bool = True) -> dict:
        """Fetch Prisma users and create email-based lookup dictionary.
        
        Args:
            prisma: PrismaClient instance
        
        Args:
            active_only: If True, include only users where enabled=True

        Returns:
            dict: Dictionary mapping lowercase email to user profile
        """
        logger.info("Fetching Prisma Cloud Application Security users...")
        prisma_users = self.get_all_users()
        if prisma_users:
            logger.info(f"Total users found: {len(prisma_users)}")

        if active_only:
            prisma_users = [
                user for user in prisma_users
                if user.get('enabled', False)
            ]
            logger.info(
                f"Enabled Prisma users found: {len(prisma_users)}"
            )

        prisma_users_by_email = {
            user.get('email').lower(): user
            for user in prisma_users
            if user.get('email')
        }
        logger.debug(f"Users with email addresses: {len(prisma_users_by_email)}")
        return prisma_users_by_email

    def log_user_repositories(self, user_data: dict) -> None:
        """Log repository details for a user.
        
        Args:
            user_data: Dictionary containing user information and repositories
        """
        for repo_info in user_data['repositories']:
            prisma_id = repo_info.get('prisma_repo_id')
            repo_path = repo_info.get('repo')
            access_level = repo_info.get('access_level', 20)
            status = (
                f"Prisma ID: {prisma_id}" if prisma_id 
                else "Not found in Prisma"
            )
            logger.debug(
                f"    - {repo_path} (Access: {access_level}) ({status})"
            )

    def create_or_update_role(
        self,
        username: str,
        user_email: str,
        repo_id_list: List[str],
        repo_id_to_url: Optional[dict] = None,
        cached_roles: Optional[list] = None
    ) -> Optional[str]:
        """Create or update a Prisma role with repositories.
        
        Args:
            username: GitLab username
            user_email: User email address
            repo_id_list: List of Prisma repository IDs
            repo_id_to_url: Optional mapping of repo IDs to URLs for logging
            cached_roles: Pre-fetched roles list to avoid redundant API calls
        
        Returns:
            str: Role ID if successful, None otherwise
        """
        role_name = self._generate_role_name(user_email)
        role_id = self.find_role_id_by_name(role_name, cached_roles=cached_roles)
        
        if role_id:
            existing_role = self.get_role_details(role_id)
            
            if not existing_role:
                logger.error(
                    f"Failed to fetch role details for '{role_name}' "
                    f"(ID: {role_id})"
                )
                return None
            
            # Extract current repo list and merge
            existing_repos = set(existing_role.get('codeRepositoryIds', []))
            merged_repos = list(existing_repos.union(set(repo_id_list)))
            
            logger.debug(
                f"  Existing repos: {len(existing_repos)}, "
                f"New repos: {len(repo_id_list)}, "
                f"Merged: {len(merged_repos)}"
            )

            if set(merged_repos) == existing_repos:
                logger.info(
                    f"Role '{role_name}' already contains all requested repositories "
                    f"({len(existing_repos)} total). Skipping update."
                )
                return role_id
            
            # Update the role with merged repositories
            try:
                config = RoleConfig(
                    name=existing_role.get('name'),
                    description=existing_role.get('description'),
                    role_type=existing_role.get('roleType', 'DEVELOPER'),
                    code_repository_ids=merged_repos,
                    repo_id_to_url=repo_id_to_url
                )
                update_response = self.update_custom_role(role_id, config)
                
                if update_response:
                    logger.info(
                        f"Updated role '{role_name}' with {len(merged_repos)} repos"
                    )
                return role_id
            except Exception as e:
                logger.error(f"Failed to update role '{role_name}': {e}")
                return None
        else:
            # Create new role with user's repositories
            try:
                config = RoleConfig(
                    name=role_name,
                    description=f"Role for user {username} with email {user_email}",
                    role_type="DEVELOPER",
                    code_repository_ids=repo_id_list if repo_id_list else [],
                    repo_id_to_url=repo_id_to_url
                )
                create_custom_role_response = self.create_custom_role(config)
                if (create_custom_role_response and 
                        create_custom_role_response.get('id')):
                    role_id = create_custom_role_response['id']
                    logger.info(
                        f"Created new role '{role_name}' with ID '{role_id}' "
                        f"and {len(repo_id_list)} repos"
                    )
                    return role_id
            except Exception as e:
                logger.error(
                    f"Failed to create role '{role_name}' for user "
                    f"'{username}': {e}"
                )
                return None

    def assign_role_to_user(self, role_id: str, role_name: str, user_email: str, user_profile: dict) -> bool:
        """Assign a role to a Prisma user.
        
        Args:
            role_id: Role ID to assign
            role_name: Role name for logging
            user_email: User email address
            user_profile: User profile dictionary with current roles
        
        Returns:
            bool: True if successful, False otherwise
        """
        existing_user_roles = user_profile.get('roleIds', [])
        
        # Add new role to user's existing roles (avoid duplicates)
        updated_role_ids = list(set(existing_user_roles + [role_id]))
        
        # Set default role (use new role if no default, else keep existing)
        default_role_id = user_profile.get('defaultRoleId') if user_profile.get('defaultRoleId') else role_id

        # Check if the default role is the UUID for 'ssdlc_developer_base', and if so, switch to the new/updated devex_ role
        ssdlc_developer_base_role_id = self.find_role_id_by_name(self.ssdlc_developer_base_role_name)
        if ssdlc_developer_base_role_id and default_role_id == ssdlc_developer_base_role_id:
            default_role_id = role_id

        logger.debug(
            f"  User currently has {len(existing_user_roles)} role(s), "
            f"will have {len(updated_role_ids)} after update. "
            f"Default role will be '{default_role_id}'"
        )
        
        try:
            user_update_response = self.update_user_roles(
                email=user_email,
                role_ids=updated_role_ids,
                default_role_id=default_role_id
            )
            
            if user_update_response:
                return True
        except Exception as e:
            logger.error(
                f"Failed to assign role '{role_name}' to user '{user_email}': {e}"
            )
        return False

    def create_or_update_user_roles(self, user_repos: dict) -> dict:
        """Create or update Prisma roles for users with their GitLab repository access.
        
        For each user, creates a new role or updates existing with current repo access.
        
        Args:
            user_repos: Dictionary mapping usernames to repository lists
        
        Returns:
            Summary dict with success/fail status and details for each user
        """
        # Fetch Prisma users and create lookup
        prisma_users_by_email = self.fetch_prisma_users_lookup()
        results = {}
        # Mapping for Prisma Repo and Gitlab URLs for logging purposes
        prisma_repos_data = self.get_repos(self.integration_id)
        intg_repos = list(prisma_repos_data.get('all_repos', []))
        repo_id_to_url = {repo['id']: repo.get('source', '') + "|" + repo.get('fullRepositoryName', '') for repo in intg_repos if repo.get('id')}
        # Pre-fetch all roles once to avoid redundant API calls per user
        cached_roles = self.get_custom_roles()
        logger.info(f"Pre-fetched {len(cached_roles)} roles for lookup")
        # Process each GitLab user
        logger.debug("=" * 80)
        for username, user_data in user_repos.items():
            logger.debug(
                f"\nUser: {username} ({user_data['name']}) | "
                f"Email: {user_data['email']}"
            )
            logger.debug(f"  Total repositories: {len(user_data['repositories'])}")
            user_email = user_data.get('email')
            repo_id_list = [
                repo_info['prisma_repo_id']
                for repo_info in user_data['repositories']
                if repo_info.get('prisma_repo_id')
            ]
            logger.debug(f"  - Prisma repoid found count: {len(repo_id_list)}")
            self.log_user_repositories(user_data)
            user_result = {
                "success": False,
                "reason": None,
                "role_id": None,
                "assigned": False
            }
            if not user_email or user_email.lower() not in prisma_users_by_email:
                logger.debug(
                    f"User '{username}' with email '{user_email}' not found in "
                    f"Prisma users - skipping role creation"
                )
                user_result["reason"] = "User not found in Prisma"
                results[username] = user_result
                continue
            logger.debug(
                f"  User '{username}' found in Prisma with email '{user_email}'"
            )
            # Create or update role with repositories
            role_id = self.create_or_update_role(
                username, user_email, repo_id_list, repo_id_to_url,
                cached_roles=cached_roles
            )
            if role_id:
                user_result["success"] = True
                user_result["role_id"] = role_id
                user_profile = prisma_users_by_email.get(user_email.lower())
                # Use the same role_name logic as in create_or_update_role
                role_name = self._generate_role_name(user_email)
                assigned = self.assign_role_to_user(
                    role_id, role_name, user_email, user_profile
                )
                user_result["assigned"] = assigned
                if not assigned:
                    user_result["reason"] = "Failed to assign role to user"
            else:
                user_result["reason"] = "Failed to create or update role"
            results[username] = user_result
        return results

    # ========================================================================
    # Role Management Methods
    # ========================================================================
    def get_custom_roles(self) -> list:
        """Retrieve all custom roles from Prisma Cloud CSPM.
        
        Requires Account Administrator or System Admin permissions.
        API Reference: https://pan.dev/prisma-cloud/api/cspm/get-roles/ 
        
        Returns:
            list: List of custom role objects, empty list on error
        """
        if self._custom_roles_cache is not None:
            logger.debug(f"Using cached custom roles ({len(self._custom_roles_cache)})")
            return self._custom_roles_cache

        logger.debug('Fetching custom roles from Prisma Cloud...')
        try:
            response = self.make_request("GET", "/user/role")
        except PrismaClientError as e:
            logger.error(f"Failed to get custom roles: {e}")
            return []
        response.raise_for_status()
        roles = response.json()
        role_count = len(roles) if isinstance(roles, list) else 0
        logger.debug(f"Found {role_count} custom roles")
        if isinstance(roles, list):
            self._custom_roles_cache = roles
            self._rebuild_role_name_cache(roles)
            return roles

        self._custom_roles_cache = []
        self._role_name_to_id_cache = {}
        return []

    def find_role_id_by_name(self, role_name: str, cached_roles: Optional[list] = None) -> Optional[str]:
        """Find a role ID by searching for a role with matching name.
        
        Args:
            role_name (str): The name of the role to search for
                (case-insensitive)
            cached_roles (list, optional): Pre-fetched roles list to avoid
                redundant API calls. If None, fetches from API.
        
        Returns:
            str: Role ID (UUID) if found, None otherwise
        """
        logger.debug(f"Searching for role with name: '{role_name}'")
        normalized_role_name = role_name.lower()

        if cached_roles is None:
            cached_role_id = self._role_name_to_id_cache.get(normalized_role_name)
            if cached_role_id:
                logger.debug(f"Found role '{role_name}' with ID from cache: {cached_role_id}")
                return cached_role_id

        roles = cached_roles if cached_roles is not None else self.get_custom_roles()
        
        if not roles:
            logger.warning("No roles found or unable to retrieve roles")
            return None
        
        # Search for role by name (case-insensitive)
        for role in roles:
            if role.get('name', '').lower() == normalized_role_name:
                role_id = role.get('id')
                if role_id:
                    self._role_name_to_id_cache[normalized_role_name] = role_id
                logger.debug(f"Found role '{role.get('name')}' with ID: {role_id}")
                return role_id
        
        logger.warning(f"Role with name '{role_name}' not found")
        return None

    def get_role_details(self, role_id: str) -> Optional[dict]:
        """
        Get detailed information about a specific role by ID.
        API Reference: https://pan.dev/prisma-cloud/api/cspm/get-role-by-id/ 
        
        Args:
            role_id (str): Role ID (UUID) to retrieve
        
        Returns:
            dict: Role details including all fields, None if error
        """
        cached_role_details = self._role_details_cache.get(role_id)
        if cached_role_details is not None:
            logger.debug(f"Using cached role details for ID: '{role_id}'")
            return cached_role_details

        logger.debug(f"Fetching role details for ID: '{role_id}'")
        try:
            response = self.make_request("GET", f"/user/role/{role_id}")
        except PrismaClientError as e:
            logger.error(f"Failed to get role details for ID '{role_id}': {e}")
            return None
        response.raise_for_status()
        role_data = response.json()
        self._role_details_cache[role_id] = role_data
        logger.debug(f"Retrieved role '{role_data.get('name')}' with {len(role_data.get('codeRepositoryIds', []))} repos")
        return role_data

    def create_custom_role(self, config: RoleConfig) -> Optional[dict]:
        """Create a custom role in Prisma Cloud.
        
        Args:
            config: RoleConfig with name, description, role_type, repository IDs, etc.
        
        Returns:
            Created role object with role ID, or None on failure
        
        API Reference: https://pan.dev/prisma-cloud/api/cspm/add-role/ 
        """
        if not config.name or not config.description:
            logger.error("Role name and description are required")
            return None

        payload = self._build_role_payload(
            config.name,
            config.description,
            config.role_type,
            config.account_group_ids,
            config.code_repository_ids,
            config.resource_list_ids,
            config.restrict_dismissal_access,
            config.additional_attributes
        )

        # Enriched logging with repo URLs and names if lookups provided
        if config.code_repository_ids:
            if config.repo_id_to_url:
                repo_details = [
                    f"{repo_id} (Source|Full Path: {config.repo_id_to_url.get(repo_id, '')})"
                    for repo_id in payload.get("codeRepositoryIds", [])
                ]
                logger.info(f"Role: {config.name} | associated Repositories: {repo_details}")
            else:
                repo_ids = payload.get("codeRepositoryIds", [])
                sample_size = 10
                repo_ids_sample = repo_ids[:sample_size]
                logger.info(
                    f"Role: {config.name} | associated Repository Id's sample "
                    f"({len(repo_ids_sample)}/{len(repo_ids)}): {repo_ids_sample}"
                )

        logger.debug(f"Payload keys: {list(payload.keys())}")
        logger.debug(f"Payload: {payload}")
        try:
            logger.debug(f"Sending to Prisma API: POST /user/role with payload: {payload}")
            response = self.make_request("POST", "/user/role", payload=payload)
        except PrismaClientError as e:
            logger.error(f"Failed to create custom role '{config.name}': {e}")
            logger.debug(f"Request payload was: {payload}")
            return None
        response.raise_for_status()
        # Handle empty response body gracefully
        if not response.text.strip():
            self._invalidate_role_caches()
            logger.debug("Create successful, but response body is empty. Returning minimal success info.")
            return {
                "name": config.name,
                "description": config.description,
                "roleType": config.role_type,
                "restrictDismissalAccess": config.restrict_dismissal_access,
                "codeRepositoryIds": config.code_repository_ids or []
            }
        role_data = response.json()
        self._invalidate_role_caches()
        logger.info(f"Successfully created custom role '{config.name}' with ID: {role_data.get('id')}")
        return role_data

    def update_custom_role(self, role_id: str, config: RoleConfig) -> Optional[dict]:
        """
        Update an existing custom role in Prisma Cloud.
        API Reference: https://pan.dev/prisma-cloud/api/cspm/update-role/ 
        
        Args:
            role_id (str): Role ID to update (required)
            config (RoleConfig): Role configuration containing updated values
        
        Returns:
            dict: Updated role object if successful, None otherwise
        """
        if not role_id or not config.name or not config.description:
            logger.error("Role ID, name, and description are required for update")
            return None

        payload = self._build_role_payload(
            config.name,
            config.description,
            config.role_type,
            config.account_group_ids,
            config.code_repository_ids,
            config.resource_list_ids,
            config.restrict_dismissal_access,
            config.additional_attributes
        )

        # Enriched logging with repo URLs and names if lookups provided
        if config.code_repository_ids:
            if config.repo_id_to_url:
                repo_details = [
                    f"{repo_id} (Source|Full Path: {config.repo_id_to_url.get(repo_id, '')})"
                    for repo_id in payload.get("codeRepositoryIds", [])
                ]
                logger.info(f"Role: {config.name} | associated Repositories: {repo_details}")
            else:
                repo_ids = payload.get("codeRepositoryIds", [])
                sample_size = 10
                repo_ids_sample = repo_ids[:sample_size]
                logger.info(
                    f"Role: {config.name} | associated Repository Id's sample "
                    f"({len(repo_ids_sample)}/{len(repo_ids)}): {repo_ids_sample}"
                )
        try:
            response = self.make_request("PUT", f"/user/role/{role_id}", payload=payload)
        except PrismaClientError as e:
            logger.error(f"Failed to update custom role '{config.name}' (ID: {role_id}): {e}")
            return None
        logger.debug(f"log response: {response.status_code} - {response.text}")
        response.raise_for_status()
        # Handle empty response body gracefully
        if not response.text.strip():
            self._invalidate_role_caches()
            self._invalidate_role_detail_cache(role_id)
            logger.debug("Update successful, but response body is empty. Returning minimal success info.")
            return {
                "id": role_id,
                "name": config.name,
                "description": config.description,
                "roleType": config.role_type,
                "restrictDismissalAccess": config.restrict_dismissal_access,
                "codeRepositoryIds": config.code_repository_ids or []
            }
        role_data = response.json()
        self._invalidate_role_caches()
        self._invalidate_role_detail_cache(role_id)
        logger.info(f"Successfully updated custom role '{config.name}' with ID: {role_id}")
        return role_data