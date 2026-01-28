"""
Abstract base class for repository clients.

This module defines the interface that all repository provider implementations
(GitHub, GitLab, etc.) must implement.
"""

import functools
import logging
import os
import shutil
import tarfile
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any

import requests
import sentry_sdk

from seer.automation.autofix.utils import generate_random_string, sanitize_branch_name
from seer.automation.codebase.utils import get_all_supported_extensions
from seer.automation.models import FileChange, FilePatch, RepoDefinition
from seer.automation.utils import AgentError

logger = logging.getLogger(__name__)


class RepoClientType(str, Enum):
    READ = "read"
    WRITE = "write"
    CODECOV_UNIT_TEST = "codecov_unit_test"
    CODECOV_PR_REVIEW = "codecov_pr_review"
    CODECOV_PR_CLOSED = "codecov_pr_closed"


@dataclass
class PullRequestResult:
    """
    Common return type for PR/MR creation across providers.
    Normalizes the differences between GitHub PR and GitLab MR attributes.
    """

    number: int  # GitHub: pr.number, GitLab: mr.iid
    html_url: str  # GitHub: pr.html_url, GitLab: mr.web_url
    id: int  # GitHub: pr.id, GitLab: mr.id
    head_ref: str  # Branch name
    head_sha: str | None = None  # Commit SHA of the head


@dataclass
class BranchRefResult:
    """
    Common return type for branch creation across providers.
    """

    ref: str  # Full ref path (e.g., "refs/heads/branch-name")
    sha: str  # Commit SHA
    name: str  # Branch name only

    @property
    def object_sha(self) -> str:
        """Alias for sha for compatibility with GitHub's branch_ref.object.sha"""
        return self.sha


