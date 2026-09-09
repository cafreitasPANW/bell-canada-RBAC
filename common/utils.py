
import re
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from common.logger import LoggerFactory


class UtilsError(Exception):
    """Custom exception for utils module errors."""
    pass

logger = LoggerFactory.get_logger(__name__)

# Constants for datetime format handling
DATETIME_FORMAT_ISO_Z_WITH_MS = "%Y-%m-%dT%H:%M:%S.%fZ"
DATETIME_FORMAT_ISO_Z_NO_MS = "%Y-%m-%dT%H:%M:%SZ"
DATETIME_FORMAT_ISO_OFFSET_WITH_MS = "%Y-%m-%dT%H:%M:%S.%f"
DATETIME_FORMAT_ISO_OFFSET_NO_MS = "%Y-%m-%dT%H:%M:%S"

# Constants for GitLab operations
DEFAULT_GITLAB_ACCESS_LEVEL = 20  # GitLab Reporter level
DEFAULT_MAX_WORKERS = 10  # Default concurrent API call workers

def parse_datetime(dt_str: Union[str, datetime]) -> datetime:
    """
    Parse GitLab datetime strings in Z or offset formats.
    
    Handles both Z and offset formats for GitLab datetime strings.
    If input is already a datetime object, returns it unchanged.
    
    Args:
        dt_str: Datetime string in format '2025-08-14T06:40:51.902-04:00' or '2024-06-07T14:32:10.123Z',
                or datetime object
    
    Returns:
        datetime: Parsed datetime object
    
    Examples:
        >>> parse_datetime('2024-06-07T14:32:10.123Z')
        datetime(2024, 6, 7, 14, 32, 10, 123000)
    """

    logger.debug(f"Parsing datetime: {dt_str}")

    if not isinstance(dt_str, str):
        logger.debug(f"Input is already a datetime object: {dt_str}")
        return dt_str

    if dt_str.endswith('Z'):
        try:
            dt = datetime.strptime(dt_str, DATETIME_FORMAT_ISO_Z_WITH_MS)
        except ValueError:
            dt = datetime.strptime(dt_str, DATETIME_FORMAT_ISO_Z_NO_MS)
    else:
        match = re.match(r"(.*)([+-]\d{2}:\d{2})$", dt_str)
        if match:
            dt_base = match.group(1)
            try:
                dt = datetime.strptime(dt_base, DATETIME_FORMAT_ISO_OFFSET_WITH_MS)
            except ValueError:
                dt = datetime.strptime(dt_base, DATETIME_FORMAT_ISO_OFFSET_NO_MS)
        else:
            try:
                dt = datetime.strptime(dt_str, DATETIME_FORMAT_ISO_OFFSET_WITH_MS)
            except ValueError:
                dt = datetime.strptime(dt_str, DATETIME_FORMAT_ISO_OFFSET_NO_MS)

    logger.debug(f"Final parsed datetime: {dt}")
    return dt

def _process_member(
    user_repos: Dict[str, Dict[str, Any]],
    member: Dict[str, Any],
    project_name: str,
    project_id: Any,
    prisma_repo_lookup: Dict[str, Any],
    allowed_emails: Optional[Set[str]] = None
) -> None:
    """Process a single project member and add them to user repository mapping.
    
    Updates user_repos dictionary with member information and their repository access.
    Handles multiple repositories for the same user across projects.
    
    Args:
        user_repos: Dictionary mapping username to user data and repository list
        member: Member dictionary from GitLab API with username, email, access_level
        project_name: GitLab project path_with_namespace
        project_id: GitLab project ID
        prisma_repo_lookup: Dictionary mapping repository names to Prisma repo info
    
    Returns:
        None (modifies user_repos in-place)
    """
    username = member.get('username')
    member_email = member.get('email') or member.get('public_email')
    if allowed_emails is not None:
        if not member_email or member_email.lower() not in allowed_emails:
            return
    if not username:
        logger.debug(f"Skipping member with no username - email: {member_email}")
        return
    if not user_repos[username]['username']:
        user_repos[username]['username'] = username
        user_repos[username]['name'] = member.get('name')
        user_repos[username]['email'] = member_email
    repo_lookup = prisma_repo_lookup.get(project_name) or {}
    user_repos[username]['repositories'].append({
        'repo': project_name,
        'access_level': member.get('access_level', DEFAULT_GITLAB_ACCESS_LEVEL),
        'project_id': project_id,
        'prisma_repo_id': repo_lookup.get('id'),
        'is_new': repo_lookup.get('is_new')
    })

