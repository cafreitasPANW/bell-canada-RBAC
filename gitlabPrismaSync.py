
"""GitLab-Prisma Cloud Synchronization Orchestrator.

Synchronizes GitLab projects and users with Prisma Cloud Code Security by:
1. Fetching active GitLab projects based on activity thresholds
2. Activating missing repositories in Prisma Cloud integrations
3. Building user-to-repository mappings from GitLab
4. Creating/updating Prisma Cloud roles for users with their repository access

Workflow:
- Validates required environment variables (credentials, integration ID)
- Initializes GitLab and Prisma Cloud API clients
- Identifies active projects and missing repositories
- Synchronizes user repository access via Prisma Cloud custom roles
- Logs results and handles errors gracefully

Environment Variables (required):
- GITLAB_TOKEN: Personal access token for GitLab API
- PRISMA_ACCESS_TOKEN: Prisma Cloud API access key
- PRISMA_SECRET_TOKEN: Prisma Cloud API secret key
- GITLAB_CONFIG_KEY: Key used to select GitLab/Prisma integration config

Environment Variables (optional):
- RUN_MODE: 'DRY_RUN' (default) or 'LIVE' - controls whether changes are applied
- TOP_N_PROJECTS: Number of active projects to sync (default: 100)
- ACTIVITY_THRESHOLD_MINUTES: Activity window in minutes (default: 30)
- LOG_LEVEL: DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)

Exit Codes:
- 0: Successful completion
- 1: Configuration validation or processing failure (check logs for details)

Signal Handling:
- SIGINT (Ctrl+C): Graceful shutdown
- SIGTERM: Graceful shutdown
"""

# Standard library imports
import json
import os
import signal
import sys
import time
from typing import Tuple, Any, Optional, Set, Dict

# Third-party imports
import requests
from dotenv import load_dotenv

# Local application imports
from client.gitlab_client import GitLabClient
from client.prisma_client import PrismaClient, PrismaClientError
from common.logger import LoggerFactory
from common.utils import build_user_repos_mapping_parallel

class EnvironmentValidationError(Exception):
    """Custom exception for environment validation errors."""
    pass

logger = LoggerFactory.get_logger(__name__)

SEPARATOR_LINE = "*****************************************************************"

# Required environment variables (set by pipeline Vault or local .env)
REQUIRED_ENV_VARS = [
    'GITLAB_TOKEN',
    'PRISMA_ACCESS_TOKEN',
    'PRISMA_SECRET_TOKEN',
    'GITLAB_CONFIG_KEY'
]

DEFAULT_GITLAB_INSTANCE_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__),
    'config',
    'gitlab_instances_config.json',
)

def signal_handler(sig: int, frame: Any) -> None:
    """Handle Ctrl+C and other termination signals gracefully."""
    logger.warning("\nReceived interrupt signal. Shutting down gracefully...")
    logger.info("Cleanup complete. Exiting.")
    sys.exit(0)

def validate_environment() -> None:
    """Validate required environment variables are present.
    
    Exits script with error if any required variables are missing.
    """
    missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing_vars:
        logger.error(
            f"Missing required environment variables: "
            f"{', '.join(missing_vars)}"
        )
        logger.error(
            "Pipeline should set these via Vault, or set them in .env "
            "for local dev"
        )
        raise EnvironmentValidationError(
            f"Missing required environment variables: {', '.join(missing_vars)}"
        )
    logger.info(
        "Successfully loaded credentials from environment variables"
    )