class BaseRepoClient(ABC):
    """
    Abstract base class defining the interface for repository clients.

    All repository provider implementations (GitHub, GitLab, etc.) must inherit
    from this class and implement all abstract methods.
    """

    provider: str
    repo_owner: str
    repo_name: str
    repo_external_id: str
    base_commit_sha: str
    base_branch: str
    repo_definition: RepoDefinition

    # Providers that this base supports - subclasses should override
    supported_providers: list[str] = []

    def __init__(self, repo_definition: RepoDefinition):
        """
        Initialize the base repo client.

        Args:
            repo_definition: Definition of the repository to work with.
        """
        self.provider = repo_definition.provider
        self.repo_owner = repo_definition.owner
        self.repo_name = repo_definition.name
        self.repo_external_id = repo_definition.external_id
        self.repo_definition = repo_definition

        # Set up caching for expensive operations
        self.get_valid_file_paths = functools.lru_cache(maxsize=8)(self._get_valid_file_paths)
        self.get_commit_history = functools.lru_cache(maxsize=16)(self._get_commit_history)
        self.get_commit_patch_for_file = functools.lru_cache(maxsize=16)(
            self._get_commit_patch_for_file
        )

    @property
    def repo_full_name(self) -> str:
        """Return the full repository name (owner/name)."""
        return f"{self.repo_owner}/{self.repo_name}"

    @classmethod
    @abstractmethod
    def from_repo_definition(
        cls, repo_def: RepoDefinition, type: RepoClientType
    ) -> "BaseRepoClient":
        """
        Factory method to create a repo client from a repo definition.

        Args:
            repo_def: Definition of the repository.
            type: Type of client access needed (read, write, etc.).

        Returns:
            An instance of the repo client.
        """
        pass

    @staticmethod
    @abstractmethod
    def check_repo_write_access(repo: RepoDefinition) -> bool | None:
        """
        Check if the client has write access to the repository.

        Args:
            repo: Repository definition to check.

        Returns:
            True if write access is available, False if not, None if unable to check.
        """
        pass

    @staticmethod
    @abstractmethod
    def check_repo_read_access(repo: RepoDefinition) -> bool | None:
        """
        Check if the client has read access to the repository.

        Args:
            repo: Repository definition to check.

        Returns:
            True if read access is available, False if not, None if unable to check.
        """
        pass

    @abstractmethod
    def get_default_branch(self) -> str:
        """
        Get the default branch name for the repository.

        Returns:
            The name of the default branch (e.g., "main", "master").
        """
        pass

    @abstractmethod
    def get_branch_head_sha(self, branch: str) -> str:
        """
        Get the head commit SHA for a branch.

        Args:
            branch: Branch name.

        Returns:
            The SHA of the head commit on the branch.
        """
        pass

    @abstractmethod
    def get_file_content(self, path: str, sha: str | None = None) -> tuple[str | None, str]:
        """
        Get the content of a file at a specific commit.

        Args:
            path: Path to the file in the repository.
            sha: Commit SHA to get the file from. Defaults to base_commit_sha.

        Returns:
            Tuple of (file_content, encoding). Content is None if file doesn't exist.
        """
        pass

    @abstractmethod
    def _get_valid_file_paths(self, commit_sha: str | None = None) -> set[str]:
        """
        Get all valid file paths in the repository at a specific commit.
        This is the uncached implementation - use get_valid_file_paths() instead.

        Args:
            commit_sha: Commit SHA to get files from. Defaults to base_commit_sha.

        Returns:
            Set of valid file paths.
        """
        pass

    @abstractmethod
    def load_repo_to_tmp_dir(self, sha: str | None = None) -> tuple[str, str]:
        """
        Download and extract the repository to a temporary directory.

        Args:
            sha: Commit SHA to download. Defaults to base_commit_sha.

        Returns:
            Tuple of (tmp_dir, tmp_repo_dir) paths.
        """
        pass

    @abstractmethod
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

        Args:
            pr_title: Title for the PR (used to generate branch name).
            file_patches: List of file patches to apply.
            file_changes: List of file changes to apply.
            branch_name: Optional specific branch name.
            from_base_sha: If True, create from base_commit_sha instead of branch head.

        Returns:
            BranchRefResult if successful, None if no changes were made.
        """
        pass

    @abstractmethod
    def create_pr_from_branch(
        self,
        branch: BranchRefResult,
        title: str,
        description: str,
        provided_base: str | None = None,
    ) -> PullRequestResult:
        """
        Create a pull/merge request from a branch.

        Args:
            branch: Branch reference to create PR from.
            title: PR title.
            description: PR description/body.
            provided_base: Optional base branch to merge into.

        Returns:
            PullRequestResult with PR details.
        """
        pass

    @abstractmethod
    def post_issue_comment(self, pr_url: str, comment: str) -> str:
        """
        Post a comment on a PR/MR.

        Args:
            pr_url: URL of the PR/MR.
            comment: Comment text to post.

        Returns:
            URL of the created comment.
        """
        pass

    @abstractmethod
    def get_branch_ref(self, branch_name: str) -> BranchRefResult | None:
        """
        Get a branch reference by name.

        Args:
            branch_name: Name of the branch.

        Returns:
            BranchRefResult if branch exists, None otherwise.
        """
        pass

    # GitHub Copilot-specific methods with default no-op implementations
    # These are only used by GitHub and can be overridden by subclasses

    def comment_root_cause_on_pr_for_copilot(
        self, pr_url: str, run_id: int, issue_id: int, comment: str
    ) -> None:
        """
        Post a root cause comment on a PR for GitHub Copilot integration.
        This is a GitHub-specific feature; other providers can override or ignore.

        Args:
            pr_url: URL of the PR.
            run_id: Autofix run ID.
            issue_id: Issue ID.
            comment: Comment text.
        """
        pass

    def comment_pr_generated_for_copilot(
        self, pr_to_comment_on_url: str, new_pr_url: str, run_id: int
    ) -> None:
        """
        Post a comment that a fix PR was generated for GitHub Copilot integration.
        This is a GitHub-specific feature; other providers can override or ignore.

        Args:
            pr_to_comment_on_url: URL of the original PR to comment on.
            new_pr_url: URL of the newly generated PR.
            run_id: Autofix run ID.
        """
        pass

    # Common methods with default implementations

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
        # Default implementation returns empty - subclasses should override
        return []

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
        # Default implementation returns None - subclasses should override
        return None

    def does_file_exist(self, path: str, sha: str | None = None) -> bool:
        """
        Check if a file exists in the repository.

        Args:
            path: Path to check.
            sha: Commit SHA to check at. Defaults to base_commit_sha.

        Returns:
            True if file exists, False otherwise.
        """
        if sha is None:
            sha = self.base_commit_sha

        all_files = self.get_valid_file_paths(sha)
        normalized_path = path.lstrip("./").lstrip("/")
        return normalized_path in all_files

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
        # Default implementation for GitHub - subclasses should override
        url = f"https://github.com/{self.repo_full_name}/blob/{self.base_commit_sha}/{file_path}"
        if start_line:
            url += f"#L{start_line}"
        if start_line and end_line:
            url += f"-L{end_line}"
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
        # Default implementation for GitHub - subclasses should override
        return f"https://github.com/{self.repo_full_name}/commit/{commit_sha}"

    def _load_archive_to_dir(
        self, archive_url: str, sha: str, auth_headers: dict[str, str] | None = None
    ) -> tuple[str, str]:
        """
        Common implementation for loading a repository archive to a temporary directory.

        Args:
            archive_url: URL to download the archive from.
            sha: Commit SHA for naming.
            auth_headers: Optional authentication headers.

        Returns:
            Tuple of (tmp_dir, tmp_repo_dir) paths.
        """
        tmp_dir = tempfile.mkdtemp(prefix=f"{self.repo_owner}-{self.repo_name}_{sha}")
        tmp_repo_dir = os.path.join(tmp_dir, "repo")

        logger.debug(f"Loading repository to {tmp_repo_dir}")

        os.makedirs(tmp_repo_dir, exist_ok=True)

        # Clean the directory
        for root, dirs, files in os.walk(tmp_repo_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))

        tarfile_path = os.path.join(tmp_dir, f"{sha}.tar.gz")

        response = requests.get(archive_url, stream=True, headers=auth_headers)
        if response.status_code == 200:
            with open(tarfile_path, "wb") as f:
                f.write(response.content)
        else:
            logger.error(
                f"Failed to get tarball url for {archive_url}. "
                "Please check if the repository exists and the provided token is valid."
            )
            logger.error(
                f"Response status code: {response.status_code}, response text: {response.text}"
            )
            raise Exception(
                f"Failed to get tarball url for {archive_url}. "
                "Please check if the repository exists and the provided token is valid."
            )

        # Extract tarball into the output directory
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

    def _get_valid_file_paths_from_tree(
        self, tree_items: list[Any], path_attr: str = "path", type_attr: str = "type"
    ) -> set[str]:
        """
        Extract valid file paths from a tree structure.

        Args:
            tree_items: List of tree items from the provider API.
            path_attr: Attribute name for the file path.
            type_attr: Attribute name for the item type.

        Returns:
            Set of valid file paths.
        """
        valid_file_paths: set[str] = set()
        valid_file_extensions = get_all_supported_extensions()

        for item in tree_items:
            path = getattr(item, path_attr, None) or item.get(path_attr)
            item_type = getattr(item, type_attr, None) or item.get(type_attr)
            size = getattr(item, "size", None) or item.get("size", 0)

            if (
                item_type == "blob"
                and path
                and any(path.endswith(ext) for ext in valid_file_extensions)
                and (size or 0) <= 1024 * 1024  # 1MB limit
            ):
                valid_file_paths.add(path)

        return valid_file_paths


def get_repo_client_for_provider(
    repos: list[RepoDefinition],
    repo_name: str | None = None,
    repo_external_id: str | None = None,
    type: RepoClientType = RepoClientType.READ,
) -> BaseRepoClient:
    """
    Gets a repo client for a given repo, routing to the appropriate provider implementation.

    Args:
        repos: List of repository definitions available.
        repo_name: Optional repository name to select.
        repo_external_id: Optional external ID to select.
        type: Type of client access needed.

    Returns:
        An appropriate repo client instance for the provider.

    Raises:
        AgentError: If the repository is not found.
    """
    # Import here to avoid circular imports
    from seer.automation.codebase.repo_client import get_repo_client

    return get_repo_client(repos, repo_name, repo_external_id, type)
