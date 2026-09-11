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


if __name__ == "__main__":
    unittest.main()
