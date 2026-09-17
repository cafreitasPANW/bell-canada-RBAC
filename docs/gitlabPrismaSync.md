# GitLab-Cortex Cloud Synchronization Workflow

## Overview

The `gitlabPrismaSync.py` script synchronizes GitLab projects and users with Cortex Cloud Application Security by automatically:

1. **Fetching active GitLab projects** based on activity thresholds
2. **Selecting missing repositories** in Cortex Cloud data sources
3. **Building user-to-repository mappings** from GitLab
4. **Creating/assigning Cortex Cloud roles** for users with their repository access

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
│   - Cortex Cloud API client             |
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
          │  │ - Update Cortex data source │
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
          │          │  │ Cortex Roles     │
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
| `CORTEX_API_KEY_ID` | Cortex Cloud API key ID | `api_key_id` |
| `CORTEX_API_SECRET` | Cortex Cloud API secret key | `api_secret` |
| `GITLAB_CONFIG_KEY` | Selects the GitLab/Cortex configuration entry to use | `ug-onprem-prod` |

**Note:** In CI/CD pipelines, credentials are typically provided via Vault. The selected `GITLAB_CONFIG_KEY` is resolved against the JSON config file at `config/gitlab_instances_config.json` unless overridden.

---

## GitLab Instance Configuration File

The script loads GitLab and Cortex data source settings from:

- `config/gitlab_instances_config.json`

Each entry must define:

