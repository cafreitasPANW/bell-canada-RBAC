import unittest
from unittest.mock import patch

import requests

from client.gitlab_client import GitLabClient


class GitLabClientTests(unittest.TestCase):
    @staticmethod
    def response(payload, headers=None):
        response = requests.Response()
        response.status_code = 200
        response._content = __import__("json").dumps(payload).encode("utf-8")
        response.headers.update(headers or {})
        return response

    @patch("client.gitlab_client.requests.get")
    def test_active_projects_require_barcode_topic(self, get):
        get.return_value = self.response([
            {
                "id": 1,
                "path_with_namespace": "group/accepted",
                "topics": ["CAL_Barcode:1234567890"],
            },
            {
                "id": 2,
                "path_with_namespace": "group/rejected",
                "topics": ["Unified-Prisma"],
            },
        ])
        client = GitLabClient(
            "https://gitlab.example.test/api/v4",
            "token",
            use_topic_filtering=False,
        )

        projects = client.get_active_projects(top_n=10)

        self.assertEqual([project["id"] for project in projects], [1])
        self.assertEqual(get.call_args.kwargs["params"]["archived"], "false")

    @patch("client.gitlab_client.requests.get")
    def test_topic_filtering_is_sent_to_gitlab(self, get):
        get.return_value = self.response([])
        client = GitLabClient(
            "https://gitlab.example.test/api/v4",
            "token",
            use_topic_filtering=True,
        )

        client.get_active_projects(top_n=10)

        self.assertEqual(get.call_args.kwargs["params"]["topic"], "Unified-Prisma")


if __name__ == "__main__":
    unittest.main()
