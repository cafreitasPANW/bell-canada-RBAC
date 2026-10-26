import json
import os
import tempfile
import unittest
from unittest.mock import patch

from gitlabCortexSync import (
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
            "role_name": "Bell-Devex-General",
            "user_group_name": "Bell-Devex-Users",
            "asset_group_repository_type": "GITLAB_REPOSITORY",
            "asset_group_name_prefix": "gitlab-repo-",
        })
        with patch.dict(os.environ, {"CORTEX_RBAC_CONFIG_FILE": config_path}):
            permissions, repo_type, group_prefix, role_name, user_group = load_cortex_rbac_config("lab")

        self.assertEqual(permissions, ["app_sec_issues_view"])
        self.assertEqual(repo_type, "GITLAB_REPOSITORY")
        self.assertEqual(group_prefix, "gitlab-repo-")
        self.assertEqual(role_name, "Bell-Devex-General")
        self.assertEqual(user_group, "Bell-Devex-Users")

    def test_rejects_missing_user_group_name(self):
        config_path = self.write_config({
            "component_permissions": ["app_sec_issues_view"],
            "role_name": "Bell-Devex-General",
            "asset_group_repository_type": "GITLAB_REPOSITORY",
            "asset_group_name_prefix": "gitlab-repo-",
        })
        with patch.dict(os.environ, {"CORTEX_RBAC_CONFIG_FILE": config_path}):
            with self.assertRaises(EnvironmentValidationError):
                load_cortex_rbac_config("lab")

    def test_repository_config_uses_shared_view_only_role(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTEX_RBAC_CONFIG_FILE", None)
            permissions, _, _, role_name, user_group = load_cortex_rbac_config("lab")

        self.assertEqual(role_name, "Bell-Devex-General")
        self.assertEqual(user_group, "Bell-Devex-General")
        self.assertTrue(all(permission.endswith("_view") for permission in permissions))

    def test_rejects_missing_repository_type(self):
        config_path = self.write_config({
            "component_permissions": ["app_sec_issues_view"],
            "role_name": "Bell-Devex-General",
            "user_group_name": "Bell-Devex-General",
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
