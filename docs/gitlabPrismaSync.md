# GitLab-Prisma Cloud Synchronization Workflow

## Overview

The `gitlabPrismaSync.py` script synchronizes GitLab projects and users with Prisma Cloud Code Security by automatically:

1. **Fetching active GitLab projects** based on activity thresholds
2. **Activating missing repositories** in Prisma Cloud integrations
3. **Building user-to-repository mappings** from GitLab
4. **Creating/updating Prisma Cloud roles** for users with their repository access

---

## Process Flow

```
┌─────────────────────────────────────────┐
│   Validate Environment & Configuration  │
├─────────────────────────────────────────┤
│   - Check required environment variables|
│   - Parse runtime configuration         |
│   - Set run mode (DRY_RUN or LIVE)      |
└────────────┬────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────┐
│   Initialize API Clients                │
├─────────────────────────────────────────┤
│   - GitLab API client                   |
│   - Prisma Cloud API client             |
└────────────┬────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────┐
│   Fetch Active GitLab Projects          │
├─────────────────────────────────────────┤
│   - Query top N projects (default: 150) │
│   - Filter by activity threshold        │
│     (default: last 35 minutes)          │
└────────────┬────────────────────────────┘
             │
             ▼
        ┌────────────┐
        │ Projects   │
        │ Found?     │
        └─┬──────┬───┘
          │ No   │ Yes
          │      ▼
          │  ┌─────────────────────────┐
          │  │ Activate Repositories   │
          │  ├─────────────────────────┤
          │  │ - Find missing repos    │
          │  │ - Update Prisma Cloud   │
          │  │   integration           │
          │  │ - Update common ssdlc   │
          │  │   role                  │
          │  │   (appsec-ssdlc-sa-dev) │
          │  └────────────┬────────────┘
          │               │
          │               ▼
          │        ┌────────────┐
          │        │ Repos      │
          │        │ Found?     │
          │        └─┬──────┬───┘
          │          │ No   │ Yes
          │          │      ▼
          │          │  ┌──────────────────┐
          │          │  │ Build User       │
          │          │  │ Mappings         │
          │          │  ├──────────────────┤
          │          │  │ - Map each user  │
          │          │  │   to their repos │
          │          │  │ - Parallel       │
          │          │  │   processing     │
          │          │  └────────┬─────────┘
          │          │           │
          │          │           ▼
          │          │  ┌──────────────────┐
          │          │  │ Create/Update    │
          │          │  │ Prisma Roles     │
          │          │  ├──────────────────┤
          │          │  │ - Sync user      │
          │          │  │   roles with     │
          │          │  │   repo access    │
          │          │  └────────┬─────────┘
          │          │           │
          │          │           ▼
          │          │  ┌──────────────────┐
          │          │  │ Summarize        │
          │          │  │ Results          │
          │          │  ├──────────────────┤
          │          │  │ - Count success  │
          │          │  │ - Count failures │
          │          │  │ - Count skipped  │
          │          │  └────────┬─────────┘
          │          │           │
          └──────────┼───────────┼───────────┐
                     │           │           │
                     ▼           ▼           ▼
                  ┌──────────────────────┐
                  │   Exit (Code: 0)     │
                  │   Success / No Data  │
                  └──────────────────────┘
```

---

## Required Environment Variables

These must be set before running the script:

| Variable | Description | Example |
|----------|-------------|---------|
| `GITLAB_TOKEN` | Personal access token for GitLab API | `glpat-xxxxxxxxxxxxx` |
| `PRISMA_ACCESS_TOKEN` | Prisma Cloud API access key | `access_key_id` |
| `PRISMA_SECRET_TOKEN` | Prisma Cloud API secret key | `secret_key` |
| `GITLAB_CONFIG_KEY` | Selects the GitLab/Prisma configuration entry to use | `ug-onprem-prod` |

**Note:** In CI/CD pipelines, credentials are typically provided via Vault. The selected `GITLAB_CONFIG_KEY` is resolved against the JSON config file at `config/gitlab_instances_config.json` unless overridden.

---

## GitLab Instance Configuration File

The script loads GitLab and Prisma integration settings from:

- `config/gitlab_instances_config.json`

Each entry must define:

| Field | Description |
|-------|-------------|
| `url` | Base GitLab host URL used to build the API URL |
| `prisma-integration-id` | Prisma Cloud integration ID associated with that GitLab instance |
| `use-topic-filtering` | If `true`, only projects tagged for Unified-Prisma processing are included |

