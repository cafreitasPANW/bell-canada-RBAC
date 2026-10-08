import unittest
from unittest.mock import Mock

from client.cortex_client import CortexClientError
from gitlabPrismaSync import projects_ready_for_repository_selection


class RepositorySelectionReadinessTests(unittest.TestCase):
    def test_requires_asset_group_and_successful_member_scopes(self):
        cortex = Mock()
        cortex.ensure_repository_asset_group.side_effect = [101, 102, CortexClientError("group unavailable")]
        projects = [
            {"path_with_namespace": "group/ready"},
            {"path_with_namespace": "group/scope-failed"},
            {"path_with_namespace": "group/group-failed"},
        ]
        user_repos = {
            "ready-user": {"repositories": [{"repo": "group/ready"}]},
            "failed-user": {"repositories": [{"repo": "group/scope-failed"}]},
        }
        role_results = {
            "ready-user": {"success": True},
            "failed-user": {"success": False, "reason": "scope update failed"},
        }

        ready = projects_ready_for_repository_selection(
            cortex, projects, user_repos, role_results
        )

        self.assertEqual(ready, [projects[0]])

    def test_administrator_skip_does_not_block_project_selection(self):
        cortex = Mock()
        cortex.ensure_repository_asset_group.return_value = 101
        projects = [{"path_with_namespace": "group/repo"}]
        user_repos = {
            "admin": {"repositories": [{"repo": "group/repo"}]},
        }
        role_results = {
            "admin": {
                "success": False,
                "reason": "Administrator users cannot receive automated SBAC scopes",
            },
        }

        ready = projects_ready_for_repository_selection(
            cortex, projects, user_repos, role_results
        )

        self.assertEqual(ready, projects)


if __name__ == "__main__":
    unittest.main()