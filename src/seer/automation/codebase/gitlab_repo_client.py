"""
GitLab repository client implementation.

This module provides GitLab-specific implementation of the BaseRepoClient interface,
enabling Merge Request creation and repository operations for GitLab repositories.
"""

import functools
import logging
import os
import tempfile
from typing import Any

import gitlab
import sentry_sdk
from gitlab.v4.objects import Project

from seer.automation.autofix.utils import generate_random_string, sanitize_branch_name
from seer.automation.codebase.base_repo_client import (
    BaseRepoClient,
    BranchRefResult,
    PullRequestResult,
    RepoClientType,
)
from seer.automation.codebase.models import GitLabMrReviewComment
from seer.automation.codebase.utils import get_all_supported_extensions
from seer.automation.models import FileChange, FilePatch, InitializationError, RepoDefinition
from seer.automation.utils import decode_raw_data
from seer.configuration import AppConfig
from seer.dependency_injection import inject, injected

logger = logging.getLogger(__name__)


@inject
def get_gitlab_token(config: AppConfig = injected) -> str | None:
    """Get the GitLab API token from configuration."""
    return config.GITLAB_TOKEN


@inject
def get_gitlab_instance_url(config: AppConfig = injected) -> str:
    """Get the GitLab instance URL from configuration."""
    return config.GITLAB_INSTANCE_URL