def parse_int_env(var_name: str, default: int) -> int:
    """Parse an integer environment variable with validation."""
    raw_value = os.getenv(var_name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.error(
            f"Invalid {var_name}: {raw_value}. Must be an integer."
        )
        raise EnvironmentValidationError(
            f"Invalid {var_name}: {raw_value}. Must be an integer."
        )

def get_runtime_config() -> Tuple[str, int, int]:
    """Load and validate runtime configuration from environment variables."""
    run_mode = os.getenv('RUN_MODE', 'DRY_RUN').upper()
    if run_mode not in ('DRY_RUN', 'LIVE'):
        logger.error(
            f"Invalid RUN_MODE: {run_mode}. Allowed values are DRY_RUN or LIVE."
        )
        raise EnvironmentValidationError(
            f"Invalid RUN_MODE: {run_mode}. Allowed values are DRY_RUN or LIVE."
        )
    top_n_projects = parse_int_env('TOP_N_PROJECTS', 100)
    activity_threshold_minutes = parse_int_env('ACTIVITY_THRESHOLD_MINUTES', 30)
    return run_mode, top_n_projects, activity_threshold_minutes

def load_gitlab_instance_config() -> Dict[str, Dict[str, Any]]:
    """Load GitLab/Prisma instance configuration from JSON file."""
    config_path = os.getenv(
        'GITLAB_INSTANCE_CONFIG_FILE',
        DEFAULT_GITLAB_INSTANCE_CONFIG_PATH,
    )

    try:
        with open(config_path, 'r', encoding='utf-8') as config_file:
            config_data = json.load(config_file)
    except FileNotFoundError as exc:
        raise EnvironmentValidationError(
            f"GitLab instance config file not found: {config_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise EnvironmentValidationError(
            f"Invalid JSON in GitLab instance config file: {config_path}"
        ) from exc

    if not isinstance(config_data, dict) or not config_data:
        raise EnvironmentValidationError(
            f"Invalid GitLab instance config format in {config_path}: expected non-empty object"
        )

    required_fields = {'url', 'prisma-integration-id', 'use-topic-filtering'}
    for config_key, config_entry in config_data.items():
        if not isinstance(config_entry, dict):
            raise EnvironmentValidationError(
                f"Invalid config entry for '{config_key}' in {config_path}: expected object"
            )
        missing_fields = required_fields - set(config_entry.keys())
        if missing_fields:
            missing = ', '.join(sorted(missing_fields))
            raise EnvironmentValidationError(
                f"Missing required fields for '{config_key}' in {config_path}: {missing}"
            )

    return config_data

def get_gitlab_config() -> Tuple[str, str, bool, Optional[str], str]:
    """Resolve GitLab and Prisma settings from GITLAB_CONFIG_KEY."""
    config_key = os.getenv('GITLAB_CONFIG_KEY', '').strip()
    if not config_key:
        raise EnvironmentValidationError("Missing required GITLAB_CONFIG_KEY")

    gitlab_instance_config = load_gitlab_instance_config()
    selected_config = gitlab_instance_config.get(config_key)
    if not selected_config:
        available = ', '.join(sorted(gitlab_instance_config.keys()))
        raise EnvironmentValidationError(
            f"Unknown GITLAB_CONFIG_KEY: {config_key}. Available values: {available}"
        )

    gitlab_host_url = selected_config['url'].rstrip('/')
    prisma_integration_id = str(selected_config['prisma-integration-id'])
    use_topic_filtering = bool(selected_config['use-topic-filtering'])
    visibility = selected_config.get('visibility')
    gitlab_api_url = f"{gitlab_host_url}/api/v4"

    logger.info(
        f"Selected GITLAB_CONFIG_KEY='{config_key}' "
        f"(url={gitlab_host_url}, use-topic-filtering={use_topic_filtering}, visibility={visibility})"
    )
    return (
        gitlab_api_url,
        prisma_integration_id,
        use_topic_filtering,
        visibility,
        config_key,
    )

def fetch_active_projects(
    gitlab: GitLabClient,
    top_n_projects: int,
    activity_threshold_minutes: int,
    run_mode: str
) -> list:
    """Fetch active GitLab projects based on activity window."""
    logger.info(
        f"Fetching top {top_n_projects} active GitLab projects "
        f"(last {activity_threshold_minutes} minutes)"
        f"(Run mode: {run_mode})"
    )
    projects = gitlab.get_active_projects(
        top_n=top_n_projects, last_minutes=activity_threshold_minutes
    )
    logger.info(f"Found {len(projects)} active projects in this pull.")
    return projects

def activate_repositories(
    prisma: PrismaClient,
    projects: list,
    prisma_intg_id: str
) -> dict:
    """Ensure Prisma Cloud integration is updated with missing repositories."""
    logger.info(SEPARATOR_LINE)
    logger.info("Repository Activation Processing")
    logger.info(SEPARATOR_LINE)
    return prisma.activate_missing_repos_integration(projects, prisma_intg_id)

def build_user_mapping(
    projects: list,
    gitlab: GitLabClient,
    prisma_repo_lookup: dict,
    allowed_prisma_emails: Optional[Set[str]] = None
) -> dict:
    """Build user-to-repository mappings for role synchronization."""
    logger.info(SEPARATOR_LINE)
    logger.info("User Mapping Processing")
    logger.info(SEPARATOR_LINE)
    start_time = time.time()
    user_repos = build_user_repos_mapping_parallel(
        projects, gitlab, prisma_repo_lookup,
        allowed_emails=allowed_prisma_emails
    )
    elapsed_time = time.time() - start_time
    logger.info(
        f"Mapping completed in {elapsed_time:.2f} seconds for "
        f"{len(user_repos)} unique users"
    )
    return user_repos

def summarize_role_sync(results: dict, run_mode: str = 'DRY_RUN') -> bool:
    """Summarize results from Prisma role synchronization.

    Args:
        results: Dictionary of user role sync results
        run_mode: 'DRY_RUN' or 'LIVE' - determines whether failures cause process failure

    Returns:
        bool: True when the sync should be considered successful,
            False when it should be considered failed.
            In DRY_RUN mode, always returns True regardless of failures.
    """
    success_count = 0
    fail_count = 0
    skipped_count = 0
    skipped_users = []
    failed_users = []
    for username, result in results.items():
        if result.get('success'):
            success_count += 1
        elif result.get('reason', '').lower() == 'user not found in prisma':
            skipped_count += 1
            skipped_users.append((username, result.get('reason')))
        else:
            fail_count += 1
            failed_users.append((username, result.get('reason')))
    logger.info(
        f"User/role sync complete: {success_count} succeeded, "
        f"{fail_count} failed, {skipped_count} skipped."
    )
    if skipped_count > 0:
        logger.warning("Skipped users (not found in Prisma):")
        for username, reason in skipped_users:
            logger.warning(f"  {username}: {reason}")
    if fail_count > 0:
        logger.warning("Failures detected in user/role sync:")
        for username, reason in failed_users:
            logger.warning(f"  {username}: {reason}")

    if success_count == 0 and fail_count > 0:
        if run_mode == 'DRY_RUN':
            logger.info(
                "DRY_RUN: User/role sync produced no successful users, "
                "but skipping failure in DRY_RUN mode."
            )
            return True
        logger.error(
            "User/role sync produced no successful users synced; "
            "marking process as failed."
        )
        return False

    return True

def initialize_clients(run_mode: str) -> Tuple[GitLabClient, PrismaClient]:
    """Initialize GitLab, Prisma API clients.
    
    Returns:
        tuple: (gitlab_client, prisma_client)
    
    Raises:
        SystemExit: If client initialization fails
    """
    try:
        gitlab_token = os.getenv('GITLAB_TOKEN')
        (
            gitlab_api_url,
            gitlab_integration_id,
            use_topic_filtering,
            visibility,
            gitlab_config_key,
        ) = get_gitlab_config()
        prisma_access_token = os.getenv('PRISMA_ACCESS_TOKEN')
        prisma_secret_token = os.getenv('PRISMA_SECRET_TOKEN')
        prisma_api_url = os.getenv(
            'PRISMA_API_URL',
            os.getenv('PRISMA_HOST_URL', 'https://api.ca.prismacloud.io ')
        )
        role_name_prefix = os.getenv('PRISMA_ROLE_NAME_PREFIX', 'devex_')
        ssdlc_developer_base_role_name = os.getenv(
            'PRISMA_SSDLC_DEVELOPER_BASE_ROLE_NAME',
            'ssdlc_developer_base',
        )
        appsec_ssdlc_sa_dev_role_name = os.getenv(
            'PRISMA_APPSEC_SSDLC_SA_DEV_ROLE_NAME',
            'appsec-ssdlc-sa-dev',
        )
        prisma_dry_run = run_mode == 'DRY_RUN'
        
        gitlab = GitLabClient(
            api_url=gitlab_api_url,
            token=gitlab_token,
            use_topic_filtering=use_topic_filtering,
            visibility=visibility,
        )
        prisma = PrismaClient(
            api_url=prisma_api_url,
            access_key=prisma_access_token,
            secret_key=prisma_secret_token,
            integration_id=gitlab_integration_id,
            dry_run=prisma_dry_run,
            role_name_prefix=role_name_prefix,
            ssdlc_developer_base_role_name=ssdlc_developer_base_role_name,
            appsec_ssdlc_sa_dev_role_name=appsec_ssdlc_sa_dev_role_name,
            gitlab_key=gitlab_config_key,
        )
        logger.info("Successfully initialized GitLab, Prisma")
        return gitlab, prisma
    except Exception as e:
        logger.error(f"Failed to initialize API clients: {e}")
        raise

def main() -> None:
    """Orchestrate GitLab-Prisma synchronization workflow.
    
    Flow:
        1. Validate environment and load configuration
        2. Initialize GitLab and Prisma clients
        3. Fetch active GitLab projects based on activity threshold
        4. Activate repositories in Prisma Cloud integration
        5. Build user-to-repository mappings from GitLab
        6. Create or update Prisma Cloud roles for users
        7. Summarize synchronization results
    
    Raises:
        SystemExit: On environment validation or general processing failure
        
    Environment Variables:
        - GITLAB_TOKEN: GitLab API token (required)
        - PRISMA_ACCESS_TOKEN: Prisma Cloud access key (required)
        - PRISMA_SECRET_TOKEN: Prisma Cloud secret key (required)
        - GITLAB_CONFIG_KEY: Selects GitLab URL + Prisma integration settings (required)
        - RUN_MODE: 'DRY_RUN' or 'LIVE' (default: DRY_RUN)
        - TOP_N_PROJECTS: Number of active projects to sync (default: 100)
        - ACTIVITY_THRESHOLD_MINUTES: Minutes to consider project active (default: 30)
    """
    load_dotenv()

    try:
        validate_environment()
        run_mode, top_n_projects, activity_threshold_minutes = get_runtime_config()
        gitlab, prisma = initialize_clients(run_mode)

        projects = fetch_active_projects(
            gitlab, top_n_projects, activity_threshold_minutes, run_mode
        )
        if not projects:
            logger.warning("No active projects found.")
            logger.info(SEPARATOR_LINE)
            return

        prisma_intg_id = prisma.integration_id
        prisma_repo_lookup = activate_repositories(
            prisma, projects, prisma_intg_id
        )
        if not prisma_repo_lookup:
            logger.warning("No repositories were returned from Prisma. Stopping further processing.")
            logger.info(SEPARATOR_LINE)
            return

        prisma_users_by_email = prisma.fetch_prisma_users_lookup(active_only=True)
        allowed_prisma_emails = set(prisma_users_by_email.keys())
        logger.info(
            f"Using {len(allowed_prisma_emails)} enabled Prisma users "
            f"to filter GitLab user mapping"
        )

        # Build user-repository mapping for role synchronization
        user_repos = build_user_mapping(
            projects,
            gitlab,
            prisma_repo_lookup,
            allowed_prisma_emails=allowed_prisma_emails
        )
        # Create or update Prisma roles for users and handle results
        logger.info(SEPARATOR_LINE)
        logger.info("User Create/Update Role Processing")
        logger.info(SEPARATOR_LINE)
        results = prisma.create_or_update_user_roles(user_repos)
        sync_ok = summarize_role_sync(results, run_mode=run_mode)
        if not sync_ok:
            logger.error(SEPARATOR_LINE)
            logger.error("Pipeline failed due to unsuccessful user/role sync results!")
            sys.exit(1)
    except (PrismaClientError, requests.exceptions.RequestException, ValueError, EnvironmentValidationError):
        logger.exception("Expected exception in main")
        logger.error(SEPARATOR_LINE)
        logger.error("Pipeline failed due to an exception in process!")
        sys.exit(1)
    except Exception:
        logger.exception("Unexpected exception captured in main")
        logger.error(SEPARATOR_LINE)
        logger.error("Pipeline failed due to an unexpected exception!")
        sys.exit(1)

if __name__ == "__main__":
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, signal_handler)  # Handle Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Handle termination signal
    
    main()