def _handle_fetch_error(project: Dict[str, Any], error: Exception) -> None:
    """Handle and log errors from fetching project members.
    
    Logs different error levels based on error type:
    - Rate limit errors (429, 'rate limit') → logger.error
    - Other errors → logger.error
    
    Args:
        project: GitLab project dictionary containing 'path_with_namespace'
        error: Exception that occurred during member fetch
    
    Returns:
        None
    """
    error_str = str(error).lower()
    project_name = project.get('path_with_namespace')
    if '429' in error_str or 'rate limit' in error_str:
        logger.error(f"Rate limit error for project {project_name}: {error}")
    else:
        logger.error(f"Error fetching members for project {project_name}: {error}")

def _fetch_project_members(
    idx_project: Tuple[int, Dict[str, Any]],
    gitlab_client: Any
) -> Tuple[Any, str, List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch members for a project (intended for ThreadPoolExecutor use).
    
    Called within thread pool to fetch project members concurrently.
    Retrieves all members including inherited ones from GitLab API.
    
    Args:
        idx_project: Tuple of (index, project_dict) for enumeration
        gitlab_client: GitLabClient instance with get_project_members method
    
    Returns:
        Tuple of (project_id, project_name, gitlab_members, project) for processing
    
    Raises:
        Exception: Re-raised from gitlab_client.get_project_members() on API errors
    """
    idx, project = idx_project
    project_id = project.get('id')
    project_name = project.get('path_with_namespace')
    logger.debug(f"{idx}) Processing project members for: {project_name} (ID: {project_id})")
    gitlab_members = gitlab_client.get_project_members(project_id, True)
    return project_id, project_name, gitlab_members, project

def create_new_repos_list(
    gitlab_projects: List[Dict[str, Any]],
    prisma_repos: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Compare GitLab projects and Prisma Cloud repositories to find projects not yet onboarded.
    
    Args:
        gitlab_projects (list): List of GitLab project dictionaries with 'path_with_namespace' field
        prisma_repos (list): List of Prisma repository dictionaries with 'fullRepositoryName' or 'repository' field
    
    Returns:
        list: List of repository paths (strings) that exist in GitLab but not in Prisma Cloud
    """
    # Extract repository paths from GitLab projects
    gitlab_repo_paths = {project.get('path_with_namespace') for project in gitlab_projects if project.get('path_with_namespace')}
    
    # Extract repository paths from Prisma Cloud repos
    # Use fullRepositoryName if available, otherwise fall back to repository
    prisma_repo_paths = set()
    for repo in prisma_repos:
        # Try fullRepositoryName first (full path), then repository (short name)
        repo_path = repo.get('fullRepositoryName') or repo.get('repository')
        if repo_path:
            prisma_repo_paths.add(repo_path)
        
    # Find repos in GitLab but not in Prisma
    missing_repo_paths = gitlab_repo_paths - prisma_repo_paths

    logger.debug(f"GitLab projects: {len(gitlab_repo_paths)}")
    logger.debug(f"Prisma repositories: {len(prisma_repo_paths)}")
    logger.debug(f"Projects not in Prisma: {len(missing_repo_paths)}")

    # Build a list of dicts for missing repos, matching prisma_repos structure
    # Use 'fullRepositoryName' as key, fallback to 'repository' if needed
    new_repos = []
    for project in gitlab_projects:
        repo_path = project.get('path_with_namespace')
        if repo_path and repo_path in missing_repo_paths:
            # Use the same structure as prisma_repos: prefer 'fullRepositoryName'
            new_repos.append({'fullRepositoryName': repo_path, 'is_new': True})

    # Mark existing repos with is_new: False for uniformity
    existing_repos = [{**repo, 'is_new': False} for repo in prisma_repos]

    # Return all original prisma_repos (marked) plus new ones
    return existing_repos + new_repos

def build_user_repos_mapping_parallel(
    projects: List[Dict[str, Any]],
    gitlab_client: Any,
    prisma_repo_lookup: Optional[Dict[str, Any]] = None,
    allowed_emails: Optional[Set[str]] = None,
    max_workers: int = DEFAULT_MAX_WORKERS
) -> Dict[str, Dict[str, Any]]:
    """
    Build a user-centric mapping of repositories from GitLab projects using parallel API calls.
    
    For each user, collects all repositories they have access to along with their access levels.
    Uses ThreadPoolExecutor to fetch project members concurrently for improved performance.
    
    Args:
        projects (list): List of GitLab project dictionaries
        gitlab_client: GitLabClient instance for fetching project members
        prisma_repo_lookup (dict, optional): Dictionary mapping repo paths to Prisma repo IDs
        allowed_emails (set, optional): Lowercased email set to include in mapping
        max_workers (int): Maximum number of parallel API calls (default: 10)
    
    Returns:
        dict: User-to-repositories mapping
              Structure: {username: {'name': name, 'email': email, 'repositories': [...]}, ...}
    """
    logger.info(f"Building user-to-repositories mapping from GitLab projects (parallel, max_workers={max_workers})...")
    prisma_repo_lookup = prisma_repo_lookup or {}
    user_repos: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'username': '', 'name': '', 'email': '', 'repositories': []})


    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_project = {
            executor.submit(_fetch_project_members, (idx, proj), gitlab_client): proj
            for idx, proj in enumerate(projects, 1)
        }
        for future in as_completed(future_to_project):
            try:
                project_id, project_name, gitlab_members, _ = future.result()
                for member in gitlab_members:
                    _process_member(
                        user_repos,
                        member,
                        project_name,
                        project_id,
                        prisma_repo_lookup,
                        allowed_emails
                    )
            except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as e:
                _handle_fetch_error(future_to_project[future], e)

    logger.debug(f"Built user-to-repositories mapping for {len(user_repos)} unique users")
    for username, user_data in user_repos.items():
        logger.debug(f"User '{username}' ({user_data['email']}) has access to {len(user_data['repositories'])} repositories")

    return dict(user_repos)