class GitLabRepoClient(BaseRepoClient):
    """
    GitLab-specific implementation of the repository client.
    Provides access to GitLab repositories via the GitLab API.
    """

    gitlab_client: gitlab.Gitlab
    project: Project

    supported_providers = ["gitlab"]

    @sentry_sdk.trace
    def __init__(self, token: str | None, repo_definition: RepoDefinition):
        """
        Initialize the GitLab repo client.

        Args:
            token: GitLab API token for authentication.
            repo_definition: Definition of the repository to work with.
        """
        if repo_definition.provider != "gitlab":
            raise InitializationError(
                f"GitLabRepoClient only supports 'gitlab' provider, got: {repo_definition.provider}"
            )

        if not token:
            raise InitializationError(
                "No GitLab token provided. Please set GITLAB_TOKEN in configuration."
            )

        instance_url = get_gitlab_instance_url()
        self.gitlab_client = gitlab.Gitlab(instance_url, private_token=token)

        # Get project - GitLab supports both numeric IDs and path-based IDs (owner/name)
        try:
            with sentry_sdk.start_span(
                op="gitlab_repo_client.project.get", description=repo_definition.full_name
            ):
                # Try by full name first
                self.project = self.gitlab_client.projects.get(repo_definition.full_name)
        except gitlab.exceptions.GitlabGetError:
            logger.warning(
                f"Could not get project by full name {repo_definition.full_name}, "
                f"trying by external_id {repo_definition.external_id}"
            )
            try:
                with sentry_sdk.start_span(
                    op="gitlab_repo_client.project.get_by_id",
                    description=repo_definition.external_id,
                ):
                    self.project = self.gitlab_client.projects.get(repo_definition.external_id)
            except gitlab.exceptions.GitlabGetError as e:
                logger.exception(
                    f"Error getting GitLab project {repo_definition.full_name} "
                    f"or {repo_definition.external_id}"
                )
                raise e

        self.provider = repo_definition.provider
        self.repo_owner = repo_definition.owner
        self.repo_name = repo_definition.name
        self.repo_external_id = repo_definition.external_id
        self.base_branch = repo_definition.branch_name or self.get_default_branch()
        self.base_commit_sha = repo_definition.base_commit_sha or self.get_branch_head_sha(
            self.base_branch
        )
        self.repo_definition = repo_definition

        # Set up caching for expensive operations
        self.get_valid_file_paths = functools.lru_cache(maxsize=8)(self._get_valid_file_paths)
        self.get_commit_history = functools.lru_cache(maxsize=16)(self._get_commit_history)
        self.get_commit_patch_for_file = functools.lru_cache(maxsize=16)(
            self._get_commit_patch_for_file
        )

    @staticmethod
    def check_repo_write_access(repo: RepoDefinition) -> bool | None:
        """
        Check if the client has write access to the repository.

        Args:
            repo: Repository definition to check.

        Returns:
            True if write access is available, False if not, None if unable to check.
        """
        token = get_gitlab_token()
        if not token:
            return None

        try:
            instance_url = get_gitlab_instance_url()
            gl = gitlab.Gitlab(instance_url, private_token=token)
            project = gl.projects.get(repo.full_name)

            # Check access level - Developer (30) or higher can push
            # Maintainer (40) or higher can create MRs to protected branches
            access_level = project.permissions.get("project_access", {}).get("access_level", 0)
            group_access = project.permissions.get("group_access", {}).get("access_level", 0)
            max_access = max(access_level or 0, group_access or 0)

            # Developer level (30) or higher has write access
            return max_access >= 30
        except Exception:
            logger.exception(f"Error checking GitLab write access for {repo.full_name}")
            return None

    @staticmethod
    def check_repo_read_access(repo: RepoDefinition) -> bool | None:
        """
        Check if the client has read access to the repository.

        Args:
            repo: Repository definition to check.

        Returns:
            True if read access is available, False if not, None if unable to check.
        """
        token = get_gitlab_token()
        if not token:
            return None

        try:
            instance_url = get_gitlab_instance_url()
            gl = gitlab.Gitlab(instance_url, private_token=token)
            project = gl.projects.get(repo.full_name)

            # If we can get the project, we have at least read access
            return project is not None
        except gitlab.exceptions.GitlabGetError:
            return False
        except Exception:
            logger.exception(f"Error checking GitLab read access for {repo.full_name}")
            return None

    @classmethod
    @functools.lru_cache(maxsize=8)
    def from_repo_definition(cls, repo_def: RepoDefinition, type: RepoClientType):
        """
        Factory method to create a GitLab repo client from a repo definition.

        Args:
            repo_def: Definition of the repository.
            type: Type of client access needed (read, write, etc.).

        Returns:
            An instance of GitLabRepoClient.
        """
        # For now, we use the same token for all access types
        # In the future, we might want to support different tokens for different access levels
        token = get_gitlab_token()
        return cls(token, repo_def)

    def get_default_branch(self) -> str:
        """
        Get the default branch name for the repository.

        Returns:
            The name of the default branch.
        """
        return self.project.default_branch

    def get_branch_head_sha(self, branch: str) -> str:
        """
        Get the head commit SHA for a branch.

        Args:
            branch: Branch name.

        Returns:
            The SHA of the head commit on the branch.
        """
        branch_obj = self.project.branches.get(branch)
        return branch_obj.commit["id"]

    def get_file_content(self, path: str, sha: str | None = None) -> tuple[str | None, str]:
        """
        Get the content of a file at a specific commit.

        Args:
            path: Path to the file in the repository.
            sha: Commit SHA to get the file from. Defaults to base_commit_sha.

        Returns:
            Tuple of (file_content, encoding). Content is None if file doesn't exist.
        """
        logger.debug(f"Getting file contents for {path} in {self.repo_full_name} on sha {sha}")
        if sha is None:
            sha = self.base_commit_sha

        # Normalize the path by removing leading slashes
        if path.startswith("/"):
            path = path[1:]
        if path.startswith("./"):
            path = path[2:]

        try:
            file_obj = self.project.files.get(file_path=path, ref=sha)
            content = file_obj.decode()

            # decode() returns bytes, we need to decode to string
            return decode_raw_data(content)
        except gitlab.exceptions.GitlabGetError as e:
            if e.response_code == 404:
                logger.warning(f"File not found: {path} at ref {sha}")
                return None, "utf-8"
            logger.exception(f"Error getting file contents: {e}")
            return None, "utf-8"
        except Exception as e:
            logger.exception(f"Error getting file contents: {e}")
            return None, "utf-8"

    def _get_valid_file_paths(self, commit_sha: str | None = None) -> set[str]:
        """
        Get all valid file paths in the repository at a specific commit.

        Args:
            commit_sha: Commit SHA to get files from. Defaults to base_commit_sha.

        Returns:
            Set of valid file paths.
        """
        if commit_sha is None:
            commit_sha = self.base_commit_sha

        valid_file_paths: set[str] = set()
        valid_file_extensions = get_all_supported_extensions()

        # GitLab's repository_tree returns items with pagination
        # We need to iterate through all pages
        try:
            tree = self.project.repository_tree(ref=commit_sha, recursive=True, get_all=True)

            for item in tree:
                if item["type"] == "blob" and any(
                    item["path"].endswith(ext) for ext in valid_file_extensions
                ):
                    # GitLab doesn't return file size in repository_tree
                    # We'll include all files and filter by size when reading
                    valid_file_paths.add(item["path"])

        except gitlab.exceptions.GitlabGetError as e:
            logger.exception(f"Error getting repository tree: {e}")

        return valid_file_paths

    @sentry_sdk.trace
    def load_repo_to_tmp_dir(self, sha: str | None = None) -> tuple[str, str]:
        """
        Download and extract the repository to a temporary directory.

        Args:
            sha: Commit SHA to download. Defaults to base_commit_sha.

        Returns:
            Tuple of (tmp_dir, tmp_repo_dir) paths.
        """
        sha = sha or self.base_commit_sha

        # Create temp directory
        tmp_dir = tempfile.mkdtemp(prefix=f"{self.repo_owner}-{self.repo_name}_{sha}")
        tmp_repo_dir = os.path.join(tmp_dir, "repo")

        logger.debug(f"Loading repository to {tmp_repo_dir}")

        os.makedirs(tmp_repo_dir, exist_ok=True)

        # Get archive from GitLab
        tarfile_path = os.path.join(tmp_dir, f"{sha}.tar.gz")

        try:
            # GitLab's repository_archive returns the archive content directly
            archive = self.project.repository_archive(sha=sha, format="tar.gz")

            with open(tarfile_path, "wb") as f:
                f.write(archive)

        except gitlab.exceptions.GitlabGetError as e:
            logger.error(f"Failed to get archive for {self.repo_full_name} at {sha}: {e}")
            raise Exception(
                f"Failed to get archive for {self.repo_full_name} at {sha}. "
                "Please check if the repository exists and the provided token is valid."
            )

        # Extract tarball - use the base class helper
        import shutil
        import tarfile

        with tarfile.open(tarfile_path, "r:gz") as tar:
            tar.extractall(path=tmp_repo_dir)
            extracted_folders = [
                name
                for name in os.listdir(tmp_repo_dir)
                if os.path.isdir(os.path.join(tmp_repo_dir, name))
            ]
            if extracted_folders:
                root_folder = extracted_folders[0]
                root_folder_path = os.path.join(tmp_repo_dir, root_folder)
                for item in os.listdir(root_folder_path):
                    s = os.path.join(root_folder_path, item)
                    d = os.path.join(tmp_repo_dir, item)
                    if os.path.isdir(s):
                        shutil.move(s, d)
                    else:
                        if not os.path.islink(s):
                            shutil.copy2(s, d)

                shutil.rmtree(root_folder_path)

        return tmp_dir, tmp_repo_dir

    def _create_branch(self, branch_name: str, from_base_sha: bool = False) -> dict[str, Any]:
        """
        Create a new branch in the repository.

        Args:
            branch_name: Name of the branch to create.
            from_base_sha: If True, create from base_commit_sha instead of branch head.

        Returns:
            Branch data dictionary.
        """
        ref = self.base_commit_sha if from_base_sha else self.get_branch_head_sha(self.base_branch)

        branch = self.project.branches.create({"branch": branch_name, "ref": ref})
        return branch.attributes

    def get_branch_ref(self, branch_name: str) -> BranchRefResult | None:
        """
        Get a branch reference by name.

        Args:
            branch_name: Name of the branch.

        Returns:
            BranchRefResult if branch exists, None otherwise.
        """
        try:
            branch = self.project.branches.get(branch_name)
            return BranchRefResult(
                ref=f"refs/heads/{branch_name}",
                sha=branch.commit["id"],
                name=branch_name,
            )
        except gitlab.exceptions.GitlabGetError as e:
            if e.response_code == 404:
                return None
            raise e

    def create_branch_from_changes(
        self,
        *,
        pr_title: str,
        file_patches: list[FilePatch] | None = None,
        file_changes: list[FileChange] | None = None,
        branch_name: str | None = None,
        from_base_sha: bool = False,
    ) -> BranchRefResult | None:
        """
        Create a new branch with the specified file changes.

        Uses GitLab's commits API to create a commit with file actions.

        Args:
            pr_title: Title for the PR (used to generate branch name).
            file_patches: List of file patches to apply.
            file_changes: List of file changes to apply.
            branch_name: Optional specific branch name.
            from_base_sha: If True, create from base_commit_sha instead of branch head.

        Returns:
            BranchRefResult if successful, None if no changes were made.
        """
        if not file_patches and not file_changes:
            raise ValueError("Either file_patches or file_changes must be provided")

        new_branch_name = sanitize_branch_name(branch_name or pr_title)

        # Create the branch
        try:
            self._create_branch(new_branch_name, from_base_sha)
        except gitlab.exceptions.GitlabCreateError as e:
            # Branch already exists, add random suffix
            if "already exists" in str(e).lower() or e.response_code == 400:
                new_branch_name = f"{new_branch_name}-{generate_random_string(n=6)}"
                self._create_branch(new_branch_name, from_base_sha)
            else:
                raise e

        # Build commit actions
        actions = []
        branch_ref = new_branch_name

        if file_patches:
            for patch in file_patches:
                action = self._build_commit_action_for_patch(patch, branch_ref)
                if action:
                    actions.append(action)
        elif file_changes:
            for change in file_changes:
                action = self._build_commit_action_for_change(change, branch_ref)
                if action:
                    actions.append(action)

        if not actions:
            # No valid actions, delete the branch
            try:
                self.project.branches.delete(new_branch_name)
            except gitlab.exceptions.GitlabDeleteError:
                logger.warning(f"Failed to delete branch {new_branch_name}")
            return None

        # Create commit with actions
        try:
            commit_data = {
                "branch": new_branch_name,
                "commit_message": pr_title,
                "actions": actions,
            }
            commit = self.project.commits.create(commit_data)

            # Verify commit was created and has changes
            base_sha = self.get_branch_head_sha(self.base_branch)
            try:
                comparison = self.project.repository_compare(base_sha, commit.id)
                # repository_compare returns dict, but typing is Response | dict
                comp_dict = comparison if isinstance(comparison, dict) else {}
                if not comp_dict.get("commits") and not comp_dict.get("diffs"):
                    # No changes, delete the branch
                    try:
                        self.project.branches.delete(new_branch_name)
                    except gitlab.exceptions.GitlabDeleteError:
                        pass
                    sentry_sdk.capture_message(
                        "Failed to create branch from changes - no changes detected"
                    )
                    return None
            except gitlab.exceptions.GitlabGetError:
                # Comparison failed but commit was created, proceed
                pass

            return BranchRefResult(
                ref=f"refs/heads/{new_branch_name}",
                sha=commit.id,
                name=new_branch_name,
            )

        except gitlab.exceptions.GitlabCreateError as e:
            logger.exception(f"Error creating commit: {e}")
            # Clean up branch
            try:
                self.project.branches.delete(new_branch_name)
            except gitlab.exceptions.GitlabDeleteError:
                pass
            raise e

    def _build_commit_action_for_patch(
        self, patch: FilePatch, branch_ref: str
    ) -> dict[str, Any] | None:
        """
        Build a GitLab commit action from a FilePatch.

        Args:
            patch: The file patch to convert.
            branch_ref: The branch reference to read existing content from.

        Returns:
            A commit action dictionary or None if the action is invalid.
        """
        path = patch.path
        if path.startswith("/"):
            path = path[1:]
        if path.startswith("./"):
            path = path[2:]

        patch_type = patch.type
        action_type: str
        if patch_type == "A":  # Add/Create
            action_type = "create"
        elif patch_type == "D":  # Delete
            action_type = "delete"
        else:  # M = Modify/Update
            action_type = "update"

        # Get existing content for non-create operations
        existing_content = None
        if action_type != "create":
            existing_content, _ = self.get_file_content(path, sha=branch_ref)

        new_content = patch.apply(existing_content)

        if action_type == "delete":
            return {"action": "delete", "file_path": path}

        if new_content is None:
            return None

        return {
            "action": action_type,
            "file_path": path,
            "content": new_content,
        }

    def _build_commit_action_for_change(
        self, change: FileChange, branch_ref: str
    ) -> dict[str, Any] | None:
        """
        Build a GitLab commit action from a FileChange.

        Args:
            change: The file change to convert.
            branch_ref: The branch reference to read existing content from.

        Returns:
            A commit action dictionary or None if the action is invalid.
        """
        path = change.path
        if path.startswith("/"):
            path = path[1:]
        if path.startswith("./"):
            path = path[2:]

        change_type = change.change_type
        if change_type == "create":
            action_type = "create"
        elif change_type == "delete":
            action_type = "delete"
        else:
            action_type = "update"

        # Get existing content for non-create operations
        existing_content = None
        if action_type != "create":
            existing_content, _ = self.get_file_content(path, sha=branch_ref)

        new_content = change.apply(existing_content)

        if action_type == "delete":
            return {"action": "delete", "file_path": path}

        if new_content is None:
            return None

        return {
            "action": action_type,
            "file_path": path,
            "content": new_content,
        }

    def create_pr_from_branch(
        self,
        branch: BranchRefResult,
        title: str,
        description: str,
        provided_base: str | None = None,
    ) -> PullRequestResult:
        """
        Create a Merge Request from a branch.

        Args:
            branch: Branch reference to create MR from.
            title: MR title.
            description: MR description/body.
            provided_base: Optional base branch to merge into.

        Returns:
            PullRequestResult with MR details.
        """
        target_branch = provided_base or self.base_branch or self.get_default_branch()

        # Check for existing MR
        existing_mrs = self.project.mergerequests.list(
            state="opened", source_branch=branch.name, target_branch=target_branch
        )

        if existing_mrs:
            logger.warning(
                f"Branch {branch.name} already has an open MR.",
                extra={
                    "branch_ref": branch.ref,
                    "title": title,
                    "description": description,
                    "provided_base": provided_base,
                },
            )
            mr = existing_mrs[0]
            return PullRequestResult(
                number=mr.iid,
                html_url=mr.web_url,
                id=mr.id,
                head_ref=branch.name,
                head_sha=branch.sha,
            )

        # Create MR as draft using "Draft:" prefix
        draft_title = f"Draft: {title}" if not title.startswith("Draft:") else title

        try:
            mr = self.project.mergerequests.create(
                {
                    "source_branch": branch.name,
                    "target_branch": target_branch,
                    "title": draft_title,
                    "description": description,
                }
            )

            return PullRequestResult(
                number=mr.iid,
                html_url=mr.web_url,
                id=mr.id,
                head_ref=branch.name,
                head_sha=branch.sha,
            )

        except gitlab.exceptions.GitlabCreateError as e:
            logger.exception(f"Error creating MR: {e}")
            raise e

    def post_issue_comment(self, pr_url: str, comment: str) -> str:
        """
        Post a comment on a Merge Request.

        Args:
            pr_url: URL of the MR.
            comment: Comment text to post.

        Returns:
            URL of the created comment.
        """
        # Extract MR iid from URL
        # URL format: https://gitlab.com/owner/repo/-/merge_requests/123
        mr_iid = int(pr_url.rstrip("/").split("/")[-1])

        mr = self.project.mergerequests.get(mr_iid)
        note = mr.notes.create({"body": comment})

        # GitLab notes don't have direct URLs, construct one
        # Format: https://gitlab.com/owner/repo/-/merge_requests/123#note_456
        return f"{mr.web_url}#note_{note.id}"

    def post_mr_review_comment(self, pr_url: str, comment: GitLabMrReviewComment) -> str:
        """
        Create a review comment (discussion) on a GitLab Merge Request.

        Args:
            pr_url: URL of the MR.
            comment: Comment data including position information.

        Returns:
            URL of the created discussion.
        """
        mr_iid = int(pr_url.rstrip("/").split("/")[-1])
        mr = self.project.mergerequests.get(mr_iid)

        discussion_data: dict[str, Any] = {"body": comment["body"]}

        # Add position data if provided
        if "position" in comment and comment["position"]:
            discussion_data["position"] = comment["position"]

        discussion = mr.discussions.create(discussion_data)
        return f"{mr.web_url}#note_{discussion.id}"

    def get_file_url(
        self, file_path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        """
        Get a URL to view a file in the repository.

        Args:
            file_path: Path to the file.
            start_line: Optional starting line number.
            end_line: Optional ending line number.

        Returns:
            URL to view the file.
        """
        instance_url = get_gitlab_instance_url().rstrip("/")
        url = f"{instance_url}/{self.repo_full_name}/-/blob/{self.base_commit_sha}/{file_path}"

        if start_line:
            url += f"#L{start_line}"
        if start_line and end_line:
            url += f"-{end_line}"
        elif end_line:
            url += f"#L{end_line}"

        return url

    def get_commit_url(self, commit_sha: str) -> str:
        """
        Get a URL to view a commit.

        Args:
            commit_sha: The commit SHA.

        Returns:
            URL to view the commit.
        """
        instance_url = get_gitlab_instance_url().rstrip("/")
        return f"{instance_url}/{self.repo_full_name}/-/commit/{commit_sha}"

    def _autocorrect_path(self, path: str, sha: str | None = None) -> tuple[str, bool]:
        """
        Attempts to autocorrect a file path by finding the closest match in the repository.

        Args:
            path: The path to autocorrect
            sha: The commit SHA to use for finding valid paths

        Returns:
            A tuple of (corrected_path, was_autocorrected)
        """
        if sha is None:
            sha = self.base_commit_sha

        path = path.lstrip("/")
        valid_paths = self.get_valid_file_paths(sha)

        # If path is valid, return it unchanged
        if path in valid_paths:
            return path, False

        # Check for partial matches if no exact match and path is long enough
        if len(path) > 3:
            path_lower = path.lower()
            partial_matches = [
                valid_path for valid_path in valid_paths if path_lower in valid_path.lower()
            ]
            if partial_matches:
                # Sort by length to get closest match (shortest containing path)
                closest_match = sorted(partial_matches, key=len)[0]
                logger.warning(
                    f"Path '{path}' not found exactly, using closest match: '{closest_match}'"
                )
                return closest_match, True

        # No match found
        logger.warning("No matching file found for provided file path", extra={"path": path})
        return path, False

    def _get_commit_history(
        self, path: str, sha: str | None = None, autocorrect: bool = False, max_commits: int = 10
    ) -> list[str]:
        """
        Get commit history for a file.

        Args:
            path: File path to get history for.
            sha: Commit SHA to start from.
            autocorrect: Whether to attempt path autocorrection.
            max_commits: Maximum number of commits to return.

        Returns:
            List of formatted commit history strings.
        """
        if sha is None:
            sha = self.base_commit_sha

        if autocorrect:
            path, was_autocorrected = self._autocorrect_path(path, sha)
            if not was_autocorrected and path not in self.get_valid_file_paths(sha):
                return []

        try:
            commits = self.project.commits.list(ref_name=sha, path=path, per_page=max_commits)
            commit_strs = []

            for commit in commits:
                short_sha = commit.id[:7]
                message = commit.message

                # Get files touched by this commit
                commit_detail = self.project.commits.get(commit.id)
                diffs = commit_detail.diff()

                diffs_list = list(diffs) if not isinstance(diffs, list) else diffs
                files_touched = [
                    {"path": d["new_path"], "status": self._map_diff_status(d)}
                    for d in diffs_list[:20]
                ]

                additional_files_note = ""
                if len(diffs_list) > 20:
                    additional_files_note = (
                        f"\n[and {len(diffs_list) - 20} more files were changed...]"
                    )

                string = f"""----------------
{short_sha} - {message}
Files touched:
{self._format_files_touched(files_touched)}{additional_files_note}
"""
                commit_strs.append(string)

            return commit_strs

        except gitlab.exceptions.GitlabGetError as e:
            logger.exception(f"Error getting commit history: {e}")
            return []

    def _map_diff_status(self, diff: dict) -> str:
        """Map GitLab diff to status string."""
        if diff.get("new_file"):
            return "added"
        elif diff.get("deleted_file"):
            return "removed"
        elif diff.get("renamed_file"):
            return "renamed"
        else:
            return "modified"

    def _format_files_touched(self, files: list[dict]) -> str:
        """Format files touched list."""
        if not files:
            return "No files changed"
        return "\n".join([f"  {f['path']} ({f['status']})" for f in files])

    def _get_commit_patch_for_file(
        self, path: str, commit_sha: str, autocorrect: bool = False
    ) -> str | None:
        """
        Get the patch for a file in a specific commit.

        Args:
            path: File path to get patch for.
            commit_sha: Commit SHA.
            autocorrect: Whether to attempt path autocorrection.

        Returns:
            Patch string or None if not found.
        """
        if autocorrect:
            path, was_autocorrected = self._autocorrect_path(path, commit_sha)
            if not was_autocorrected and path not in self.get_valid_file_paths(commit_sha):
                return None

        try:
            commit = self.project.commits.get(commit_sha)
            diffs = commit.diff()

            for diff in diffs:
                if diff["new_path"] == path or diff["old_path"] == path:
                    return diff.get("diff")

            return None

        except gitlab.exceptions.GitlabGetError as e:
            logger.exception(f"Error getting commit patch: {e}")
            return None

    def get_mr_diff_content(self, mr_url: str) -> str:
        """
        Get the diff content of a Merge Request.

        Args:
            mr_url: URL of the MR.

        Returns:
            The diff content as a string.
        """
        mr_iid = int(mr_url.rstrip("/").split("/")[-1])
        mr = self.project.mergerequests.get(mr_iid)

        # Get all diffs
        changes = mr.changes()
        diffs = []
        # changes() returns dict, but typing is Response | dict
        changes_dict = changes if isinstance(changes, dict) else {}

        for change in changes_dict.get("changes", []):
            diff = change.get("diff", "")
            if diff:
                diffs.append(f"diff --git a/{change['old_path']} b/{change['new_path']}\n{diff}")

        return "\n".join(diffs)

    def get_mr_head_sha(self, mr_url: str) -> str:
        """
        Get the head SHA of a Merge Request.

        Args:
            mr_url: URL of the MR.

        Returns:
            The head commit SHA.
        """
        mr_iid = int(mr_url.rstrip("/").split("/")[-1])
        mr = self.project.mergerequests.get(mr_iid)
        return mr.sha