| Field | Description |
|-------|-------------|
| `url` | Base GitLab host URL used to build the API URL |
| `cortex-data-source-id` | Cortex Cloud GitLab data source ID associated with that GitLab instance |
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
    "cortex-data-source-id": "1234",
    "use-topic-filtering": false,
    "visibility": "private"
  },
  "ug-onprem-prod": {
    "url": "https://gitlab.int.bell.ca",
    "cortex-data-source-id": "5678",
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
| `GITLAB_INSTANCE_CONFIG_FILE` | Custom path to the GitLab/Cortex instance JSON config file | `config/gitlab_instances_config.json` |
| `CORTEX_API_URL` | Custom Cortex Cloud API URL | `https://api-yourfqdn` |
| `CORTEX_HOST_URL` | Custom Cortex Cloud host URL used when `CORTEX_API_URL` is unset | `https://api-yourfqdn` |
| `CORTEX_ROLE_NAME_PREFIX` | Prefix used to generate per-user Cortex role names | `devex_` |
| `CORTEX_ROLE_COMPONENT_PERMISSIONS` | Comma-separated Cortex component permissions for generated roles | `appsec.repositories.view` |
| `RUN_MODE` | Execution mode: `DRY_RUN` (no changes) or `LIVE` (apply changes) | `DRY_RUN` |
| `TOP_N_PROJECTS` | Number of active projects to synchronize | `100` |

---

## Execution Modes

### DRY_RUN (Default)
- Performs all Cortex write operations **without applying changes**
- Safe for validation and testing
- Useful for preview before going live
- No modifications made to Cortex Cloud or GitLab

### LIVE
- **Applies all changes** to Cortex Cloud
- Creates new roles, updates existing roles, and activates repositories
- Use with caution in production environments

### Cortex Data-Source Discovery Modes

The synchronizer detects the configured Cortex data-source `selectionType` before updating repository selections.

- For `MANUAL_SELECTION`, missing repositories may be added with a manual `state` update.
- For auto-discovery modes such as `CURRENT_STATE_AND_FUTURE`, the synchronizer does not switch the data source to manual selection. It leaves Cortex auto-discovery unchanged and waits for Cortex to discover the repository.
- A newly created or recently activated GitLab project may not have a Cortex repository asset immediately. Run the synchronization again after Cortex finishes discovery.
- The synchronizer is additive; it does not remove repositories from a Cortex data source.

---

## Local Testing

The repository includes network-free tests for GitLab filtering and Cortex API behavior:

- [tests/test_gitlab_client.py](../tests/test_gitlab_client.py) tests required topics and topic filtering.
- [tests/test_cortex_client.py](../tests/test_cortex_client.py) tests Cortex headers, read-only lookups, repository selection payloads, role assignment payloads, and dry-run mutation protection.

Run the tests from the repository root:

```bash
python -m unittest discover -s tests -v
```

The tests use mocked HTTP responses. They do not require GitLab or Cortex credentials and do not contact external services.

Run syntax and configuration checks separately:

```bash
python -m py_compile gitlabPrismaSync.py client/cortex_client.py client/gitlab_client.py common/utils.py
python -m json.tool config/gitlab_instances_config.json
```

In `DRY_RUN`, read-only Cortex POST endpoints are allowed so users and roles can be inspected:

```http
POST /public_api/v1/rbac/get_users
POST /public_api/v1/rbac/get_roles
```

Cortex mutations remain blocked, including repository data-source updates, role creation, and user-role assignment.

## Customer Validation

Local tests cannot verify tenant-specific Cortex permissions, data-source IDs, repository response fields, or live role assignment. The customer should validate these steps in an isolated test environment:

1. Configure the correct `cortex-data-source-id` for the GitLab instance.
2. Confirm the Cortex API key can read users, roles, repositories, and data sources.
3. Confirm the configured `CORTEX_ROLE_COMPONENT_PERMISSIONS` values exist in the tenant.
4. Run with `RUN_MODE=DRY_RUN` and review the logs.
5. Test with one repository and one non-production Cortex user.
6. Run with `RUN_MODE=LIVE` only after the dry-run output is approved.
7. Verify the repository selection, generated role, and user assignment in Cortex.

Do not use production users or broad repository selections for the first live test.

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
- Loads GitLab and Cortex data source settings from the JSON config file
- Validates entry structure and required fields
- Raises a configuration error for missing files, invalid JSON, or incomplete entries

### `get_gitlab_config()`
- Resolves the selected entry from `GITLAB_CONFIG_KEY`
- Derives the GitLab API URL from the selected `url`
- Returns the Cortex data source ID and topic-filtering mode for the selected entry

### `initialize_clients(run_mode)`
- Creates GitLab API client with provided token
- Resolves GitLab and Cortex settings from the selected config entry
- Creates the Cortex Cloud client with API key ID and secret
- Sets the Cortex client to dry-run mode if `RUN_MODE=DRY_RUN`

### `fetch_active_projects(gitlab, top_n_projects, activity_threshold_minutes, run_mode)`
- Queries GitLab for active projects
- Filters based on activity in the last N minutes
- Limits results to top N projects
- Logs count of projects found

### `activate_repositories(cortex, projects, cortex_data_source_id)`
- Identifies GitLab repositories not selected in the Cortex data source
- Updates the Cortex data source `state` selection
- Returns a lookup table of Cortex repository asset IDs
- Respects `DRY_RUN` mode for preview

### `build_user_mapping(projects, gitlab, cortex_repo_lookup)`
- Extracts user information from GitLab projects
- Maps each user to their accessible repositories
- Uses parallel processing for performance
- Returns dictionary of user-to-repos mappings

### `create_or_update_user_roles(user_repos)`
- Creates Cortex roles with configured component permissions
- Assigns generated roles through Cortex RBAC
- Respects `DRY_RUN` mode for preview

### `summarize_role_sync(results)`
- Counts successful, failed, and skipped operations
- Logs warnings for skipped users (not found in Cortex)
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

- **`CortexClientError`**: Issues with Cortex Cloud API communication
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
export CORTEX_API_KEY_ID="api_key_id"
export CORTEX_API_SECRET="api_secret"
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
3. **Registration**: Select missing repos in the Cortex data source
4. **Mapping**: Build user-to-repository relationships
5. **Synchronization**: Create/assign Cortex Cloud roles
6. **Reporting**: Summarize results with success/failure counts

The entire process is logged for audit and troubleshooting purposes, with `DRY_RUN` mode available for safe preview before applying live changes.

---

## Recent Updates

### 2026-09-11

- Migrated the integration from Prisma Cloud to Cortex Cloud Application Security and Platform RBAC APIs.
- Added [client/cortex_client.py](../client/cortex_client.py) as the Cortex API client.
- Replaced Prisma authentication with Cortex `CORTEX_API_KEY_ID` and `CORTEX_API_SECRET` headers.
- Added Cortex data-source repository synchronization using:
  - `GET /public_api/appsec/v1/data_source_instances`
  - `GET /public_api/appsec/v1/repositories`
  - `PUT /public_api/appsec/v1/data_source_instances/{id}`
- Replaced Prisma integration IDs with `cortex-data-source-id` values in `config/gitlab_instances_config.json`.
- Updated the orchestrator to initialize `CortexClient`, select Cortex data sources, and use Cortex error handling.
- Added Cortex user discovery through `POST /public_api/v1/rbac/get_users`.
- Added Cortex role discovery through `POST /public_api/v1/rbac/get_roles`.
- Added generated per-user Cortex roles using `POST /platform/iam/v1/role` and `CORTEX_ROLE_COMPONENT_PERMISSIONS`.
- Added Cortex user-role assignment through `POST /public_api/v1/rbac/set_user_role`.
- Preserved GitLab project activity filtering, required `CAL_Barcode:` topics, optional `Unified-Prisma` filtering, visibility filtering, pagination, member filtering, and parallel user mapping.
- Preserved `DRY_RUN` and `LIVE` execution modes, while allowing read-only Cortex RBAC POST requests during `DRY_RUN` and blocking mutations.
- Added retry handling for transient Cortex responses including `429`, `500`, `502`, `503`, and `504`.
- Added `.env` and `.env.example` using Cortex configuration names and safe dry-run defaults.
- Added mocked, network-free tests in [tests/test_cortex_client.py](../tests/test_cortex_client.py) and [tests/test_gitlab_client.py](../tests/test_gitlab_client.py).
- Added documentation for local testing and customer-side dry-run/live validation.
- Removed the obsolete Prisma client implementation and updated the workspace custom agent to target GitLab-Cortex RBAC work.
- Documented the remaining limitation: Cortex repository asset IDs are collected, but per-user SBAC repository scopes still require tenant-specific scope criteria and are not automatically applied.

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