def filefed_user_repos_mapping_parallel(
    projects: List[Dict[str, Any]],
    prisma_repo_lookup: Optional[Dict[str, Any]] = None,
    max_workers: int = DEFAULT_MAX_WORKERS
) -> Dict[str, Dict[str, Any]]:
    """
    Build a user-centric mapping of repositories from GitLab projects using parallel API calls.
    
    For each user, collects all repositories they have access to along with their access levels.
    Uses ThreadPoolExecutor to fetch project members concurrently for improved performance.
    
    Args:
        projects (list): List of GitLab project dictionaries
        prisma_repo_lookup (dict, optional): Dictionary mapping repo paths to Prisma repo IDs
        max_workers (int): Maximum number of parallel API calls (default: 10)
    
    Returns:
        dict: User-to-repositories mapping
              Structure: {username: {'name': name, 'email': email, 'repositories': [...]}, ...}
    """
    logger.info(f"Building user-to-repositories mapping from GitLab projects (parallel, max_workers={max_workers})...")
    prisma_repo_lookup = prisma_repo_lookup or {}
    user_repos: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'username': '', 'name': '', 'email': '', 'repositories': []})

    # Instead of fetching members via API, use members from each project dict
    for idx, project in enumerate(projects, 1):
        project_id = project.get('id')
        project_name = project.get('path_with_namespace')
        gitlab_members = project.get('members', [])
        logger.debug(f"{idx}) Processing project members for: {project_name} (ID: {project_id})")
        for member in gitlab_members:
            _process_member(user_repos, member, project_name, project_id, prisma_repo_lookup)

    logger.debug(f"Built user-to-repositories mapping for {len(user_repos)} unique users")
    for username, user_data in user_repos.items():
        logger.debug(f"User '{username}' ({user_data['email']}) has access to {len(user_data['repositories'])} repositories")

    return dict(user_repos)

