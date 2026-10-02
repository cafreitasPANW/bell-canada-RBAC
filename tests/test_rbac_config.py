import json
import os
import tempfile
import unittest
from unittest.mock import patch

from gitlabPrismaSync import (
    EnvironmentValidationError,
    get_gitlab_config,
    load_cortex_rbac_config,
)


class CortexRbacConfigTests(unittest.TestCase):
    def write_config(self, payload):
        temporary = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        with temporary:
            json.dump(payload, temporary)
        self.addCleanup(lambda: os.path.exists(temporary.name) and os.unlink(temporary.name))
        return temporary.name

    def test_loads_permissions_and_dynamic_asset_group_settings(self):
        config_path = self.write_config({
            "component_permissions": ["app_sec_issues_view"],
            "role_name_prefix": "lab_devex_",
            "asset_group_repository_type": "GITLAB_REPOSITORY",
            "asset_group_name_prefix": "gitlab-repo-",
        })
        with patch.dict(os.environ, {"CORTEX_RBAC_CONFIG_FILE": config_path}):
            permissions, repo_type, group_prefix, role_prefix = load_cortex_rbac_config("lab")

        self.assertEqual(permissions, ["app_sec_issues_view"])
        self.assertEqual(repo_type, "GITLAB_REPOSITORY")
        self.assertEqual(group_prefix, "gitlab-repo-")
        self.assertEqual(role_prefix, "lab_devex_")

    def test_rejects_missing_repository_type(self):
        config_path = self.write_config({
            "component_permissions": ["app_sec_issues_view"],
            "role_name_prefix": "devex_",
            "asset_group_repository_type": "",
            "asset_group_name_prefix": "gitlab-repo-",
        })
        with patch.dict(os.environ, {"CORTEX_RBAC_CONFIG_FILE": config_path}):
            with self.assertRaises(EnvironmentValidationError):
                load_cortex_rbac_config("lab")

    def test_gitlab_config_returns_selected_data_source_settings(self):
        config_path = self.write_config({
            "lab": {
                "url": "https://gitlab.example.test/",
                "cortex-data-source-id": "source-123",
                "use-topic-filtering": True,
                "visibility": "private",
            }
        })
        with patch.dict(os.environ, {
            "GITLAB_INSTANCE_CONFIG_FILE": config_path,
            "GITLAB_CONFIG_KEY": "lab",
        }):
            selected = get_gitlab_config()

        self.assertEqual(selected, (
            "https://gitlab.example.test/api/v4",
            "source-123",
            True,
            "private",
            "lab",
        ))


if __name__ == "__main__":
    unittest.main()
