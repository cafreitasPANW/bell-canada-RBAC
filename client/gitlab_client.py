"""GitLab API client for repository and user management."""

import os
import requests
from datetime import datetime, timedelta, timezone
from common.utils import parse_datetime
import re
from common.logger import LoggerFactory

logger = LoggerFactory.get_logger(__name__)

class GitLabClient:
    def __init__(self, api_url, token, use_topic_filtering=True, visibility=None):
        """Initialize GitLab client.
        
        Args:
            api_url (str): GitLab API URL (required)
            token (str): Personal access token for authentication (required)
            use_topic_filtering (bool): If True, enforce Unified-Prisma topic filtering
            visibility (str | None): Optional GitLab visibility filter to apply to project queries
        
        Raises:
            ValueError: If any required parameter is missing
        """
        if not all([api_url, token]):
            missing = []
            if not api_url:
                missing.append('api_url')
            if not token:
                missing.append('token')
            error_msg = (
                f"Missing required GitLab parameters: {', '.join(missing)}"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        self.api_url = api_url
        self.token = token
        self.use_topic_filtering = use_topic_filtering
        self.visibility = visibility
        self._user_cache: dict = {}  # Cache user details to avoid redundant API calls

    def get_project_members(self, project_id, include_inherited=False):
        """
        Get members of a project.
        If include_inherited is True, fetch all members including inherited ones.
        Traverses all pages to collect all members.
        Skips members whose 'username' or 'name' starts with 'project_' or 'group_'.
        Fetches individual user details to get email addresses.
        """
        url = f"{self.api_url}/projects/{project_id}/members"
        headers = {'PRIVATE-TOKEN': self.token}
        params = {'per_page': 100}
        if include_inherited:
            url += "/all"
        members = []
        page = 1
        logger.debug(f"Fetching members for project {project_id} (inherited: {include_inherited})")
        while True:
            params['page'] = page
            try:
                resp = requests.get(url, headers=headers, params=params)
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Error fetching members on page {page}: {e}")
                break
            data = resp.json()
            if not data:
                logger.debug(f"No data returned for page {page}")
                break
            for member in data:
                username = member.get('username', '')
                name = member.get('name', '')
                if username.startswith("project_") or username.startswith("group_") or \
                   name.startswith("project_") or name.startswith("group_"):
                    logger.debug(f"Skipping member: username={username}, name={name}")
                    continue
                
                # Fetch user details to get email address
                user_id = member.get('id')
                if user_id:
                    user_details = self._get_user_details(user_id)
                    if user_details and user_details.get('email'):
                        member['email'] = user_details.get('email')
                        member['public_email'] = user_details.get('public_email')
                
                members.append(member)
            # Check if there are more pages
            if 'X-Next-Page' in resp.headers and resp.headers['X-Next-Page']:
                logger.debug(f"Next page: {resp.headers['X-Next-Page']}")
                page = int(resp.headers['X-Next-Page'])
            else:
                logger.debug(f"Fetched all pages for project members.")
                break
        logger.debug(f"Total filtered members: {len(members)}")
        return members
    
    def _get_user_details(self, user_id):
        """
        Get detailed information for a specific user including email.
        Returns cached result if available, otherwise fetches from API and caches.
        """
        if user_id in self._user_cache:
            logger.debug(f"User {user_id} found in cache, skipping API call")
            return self._user_cache[user_id]
        url = f"{self.api_url}/users/{user_id}"
        headers = {'PRIVATE-TOKEN': self.token}
        try:
            resp = requests.get(url, headers=headers)
            resp.raise_for_status()
            user_data = resp.json()
            self._user_cache[user_id] = user_data
            return user_data
        except Exception as e:
            logger.debug(f"Error fetching user details for user {user_id}: {e}")
            self._user_cache[user_id] = None  # Cache failures to avoid retrying
            return None

    def get_active_projects(self, top_n=10, last_minutes=None):
        """
        Fetch the top N active projects sorted by last activity.
        Navigates pages until top_n is reached or no more data.
        If last_minutes is provided, only include projects active in the last X minutes.
        Applies the activity cutoff server-side when possible to reduce payload size.
        Always requires topics containing 'CAL_Barcode:' with length 15 or more.
        Also requires topic 'Unified-Prisma' when use_topic_filtering is enabled.
        Handles both Z and timezone offset formats.
        """
        url = f"{self.api_url}/projects"
        headers = {'PRIVATE-TOKEN': self.token}
        per_page = 100 if top_n > 100 else top_n
        params = {
            'order_by': 'last_activity_at',
            'sort': 'desc',
            'per_page': per_page,
            'archived': 'false',
        }
        if self.use_topic_filtering:
            params['topic'] = "Unified-Prisma"
        if self.visibility:
            params['visibility'] = self.visibility
        logger.debug(f"Fetching up to {top_n} active projects")
        projects = []
        page = 1
        fetched = 0

        # Determine cutoff time
        cutoff = None
        if last_minutes is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=last_minutes)
            params['last_activity_after'] = cutoff.isoformat()
            logger.info(f"Current time: {datetime.now(timezone.utc)}, Cutoff time: {cutoff}")
            logger.debug(
                f"Applying server-side last_activity_after filter: {params['last_activity_after']}"
            )

        while fetched < top_n:
            params['page'] = page
            try:
                logger.debug(f"Url: {url} | params: {params}")
                resp = requests.get(url, headers=headers, params=params)
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Error fetching active projects on page {page}: {e}")
                break
            data = resp.json()
            logger.debug(f"Fetched active projects in this scan: {len(data)}")
            logger.debug(f"Data set : {data}")
            if not data:
                logger.debug(f"No data returned for page {page}")
                break
            for project in data:
                # Always enforce CAL_BARCODE topic requirement.
                topics = project.get('topics', [])
                logger.debug(f"Checking {project.get('path_with_namespace')} project ({project.get('id')}) topics: {topics}")
                has_cal_barcode = any((topic.upper().startswith("CAL_BARCODE:") and len(topic) >= 15) for topic in topics)
                if not has_cal_barcode:
                    logger.debug(f"Skipping project {project.get('id')} - missing required topics. {project.get('web_url')}")
                    continue
                projects.append(project)
                fetched += 1
                if fetched >= top_n:
                    break
            if len(data) < per_page or fetched >= top_n:
                logger.debug(f"Fetched {len(projects)} active projects (limit or no more data).")
                break
            page += 1
        logger.debug(f"Total active projects returned: {len(projects)}")
        return projects

    def add_project_member(self, project_id, user_id=2657, access_level=20):
        """
        Add a member to a GitLab project.
        access_level: 10 (Guest), 20 (Reporter), 30 (Developer), 40 (Maintainer), 50 (Owner)
        """
        logger.debug(f"add_project_member called but POST action is disabled (project_id={project_id}, user_id={user_id}, access_level={access_level})")
        return None

    def get_pipelines_for_date(self, project_id, date):
        """
        Get all pipelines for a project for a specific date.
        Returns a list of pipelines.
        """
        url = f"{self.api_url}/projects/{project_id}/pipelines"
        headers = {'PRIVATE-TOKEN': self.token}
        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
        params = {
            'updated_after': start.isoformat(),
            'updated_before': end.isoformat(),
            'per_page': 100
        }
        pipelines = []
        page = 1
        while True:
            params['page'] = page
            try:
                resp = requests.get(url, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                pipelines.extend(data)
                if 'X-Next-Page' in resp.headers and resp.headers['X-Next-Page']:
                    page = int(resp.headers['X-Next-Page'])
                else:
                    break
            except Exception as e:
                logger.error(f"Error fetching pipelines for project {project_id} on page {page}: {e}")
                break
        return pipelines

    def get_jobs_for_pipeline(self, project_id, pipeline_id):
        """
        Get all jobs for a given pipeline in a project.
        Returns a list of jobs.
        """
        url = f"{self.api_url}/projects/{project_id}/pipelines/{pipeline_id}/jobs"
        headers = {'PRIVATE-TOKEN': self.token}
        jobs = []
        page = 1
        params = {'per_page': 100}
        while True:
            params['page'] = page
            try:
                resp = requests.get(url, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                jobs.extend(data)
                if 'X-Next-Page' in resp.headers and resp.headers['X-Next-Page']:
                    page = int(resp.headers['X-Next-Page'])
                else:
                    break
            except Exception as e:
                logger.error(f"Error fetching jobs for pipeline {pipeline_id} in project {project_id} on page {page}: {e}")
                break
        return jobs

    def get_active_projects_for_date(self, date, top_n=None):
        """
        Fetch active projects for a specific day.
        Only includes projects with topic 'Unified-Sonarqube' and last_activity_at within the given date.
        If top_n is None, returns all matching projects.
        """
        url = f"{self.api_url}/projects"
        headers = {'PRIVATE-TOKEN': self.token}
        per_page = 100
        params = {
            'order_by': 'last_activity_at',
            'sort': 'desc',
            'per_page': per_page,
            'last_activity_after' : date.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            'last_activity_before' : date.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
        }
        projects = []
        page = 1
        fetched = 0

        # Calculate start and end of the day
        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = date.replace(hour=23, minute=59, second=59, microsecond=999999)

        while True:
            params['page'] = page
            try:
                logger.debug(f"Fetching active projects on page {page} for date {date.date()}")
                resp = requests.get(url, headers=headers, params=params)
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Error fetching active projects on page {page}: {e}")
                break
            data = resp.json()
            if not data:
                break
            for project in data:
                last_activity = project.get('last_activity_at')
                if last_activity:
                    try:
                        dt = parse_datetime(last_activity)
                        if dt >= start and dt <= end:
                            projects.append(project)
                            fetched += 1
                            if top_n is not None and fetched >= top_n:
                                logger.debug(f"Top {top_n} active projects for {date.date()}: {len(projects)}")
                                return projects
                    except Exception:
                        logger.warning(f"Skipping project {project.get('id')} due to datetime parsing error.")
                        continue
            if len(data) < per_page:
                break
            page += 1
        logger.debug(f"Total active projects for {date.date()}: {len(projects)}")
        return projects

    def search_patterns_in_raw_file(self, project_id, file_path, ref, patterns):
        """
        Reads a raw file from a GitLab project at a given ref and searches for any of the provided patterns.
        Dumps the file content in a 'dump' folder with filename as project_id.
        Returns a dict with pattern as key and list of matching lines as value.
        If a pattern contains ';', all sub-patterns must be present in the line.
        """
        url = f"{self.api_url}/projects/{project_id}/repository/files/{requests.utils.quote(file_path, safe='')}/raw"
        headers = {'PRIVATE-TOKEN': self.token}
        params = {'ref': ref}
        try:
            resp = requests.get(url, headers=headers, params=params)
            resp.raise_for_status()
            content = resp.text
        except Exception as e:
            logger.error(f"Error reading raw file '{file_path}' from project {project_id} at ref '{ref}': {e}")
            return {}

        # Dump the file content
        dump_dir = os.path.join(os.getcwd(), "dump")
        os.makedirs(dump_dir, exist_ok=True)
        dump_path = os.path.join(dump_dir, f"{project_id}.yml")
        try:
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.debug(f"Dumped raw file for project {project_id} to {dump_path}")
        except Exception as e:
            logger.error(f"Error dumping raw file for project {project_id}: {e}")

        results = {pattern: [] for pattern in patterns}
        for line in content.splitlines():
            for pattern in patterns:
                sub_patterns = pattern.split(';')
                if all(re.search(sub_pat, line) for sub_pat in sub_patterns):
                    results[pattern].append(line)
        return results

