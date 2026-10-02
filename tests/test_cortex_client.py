import json
import unittest
from unittest.mock import Mock, patch

import requests

from client.cortex_client import CortexClient


class CortexClientTests(unittest.TestCase):
    def setUp(self):
        self.client = CortexClient(
            api_url="https://cortex.example.test",
            access_key="key-id",
            secret_key="secret",
            integration_id="data-source-id",
            dry_run=True,
        )

    @staticmethod
    def response(payload, status_code=200):
        response = requests.Response()
        response.status_code = status_code
        response._content = json.dumps(payload).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        return response

    @patch("client.cortex_client.requests.request")
    def test_dry_run_allows_read_only_user_lookup(self, request):
        request.return_value = self.response({"data": [{"email": "user@example.com", "enabled": True}]})

        users = self.client.fetch_cortex_users_lookup()

        self.assertIn("user@example.com", users)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("POST", "https://cortex.example.test/public_api/v1/rbac/get_users"))
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "secret")
        self.assertEqual(request.call_args.kwargs["headers"]["x-xdr-auth-id"], "key-id")

    @patch("client.cortex_client.requests.request")
    def test_user_lookup_handles_reply_wrapper_and_active_status(self, request):
        request.return_value = self.response({
            "reply": {
                "users": [
                    {"user_email": "active@example.com", "status": "ACTIVE"},
                    {"user_email": "disabled@example.com", "status": "DISABLED"},
                ]
            }
        })

        users = self.client.fetch_cortex_users_lookup()

        self.assertEqual(list(users), ["active@example.com"])

    @patch("client.cortex_client.requests.request")
    def test_user_lookup_handles_user_list_wrapper(self, request):
        request.return_value = self.response({
            "reply": {"user_list": [{"email": "active@example.com", "enabled": True}]}
        })

        users = self.client.fetch_cortex_users_lookup()

        self.assertIn("active@example.com", users)

    @patch("client.cortex_client.requests.request")
    def test_user_lookup_handles_reply_list_from_public_api(self, request):
        request.return_value = self.response({
            "reply": [{
                "user_email": "active@example.com",
                "user_first_name": "Active",
                "user_last_name": "User",
                "role_name": "viewer",
                "user_type": "LOCAL",
                "groups": [],
                "scope": [],
            }]
        })

        users = self.client.fetch_cortex_users_lookup()

        self.assertIn("active@example.com", users)

    @patch("client.cortex_client.requests.request")
    def test_dry_run_skips_role_assignment_mutation(self, request):
        assigned = self.client.set_user_role("user@example.com", "devex_user")

        self.assertTrue(assigned)
        request.assert_not_called()

    @patch("client.cortex_client.requests.request")
    def test_repository_selection_update_payload(self, request):
        request.side_effect = [
            self.response({"data": [{"id": "repo-1", "name": "group/project-a"}]}),
            self.response({"data": [{"id": "data-source-id", "state": ["group/project-a"]}]}),
        ]
        projects = [{"path_with_namespace": "group/project-a"}, {"path_with_namespace": "group/project-b"}]

        lookup = self.client.activate_missing_repos_integration(projects, "data-source-id")

        self.assertEqual(lookup["group/project-a"]["id"], "repo-1")
        self.assertTrue(lookup["group/project-a"]["is_new"] is False)
        request.assert_any_call(
            "GET",
            "https://cortex.example.test/public_api/appsec/v1/repositories",
            headers=unittest.mock.ANY,
            json=None,
            params=None,
            timeout=120,
        )
        self.assertEqual(request.call_count, 2)

    @patch("client.cortex_client.requests.request")
    def test_role_assignment_uses_cortex_payload(self, request):
        client = CortexClient(
            api_url="https://cortex.example.test",
            access_key="key-id",
            secret_key="secret",
            integration_id="data-source-id",
            dry_run=False,
        )
        request.return_value = self.response({"reply": {"update_count": "1"}})

        client.set_user_role("user@example.com", "devex_user")

        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload, {
            "request_data": {
                "user_emails": ["user@example.com"],
                "role_name": "devex_user",
            }
        })
        self.assertEqual(request.call_args.args[0], "POST")
        self.assertTrue(request.call_args.args[1].endswith("/public_api/v1/rbac/set_user_role"))

    @patch("client.cortex_client.requests.request")
    def test_user_asset_scope_uses_cortex_scope_schema(self, request):
        client = CortexClient(
            api_url="https://cortex.example.test",
            access_key="key-id",
            secret_key="secret",
            integration_id="data-source-id",
            dry_run=False,
        )
        request.return_value = self.response({"data": {"message": "ok"}})

        client.update_user_asset_scope("user@example.com", [101, 202])

        self.assertEqual(request.call_args.args[0], "PUT")
        self.assertEqual(
            request.call_args.args[1],
            "https://cortex.example.test/platform/iam/v1/scope/user/user%40example.com",
        )
        self.assertEqual(request.call_args.kwargs["json"], {
            "request_data": {
                "assets": {
                    "mode": "scope",
                    "asset_group_ids": [101, 202],
                }
            }
        })

    @patch("client.cortex_client.requests.request")
    def test_role_creation_uses_configured_permissions(self, request):
        client = CortexClient(
            api_url="https://cortex.example.test",
            access_key="key-id",
            secret_key="secret",
            integration_id="data-source-id",
            dry_run=False,
            component_permissions=["app_sec_issues_view", "app_sec_scans_ci_cd_view"],
        )
        request.side_effect = [
            self.response({"data": {"message": "created"}}),
            self.response({"reply": [{"pretty_name": "devex_user", "role_id": "role-1"}]}),
        ]

        role_id = client.create_custom_role("devex_user", "GitLab repository access")

        self.assertEqual(request.call_args_list[0].kwargs["json"], {
            "request_data": {
                "pretty_name": "devex_user",
                "description": "GitLab repository access",
                "component_permissions": [
                    "app_sec_issues_view",
                    "app_sec_scans_ci_cd_view",
                ],
            }
        })
        self.assertEqual(role_id, "role-1")

    @patch("client.cortex_client.requests.request")
    def test_get_roles_includes_role_names(self, request):
        request.return_value = self.response({"reply": {"roles": []}})

        self.client.get_custom_roles(["devex_user"])

        self.assertEqual(request.call_args.kwargs["json"], {
            "request_data": {"role_names": ["devex_user"]}
        })

    def test_empty_user_mapping_skips_role_lookup(self):
        with patch.object(self.client, "get_custom_roles") as get_roles:
            self.assertEqual(self.client.create_or_update_user_roles({}), {})
            get_roles.assert_not_called()

    @patch("client.cortex_client.requests.request")
    def test_ensure_repository_asset_group_creates_dynamic_group(self, request):
        self.client.dry_run = False
        request.side_effect = [
            self.response([{"reply": {"data": [], "metadata": {"total_count": 0}}}]),
            self.response([{"reply": {"data": {"success": True, "asset_group_id": 987}}}]),
        ]

        group_id = self.client.ensure_repository_asset_group("group/repo-x")

        self.assertEqual(group_id, 987)
        create_call = request.call_args_list[1]
        group = create_call.kwargs["json"]["request_data"]["asset_group"]
        self.assertEqual(group["group_type"], "Dynamic")
        self.assertEqual(group["membership_predicate"], {
            "AND": [
                {
                    "SEARCH_FIELD": "xdm.asset.type.id",
                    "SEARCH_TYPE": "EQ",
                    "SEARCH_VALUE": "GITLAB_REPOSITORY",
                },
                {
                    "SEARCH_FIELD": "xdm.asset.name",
                    "SEARCH_TYPE": "EQ",
                    "SEARCH_VALUE": "group/repo-x",
                },
            ]
        })

    @patch("client.cortex_client.requests.request")
    def test_ensure_repository_asset_group_reuses_existing_group(self, request):
        self.client.dry_run = False
        group_name = self.client._repository_asset_group_name("group/repo-x")
        request.return_value = self.response([{
            "reply": {
                "data": [{"group_name": group_name, "group_id": 987}],
                "metadata": {"total_count": 1},
            }
        }])

        self.assertEqual(self.client.ensure_repository_asset_group("group/repo-x"), 987)
        request.assert_called_once()

    def test_role_sync_scopes_user_to_all_member_repo_groups_before_assignment(self):
        client = CortexClient(
            api_url="https://cortex.example.test",
            access_key="key-id",
            secret_key="secret",
            integration_id="data-source-id",
            dry_run=False,
            component_permissions=["app_sec_issues_view"],
            asset_group_repository_type="GITLAB_REPOSITORY",
            gitlab_key="lab",
        )
        client.fetch_cortex_users_lookup = Mock(return_value={"dev@example.com": {}})
        client.get_custom_roles = Mock(return_value=[{"pretty_name": "devex_dev", "role_id": "role-1"}])
        client.ensure_repository_asset_group = Mock(
            side_effect=lambda path: {"group/repo-x": 11, "group/repo-y": 22}[path]
        )
        calls = []
        client.update_user_asset_scope = Mock(
            side_effect=lambda email, ids: calls.append(("scope", email, ids)) or True
        )
        client.set_user_role = Mock(
            side_effect=lambda email, name: calls.append(("role", email, name)) or True
        )
        user_repos = {
            "dev": {
                "email": "dev@example.com",
                "repositories": [
                    {"repo": "group/repo-x"},
                    {"repo": "group/repo-y"},
                ],
            }
        }

        results = client.create_or_update_user_roles(user_repos)

        self.assertTrue(results["dev"]["success"])
        self.assertEqual(calls, [
            ("scope", "dev@example.com", [11, 22]),
            ("role", "dev@example.com", "devex_dev"),
        ])

    def test_role_name_uses_configured_prefix(self):
        client = CortexClient(
            api_url="https://cortex.example.test",
            access_key="key-id",
            secret_key="secret",
            integration_id="data-source-id",
            role_name_prefix="lab_devex_",
        )

        self.assertEqual(client._generate_role_name("alice@example.com"), "lab_devex_alice")

    @patch("client.cortex_client.requests.request")
    def test_unresolved_gitlab_repo_group_prevents_role_and_scope_assignment(self, request):
        self.client.gitlab_key = "lab"
        with patch.object(self.client, "fetch_cortex_users_lookup", return_value={"dev@example.com": {}}), \
             patch.object(self.client, "get_custom_roles", return_value=[]), \
             patch.object(self.client, "ensure_repository_asset_group", side_effect=[11, None]), \
             patch.object(self.client, "create_custom_role") as create_role, \
             patch.object(self.client, "update_user_asset_scope") as update_scope, \
             patch.object(self.client, "set_user_role") as assign_role:
            results = self.client.create_or_update_user_roles({
                "dev": {
                    "email": "dev@example.com",
                    "repositories": [
                        {"repo": "group/repo-x"},
                        {"repo": "group/repo-unmapped"},
                    ],
                }
            })

        self.assertFalse(results["dev"]["success"])
        self.assertIn("group/repo-unmapped", results["dev"]["reason"])
        create_role.assert_not_called()
        update_scope.assert_not_called()
        assign_role.assert_not_called()
        request.assert_not_called()

    @patch("client.cortex_client.requests.request")
    def test_auto_discovery_switches_to_manual_and_adds_repo_one_at_a_time(self, request):
        self.client.dry_run = False
        request.side_effect = [
            self.response({"data": [{"id": "repo-1", "name": "group/project-a"}]}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "CURRENT_STATE_AND_FUTURE",
                "state": ["group/project-a"],
            }]}),
            self.response({}),
            self.response({}),
            self.response({}),
            self.response({"data": [
                {"id": "repo-1", "name": "group/project-a"},
                {"id": "repo-2", "name": "group/project-b"},
            ]}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "MANUAL_SELECTION",
                "state": ["group/project-a", "group/project-b"],
            }]}),
        ]

        lookup = self.client.activate_missing_repos_integration(
            [
                {"path_with_namespace": "group/project-b"},
                {"path_with_namespace": "group/project-c"},
            ],
            "data-source-id",
        )

        self.assertEqual(request.call_count, 7)
        self.assertEqual(request.call_args_list[2].kwargs["json"], {
            "selectionType": "MANUAL_SELECTION",
            "state": ["group/project-a"],
        })
        self.assertEqual(request.call_args_list[3].kwargs["json"], {
            "selectionType": "MANUAL_SELECTION",
            "state": ["group/project-a", "group/project-b"],
        })
        self.assertEqual(request.call_args_list[4].kwargs["json"], {
            "selectionType": "MANUAL_SELECTION",
            "state": ["group/project-a", "group/project-b", "group/project-c"],
        })

    @patch("client.cortex_client.requests.request")
    def test_auto_discovery_is_switched_even_when_no_repos_are_new(self, request):
        self.client.dry_run = False
        request.side_effect = [
            self.response({"data": [{"id": "repo-1", "name": "group/project-a"}]}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "CURRENT_STATE_AND_FUTURE",
                "state": ["group/project-a"],
            }]}),
            self.response({}),
        ]

        self.client.activate_missing_repos_integration(
            [{"path_with_namespace": "group/project-a"}],
            "data-source-id",
        )

        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[2].kwargs["json"], {
            "selectionType": "MANUAL_SELECTION",
            "state": ["group/project-a"],
        })

    @patch("client.cortex_client.requests.request")
    def test_manual_update_preserves_cortex_state_identifiers(self, request):
        self.client.dry_run = False
        request.side_effect = [
            self.response({"data": [{"id": "repo-1", "name": "project-a"}]}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "MANUAL_SELECTION",
                "state": ["external-project-a"],
            }]}),
            self.response({}),
            self.response({"data": [{"id": "repo-1", "name": "project-a"}]}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "MANUAL_SELECTION",
                "state": ["external-project-a", "group/project-b"],
            }]}),
        ]

        self.client.activate_missing_repos_integration(
            [{"path_with_namespace": "group/project-b"}],
            "data-source-id",
        )

        update_call = request.call_args_list[2]
        self.assertEqual(update_call.kwargs["json"], {
            "selectionType": "MANUAL_SELECTION",
            "state": ["external-project-a", "group/project-b"],
        })

    @patch("client.cortex_client.requests.request")
    def test_empty_manual_state_does_not_mean_all_repositories_selected(self, request):
        self.client.dry_run = False
        request.side_effect = [
            self.response({"data": [{"id": "repo-1", "name": "group/project-a"}]}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "MANUAL_SELECTION",
                "state": [],
            }]}),
            self.response({}),
            self.response({"data": [{"id": "repo-1", "name": "group/project-a"}]}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "MANUAL_SELECTION",
                "state": ["group/project-a"],
            }]}),
        ]

        result = self.client.activate_missing_repos_integration(
            [{"path_with_namespace": "group/project-a"}],
            "data-source-id",
        )

        self.assertEqual(result, {"group/project-a": {"id": "repo-1", "is_new": True}})
        self.assertEqual(request.call_count, 5)
        self.assertEqual(request.call_args_list[2].args[0], "PUT")
        self.assertEqual(request.call_args_list[2].kwargs["json"], {
            "selectionType": "MANUAL_SELECTION",
            "state": ["group/project-a"],
        })
        self.assertEqual(request.call_args_list[3].args[0], "GET")

    @patch("client.cortex_client.requests.request")
    def test_selected_repo_is_returned_before_appsec_asset_is_indexed(self, request):
        self.client.dry_run = False
        request.side_effect = [
            self.response({"data": []}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "MANUAL_SELECTION",
                "state": [],
            }]}),
            self.response({}),
            self.response({"data": []}),
            self.response({"data": [{
                "id": "data-source-id",
                "selectionType": "MANUAL_SELECTION",
                "state": ["group/new-repo"],
            }]}),
        ]

        lookup = self.client.activate_missing_repos_integration(
            [{"path_with_namespace": "group/new-repo"}],
            "data-source-id",
        )

        self.assertEqual(lookup, {
            "group/new-repo": {"id": None, "is_new": True}
        })


if __name__ == "__main__":
    unittest.main()