Each entry may also define:

| Field | Description |
|-------|-------------|
| `visibility` | Optional GitLab visibility filter applied when listing projects, for example `private` |

Example:

```json
{
  "ug-saas-prod": {
    "url": "https://gitlab.com",
    "prisma-integration-id": "1234",
    "use-topic-filtering": false,
    "visibility": "private"
  },
  "ug-onprem-prod": {
    "url": "https://gitlab.int.bell.ca",
    "prisma-integration-id": "5678",
    "use-topic-filtering": true
  }
}
```

---

## Optional Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ACTIVITY_THRESHOLD_MINUTES` | Time window for determining project activity | `30` |
| `LOG_LEVEL` | Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL | `INFO` |
| `GITLAB_API_URL` | Custom GitLab API URL | `https://gitlab.int.bell.ca/api/v4` |
| `GITLAB_INSTANCE_CONFIG_FILE` | Custom path to the GitLab/Prisma instance JSON config file | `config/gitlab_instances_config.json` |
| `PRISMA_API_URL` | Custom Prisma Cloud API URL | `https://api.ca.prismacloud.io` |
| `PRISMA_APPSEC_SSDLC_SA_DEV_ROLE_NAME` | Shared CI/CD role name updated with all integration repositories | `appsec-ssdlc-sa-dev` |
| `PRISMA_HOST_URL` | Custom Prisma Cloud host URL (used to build API URL if API URL not set) | `https://api.ca.prismacloud.io` |
| `PRISMA_ROLE_NAME_PREFIX` | Prefix used to generate per-user Prisma role names | `devex_` |
| `PRISMA_SSDLC_DEVELOPER_BASE_ROLE_NAME` | Default base role name that is replaced with generated user role when assigned as default | `ssdlc_developer_base` |
| `RUN_MODE` | Execution mode: `DRY_RUN` (no changes) or `LIVE` (apply changes) | `DRY_RUN` |
| `TOP_N_PROJECTS` | Number of active projects to synchronize | `100` |

---

## Execution Modes

### DRY_RUN (Default)
- Performs all operations **without applying changes**
- Safe for validation and testing
- Useful for preview before going live
- No modifications made to Prisma Cloud or GitLab

### LIVE
- **Applies all changes** to Prisma Cloud
- Creates new roles, updates existing roles, and activates repositories
- Use with caution in production environments

---

## Exit Codes

| Code | Meaning | Action |
|------|---------|--------|
| `0` | **Success** | Script completed successfully |
| `1` | **Failure** | Configuration validation or processing error occurred (see logs for details) |

---

## Key Functions

### `validate_environment()`
- Checks that all required environment variables are present
- Exits with error if any are missing
- Logs confirmation on success

### `get_runtime_config()`
- Parses optional environment variables
- Validates `RUN_MODE` is either `DRY_RUN` or `LIVE`
- Returns configured values with defaults

### `load_gitlab_instance_config()`
- Loads GitLab and Prisma integration settings from the JSON config file
- Validates entry structure and required fields
- Raises a configuration error for missing files, invalid JSON, or incomplete entries

### `get_gitlab_config()`
- Resolves the selected entry from `GITLAB_CONFIG_KEY`
- Derives the GitLab API URL from the selected `url`
- Returns the Prisma integration ID and topic-filtering mode for the selected entry

### `initialize_clients(run_mode)`
- Creates GitLab API client with provided token
- Resolves GitLab and Prisma settings from the selected config entry
- Creates Prisma Cloud client with access/secret keys
- Sets Prisma client to dry-run mode if `RUN_MODE=DRY_RUN`

### `fetch_active_projects(gitlab, top_n_projects, activity_threshold_minutes, run_mode)`
- Queries GitLab for active projects
- Filters based on activity in the last N minutes
- Limits results to top N projects
- Logs count of projects found

### `activate_repositories(prisma, projects, prisma_intg_id)`
- Identifies repositories not yet in Prisma Cloud
- Adds missing repositories to Prisma integration
- Returns lookup table of activated repositories
- Respects `DRY_RUN` mode for preview

### `build_user_mapping(projects, gitlab, prisma_repo_lookup)`
- Extracts user information from GitLab projects
- Maps each user to their accessible repositories
- Uses parallel processing for performance
- Returns dictionary of user-to-repos mappings

### `create_or_update_user_roles(user_repos)`
- Creates new roles in Prisma Cloud for users
- Updates existing roles with repository access
- Respects `DRY_RUN` mode for preview

### `summarize_role_sync(results)`
- Counts successful, failed, and skipped operations
- Logs warnings for skipped users (not found in Prisma)
- Logs errors for any failed synchronizations
- Provides summary of sync operation completion

---

## Signal Handling

The script gracefully handles termination signals:

| Signal | Trigger | Behavior |
|--------|---------|----------|
| `SIGINT` | Ctrl+C | Logs interrupt message, performs cleanup, exits with code 0 |
| `SIGTERM` | Termination request | Same as SIGINT |

---

## Error Handling

The script handles multiple error types:

- **`PrismaClientError`**: Issues with Prisma Cloud API communication
- **`requests.exceptions.RequestException`**: Network/HTTP errors
- **`ValueError`**: Invalid data or parsing errors
- **`EnvironmentValidationError`**: Missing or invalid environment variables
- **Other exceptions**: Logged as unexpected errors

All exceptions are logged with full traceback and the script exits with code 1.

---

## Logging

All operations are logged with timestamps and severity levels:

- **DEBUG**: Detailed diagnostic information
- **INFO**: General informational messages (start/end of major steps)
- **WARNING**: Warning about skipped users or partial failures
- **ERROR**: Errors that prevent successful completion
- **CRITICAL**: Critical system failures

Logs help with:
- Auditing synchronization activities
- Troubleshooting failures
- Verifying operations in DRY_RUN mode before going LIVE

---

## Example Usage

### Local Development (DRY_RUN)
```bash
export GITLAB_TOKEN="glpat-xxxxxxxxxxxx"
export PRISMA_ACCESS_TOKEN="access_key"
export PRISMA_SECRET_TOKEN="secret_key"
export GITLAB_CONFIG_KEY="ug-onprem-prod"
export RUN_MODE="DRY_RUN"
export TOP_N_PROJECTS="10"
export LOG_LEVEL="INFO"

python gitlabPrismaSync.py
```

### CI/CD Pipeline (LIVE)
```bash
export RUN_MODE="LIVE"
export GITLAB_CONFIG_KEY="ug-onprem-prod"
python gitlabPrismaSync.py
# (Credentials provided via Vault)
```

---

## Workflow Summary

1. **Initialization**: Validate environment, load clients
2. **Discovery**: Find active GitLab projects within threshold
3. **Registration**: Activate missing repos in Prisma Cloud
4. **Mapping**: Build user-to-repository relationships
5. **Synchronization**: Create/update Prisma Cloud roles
6. **Reporting**: Summarize results with success/failure counts

The entire process is logged for audit and troubleshooting purposes, with `DRY_RUN` mode available for safe preview before applying live changes.

---

## Recent Updates

### 2026-04-23

- Replaced direct `GITLAB_INTEGRATION_ID` input with `GITLAB_CONFIG_KEY` selection.
- Moved GitLab/Prisma instance settings into `config/gitlab_instances_config.json`.
- Added runtime loading and validation for the external instance config file.
- Replaced `GITLAB_HOST_URL`-driven selection with config-file `url` entries.
- Added `GITLAB_INSTANCE_CONFIG_FILE` as an optional override for the default config path.
- Updated topic filtering behavior to be controlled by `use-topic-filtering` per config entry.
- Added optional per-instance `visibility` support so GitLab.com and on-prem filtering can be configured independently.

### 2026-04-05

- Extracted repository activation polling into a dedicated helper in [client/prisma_client.py](client/prisma_client.py): `_poll_for_activated_repos(...)`.
- Updated repository activation flow to send only newly discovered repositories (`is_new=True`) instead of re-sending all repositories.
- Added eventual-consistency polling after repository activation to confirm new repositories are visible before subsequent role updates.
- Added role sync helper `_update_appsec_ssdlc_sa_dev_role(...)` to keep the shared CI/CD role `appsec-ssdlc-sa-dev` aligned with integration repositories.
- Switched integration repository onboarding call to `POST /code/api/v2/repositories` payload format (`integrationId`, `integrationType`, `repositoriesNames`).
- Added integration type caching (`_load_integration_cache`, `get_integration_type`) to resolve integration metadata once and reuse it.
- Updated default-role assignment behavior: when a user's default role is `ssdlc_developer_base`, it is switched to the generated `devex_*` role during assignment.
- Improved exception messages in `activate_missing_repos_integration(...)` for clearer operational debugging.
