from unittest.mock import MagicMock, patch

import gitlab
import pytest

from seer.automation.codebase.base_repo_client import BranchRefResult, RepoClientType
from seer.automation.codebase.gitlab_repo_client import GitLabRepoClient
from seer.automation.models import RepoDefinition
from seer.configuration import AppConfig
from seer.dependency_injection import resolve


@pytest.fixture(autouse=True)
def clear_gitlab_repo_client_cache():
    """Clear the GitLabRepoClient.from_repo_definition cache before each test"""
    GitLabRepoClient.from_repo_definition.cache_clear()
    yield


@pytest.fixture(autouse=True)
def setup_gitlab_config():
    app_config = resolve(AppConfig)
    app_config.GITLAB_TOKEN = "test_token"
    app_config.GITLAB_INSTANCE_URL = "https://gitlab.com"
    yield


@pytest.fixture
def mock_gitlab():
    with patch("seer.automation.codebase.gitlab_repo_client.gitlab.Gitlab") as mock:
        mock_instance = mock.return_value
        mock_project = MagicMock()
        mock_project.default_branch = "main"

        # Mock branch for get_branch_head_sha
        mock_branch = MagicMock()
        mock_branch.commit = {"id": "default_sha"}
        mock_project.branches.get.return_value = mock_branch

        mock_instance.projects.get.return_value = mock_project
        yield mock_instance


@pytest.fixture
def gitlab_repo_definition():
    return RepoDefinition(
        provider="gitlab",
        owner="test-group",
        name="test-project",
        external_id="12345",
        base_commit_sha="test_sha",
        branch_name="test_branch",
    )


@pytest.fixture
def gitlab_client(mock_gitlab, gitlab_repo_definition):
    return GitLabRepoClient.from_repo_definition(gitlab_repo_definition, RepoClientType.READ)


class TestGitLabRepoClient:

    def test_gitlab_client_initialization(self, gitlab_client):
        assert gitlab_client.provider == "gitlab"
        assert gitlab_client.repo_owner == "test-group"
        assert gitlab_client.repo_name == "test-project"
        assert gitlab_client.repo_external_id == "12345"
        assert gitlab_client.base_commit_sha == "test_sha"
        assert gitlab_client.base_branch == "test_branch"

    def test_gitlab_client_initialization_without_base_commit_sha(self, mock_gitlab):
        mock_gitlab.projects.get.return_value.branches.get.return_value.commit = {
            "id": "default_sha"
        }
        mock_gitlab.projects.get.return_value.default_branch = "main"

        repo_def_without_sha = RepoDefinition(
            provider="gitlab", owner="test-group", name="test-project", external_id="12345"
        )
        client = GitLabRepoClient.from_repo_definition(repo_def_without_sha, RepoClientType.READ)

        assert client.base_commit_sha == "default_sha"
        assert client.base_branch == "main"

    def test_gitlab_client_rejects_github_provider(self, mock_gitlab):
        with pytest.raises(Exception, match="GitLabRepoClient only supports 'gitlab' provider"):
            GitLabRepoClient(
                "test_token",
                RepoDefinition(
                    provider="github", owner="test-org", name="test-repo", external_id="123"
                ),
            )

    def test_gitlab_client_requires_token(self, mock_gitlab):
        with patch(
            "seer.automation.codebase.gitlab_repo_client.get_gitlab_token", return_value=None
        ):
            with pytest.raises(Exception, match="No GitLab token provided"):
                GitLabRepoClient(
                    None,
                    RepoDefinition(
                        provider="gitlab",
                        owner="test-group",
                        name="test-project",
                        external_id="123",
                    ),
                )

    def test_get_default_branch(self, gitlab_client, mock_gitlab):
        mock_gitlab.projects.get.return_value.default_branch = "develop"
        assert gitlab_client.get_default_branch() == "develop"

    def test_get_branch_head_sha(self, gitlab_client, mock_gitlab):
        mock_branch = MagicMock()
        mock_branch.commit = {"id": "new_sha_12345"}
        mock_gitlab.projects.get.return_value.branches.get.return_value = mock_branch

        result = gitlab_client.get_branch_head_sha("feature-branch")

        assert result == "new_sha_12345"
        mock_gitlab.projects.get.return_value.branches.get.assert_called_with("feature-branch")

    def test_get_file_content(self, gitlab_client, mock_gitlab):
        mock_file = MagicMock()
        mock_file.decode.return_value = b"test content"
        mock_gitlab.projects.get.return_value.files.get.return_value = mock_file

        content, encoding = gitlab_client.get_file_content("test_file.py")

        assert content == "test content"
        mock_gitlab.projects.get.return_value.files.get.assert_called_with(
            file_path="test_file.py", ref="test_sha"
        )

    def test_get_file_content_not_found(self, gitlab_client, mock_gitlab):
        mock_error = gitlab.exceptions.GitlabGetError()
        mock_error.response_code = 404
        mock_gitlab.projects.get.return_value.files.get.side_effect = mock_error

        content, encoding = gitlab_client.get_file_content("nonexistent.py")

        assert content is None
        assert encoding == "utf-8"

    def test_get_file_content_strips_leading_slashes(self, gitlab_client, mock_gitlab):
        mock_file = MagicMock()
        mock_file.decode.return_value = b"content"
        mock_gitlab.projects.get.return_value.files.get.return_value = mock_file

        gitlab_client.get_file_content("/path/to/file.py")

        mock_gitlab.projects.get.return_value.files.get.assert_called_with(
            file_path="path/to/file.py", ref="test_sha"
        )

    def test_get_valid_file_paths(self, gitlab_client, mock_gitlab):
        mock_tree = [
            {"path": "file1.py", "type": "blob"},
            {"path": "file2.py", "type": "blob"},
            {"path": "dir", "type": "tree"},
            {"path": "file3.txt", "type": "blob"},
        ]
        mock_gitlab.projects.get.return_value.repository_tree.return_value = mock_tree

        file_paths = gitlab_client.get_valid_file_paths()

        assert "file1.py" in file_paths
        assert "file2.py" in file_paths
        assert "dir" not in file_paths  # directories excluded

    @patch("seer.automation.codebase.gitlab_repo_client.tempfile.mkdtemp")
    def test_load_repo_to_tmp_dir(self, mock_mkdtemp, gitlab_client, mock_gitlab, tmp_path):
        mock_mkdtemp.return_value = str(tmp_path)
        mock_gitlab.projects.get.return_value.repository_archive.return_value = b"archive_content"

        with patch("builtins.open", MagicMock()):
            with patch("tarfile.open"):
                with patch("os.listdir", return_value=[]):
                    tmp_dir, tmp_repo_dir = gitlab_client.load_repo_to_tmp_dir()

        assert tmp_dir == str(tmp_path)
        assert tmp_repo_dir == str(tmp_path / "repo")
        mock_gitlab.projects.get.return_value.repository_archive.assert_called_once_with(
            sha="test_sha", format="tar.gz"
        )

    def test_create_branch_from_changes_invalid_input(self, gitlab_client):
        with pytest.raises(
            ValueError, match="Either file_patches or file_changes must be provided"
        ):
            gitlab_client.create_branch_from_changes(
                pr_title="Test MR", file_patches=None, file_changes=None
            )

    def test_create_branch_from_changes_success(self, gitlab_client, mock_gitlab):
        # Mock branch creation
        mock_gitlab.projects.get.return_value.branches.create.return_value = MagicMock(
            attributes={"name": "test-branch", "commit": {"id": "new_sha"}}
        )

        # Mock commit creation
        mock_commit = MagicMock()
        mock_commit.id = "commit_sha_123"
        mock_gitlab.projects.get.return_value.commits.create.return_value = mock_commit

        # Mock comparison
        mock_gitlab.projects.get.return_value.repository_compare.return_value = {
            "commits": [{"id": "abc"}],
            "diffs": [{"diff": "some diff"}],
        }

        # Mock file patch
        mock_patch = MagicMock()
        mock_patch.path = "test.py"
        mock_patch.type = "create"
        mock_patch.apply.return_value = "new content"

        result = gitlab_client.create_branch_from_changes(
            pr_title="Test MR", file_patches=[mock_patch]
        )

        assert result is not None
        assert result.sha == "commit_sha_123"
        assert "test-mr" in result.name.lower()

    def test_create_branch_from_changes_branch_exists(self, gitlab_client, mock_gitlab):
        # First call raises error for existing branch
        mock_error = gitlab.exceptions.GitlabCreateError()
        mock_error.response_code = 400

        mock_gitlab.projects.get.return_value.branches.create.side_effect = [
            mock_error,
            MagicMock(attributes={"name": "test-branch-abc123", "commit": {"id": "new_sha"}}),
        ]

        # Mock commit creation
        mock_commit = MagicMock()
        mock_commit.id = "commit_sha_123"
        mock_gitlab.projects.get.return_value.commits.create.return_value = mock_commit

        # Mock comparison
        mock_gitlab.projects.get.return_value.repository_compare.return_value = {
            "commits": [{"id": "abc"}]
        }

        # Mock file patch
        mock_patch = MagicMock()
        mock_patch.path = "test.py"
        mock_patch.type = "create"
        mock_patch.apply.return_value = "new content"

        result = gitlab_client.create_branch_from_changes(
            pr_title="Test MR", file_patches=[mock_patch]
        )

        assert result is not None
        # Verify branch creation was called twice (first failed, second with suffix)
        assert mock_gitlab.projects.get.return_value.branches.create.call_count == 2

    def test_create_pr_from_branch_success(self, gitlab_client, mock_gitlab):
        branch = BranchRefResult(ref="refs/heads/test-branch", sha="sha123", name="test-branch")

        mock_mr = MagicMock()
        mock_mr.iid = 42
        mock_mr.web_url = "https://gitlab.com/test-group/test-project/-/merge_requests/42"
        mock_mr.id = 12345
        mock_gitlab.projects.get.return_value.mergerequests.list.return_value = []
        mock_gitlab.projects.get.return_value.mergerequests.create.return_value = mock_mr

        result = gitlab_client.create_pr_from_branch(
            branch, title="Test MR", description="Test description"
        )

        assert result.number == 42
        assert result.html_url == "https://gitlab.com/test-group/test-project/-/merge_requests/42"
        assert result.id == 12345
        assert result.head_ref == "test-branch"

    def test_create_pr_from_branch_existing_mr(self, gitlab_client, mock_gitlab):
        branch = BranchRefResult(ref="refs/heads/test-branch", sha="sha123", name="test-branch")

        mock_existing_mr = MagicMock()
        mock_existing_mr.iid = 41
        mock_existing_mr.web_url = "https://gitlab.com/test-group/test-project/-/merge_requests/41"
        mock_existing_mr.id = 11111
        mock_gitlab.projects.get.return_value.mergerequests.list.return_value = [mock_existing_mr]

        result = gitlab_client.create_pr_from_branch(
            branch, title="Test MR", description="Test description"
        )

        # Should return existing MR
        assert result.number == 41
        mock_gitlab.projects.get.return_value.mergerequests.create.assert_not_called()

    def test_create_pr_from_branch_draft_prefix(self, gitlab_client, mock_gitlab):
        branch = BranchRefResult(ref="refs/heads/test-branch", sha="sha123", name="test-branch")

        mock_mr = MagicMock()
        mock_mr.iid = 42
        mock_mr.web_url = "https://gitlab.com/test-group/test-project/-/merge_requests/42"
        mock_mr.id = 12345
        mock_gitlab.projects.get.return_value.mergerequests.list.return_value = []
        mock_gitlab.projects.get.return_value.mergerequests.create.return_value = mock_mr

        gitlab_client.create_pr_from_branch(branch, title="Test MR", description="Description")

        # Verify MR was created with Draft: prefix
        call_args = mock_gitlab.projects.get.return_value.mergerequests.create.call_args
        assert call_args[0][0]["title"].startswith("Draft:")

    def test_post_issue_comment(self, gitlab_client, mock_gitlab):
        mock_mr = MagicMock()
        mock_mr.web_url = "https://gitlab.com/test-group/test-project/-/merge_requests/42"
        mock_note = MagicMock()
        mock_note.id = 999
        mock_mr.notes.create.return_value = mock_note
        mock_gitlab.projects.get.return_value.mergerequests.get.return_value = mock_mr

        result = gitlab_client.post_issue_comment(
            "https://gitlab.com/test-group/test-project/-/merge_requests/42",
            "Test comment",
        )

        assert "#note_999" in result
        mock_mr.notes.create.assert_called_once_with({"body": "Test comment"})

    def test_get_file_url(self, gitlab_client):
        url = gitlab_client.get_file_url("src/main.py")
        assert "test-group/test-project" in url
        assert "test_sha" in url
        assert "src/main.py" in url
        assert "/-/blob/" in url

    def test_get_file_url_with_lines(self, gitlab_client):
        url = gitlab_client.get_file_url("src/main.py", start_line=10, end_line=20)
        assert "#L10-20" in url

    def test_get_commit_url(self, gitlab_client):
        url = gitlab_client.get_commit_url("abc123")
        assert "test-group/test-project" in url
        assert "abc123" in url
        assert "/-/commit/" in url

    @patch(
        "seer.automation.codebase.gitlab_repo_client.get_gitlab_token", return_value="test_token"
    )
    @patch("seer.automation.codebase.gitlab_repo_client.gitlab.Gitlab")
    def test_check_repo_write_access_success(self, mock_gitlab_class, mock_get_token):
        mock_gl = MagicMock()
        mock_project = MagicMock()
        mock_project.permissions = {"project_access": {"access_level": 40}}  # Maintainer
        mock_gl.projects.get.return_value = mock_project
        mock_gitlab_class.return_value = mock_gl

        result = GitLabRepoClient.check_repo_write_access(
            RepoDefinition(
                provider="gitlab", owner="test-group", name="test-project", external_id="123"
            )
        )

        assert result is True

    @patch(
        "seer.automation.codebase.gitlab_repo_client.get_gitlab_token", return_value="test_token"
    )
    @patch("seer.automation.codebase.gitlab_repo_client.gitlab.Gitlab")
    def test_check_repo_write_access_insufficient(self, mock_gitlab_class, mock_get_token):
        mock_gl = MagicMock()
        mock_project = MagicMock()
        mock_project.permissions = {"project_access": {"access_level": 20}}  # Reporter
        mock_gl.projects.get.return_value = mock_project
        mock_gitlab_class.return_value = mock_gl

        result = GitLabRepoClient.check_repo_write_access(
            RepoDefinition(
                provider="gitlab", owner="test-group", name="test-project", external_id="123"
            )
        )

        assert result is False

    @patch("seer.automation.codebase.gitlab_repo_client.get_gitlab_token", return_value=None)
    def test_check_repo_write_access_no_token(self, mock_get_token):
        result = GitLabRepoClient.check_repo_write_access(
            RepoDefinition(
                provider="gitlab", owner="test-group", name="test-project", external_id="123"
            )
        )

        assert result is None

    @patch(
        "seer.automation.codebase.gitlab_repo_client.get_gitlab_token", return_value="test_token"
    )
    @patch("seer.automation.codebase.gitlab_repo_client.gitlab.Gitlab")
    def test_check_repo_read_access_success(self, mock_gitlab_class, mock_get_token):
        mock_gl = MagicMock()
        mock_project = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gitlab_class.return_value = mock_gl

        result = GitLabRepoClient.check_repo_read_access(
            RepoDefinition(
                provider="gitlab", owner="test-group", name="test-project", external_id="123"
            )
        )

        assert result is True

    @patch(
        "seer.automation.codebase.gitlab_repo_client.get_gitlab_token", return_value="test_token"
    )
    @patch("seer.automation.codebase.gitlab_repo_client.gitlab.Gitlab")
    def test_check_repo_read_access_not_found(self, mock_gitlab_class, mock_get_token):
        mock_gl = MagicMock()
        mock_gl.projects.get.side_effect = gitlab.exceptions.GitlabGetError()
        mock_gitlab_class.return_value = mock_gl

        result = GitLabRepoClient.check_repo_read_access(
            RepoDefinition(
                provider="gitlab", owner="test-group", name="test-project", external_id="123"
            )
        )

        assert result is False

    def test_get_mr_diff_content(self, gitlab_client, mock_gitlab):
        mock_mr = MagicMock()
        mock_mr.changes.return_value = {
            "changes": [
                {
                    "old_path": "file1.py",
                    "new_path": "file1.py",
                    "diff": "@@ -1,5 +1,7 @@\n+new line",
                },
                {
                    "old_path": "file2.py",
                    "new_path": "file2.py",
                    "diff": "@@ -10,3 +10,5 @@\n+another",
                },
            ]
        }
        mock_gitlab.projects.get.return_value.mergerequests.get.return_value = mock_mr

        result = gitlab_client.get_mr_diff_content(
            "https://gitlab.com/test-group/test-project/-/merge_requests/42"
        )

        assert "file1.py" in result
        assert "file2.py" in result
        assert "+new line" in result

    def test_get_mr_head_sha(self, gitlab_client, mock_gitlab):
        mock_mr = MagicMock()
        mock_mr.sha = "head_sha_123"
        mock_gitlab.projects.get.return_value.mergerequests.get.return_value = mock_mr

        result = gitlab_client.get_mr_head_sha(
            "https://gitlab.com/test-group/test-project/-/merge_requests/42"
        )

        assert result == "head_sha_123"

    def test_autocorrect_path_exact_match(self, gitlab_client):
        gitlab_client.get_valid_file_paths = MagicMock(
            return_value={"src/main.py", "tests/test.py"}
        )

        path, was_corrected = gitlab_client._autocorrect_path("src/main.py")

        assert path == "src/main.py"
        assert was_corrected is False

    def test_autocorrect_path_partial_match(self, gitlab_client):
        gitlab_client.get_valid_file_paths = MagicMock(
            return_value={"src/main.py", "tests/test.py"}
        )

        path, was_corrected = gitlab_client._autocorrect_path("main.py")

        assert path == "src/main.py"
        assert was_corrected is True

    def test_autocorrect_path_no_match(self, gitlab_client):
        gitlab_client.get_valid_file_paths = MagicMock(
            return_value={"src/main.py", "tests/test.py"}
        )

        path, was_corrected = gitlab_client._autocorrect_path("nonexistent.py")

        assert path == "nonexistent.py"
        assert was_corrected is False

    def test_get_commit_history(self, gitlab_client, mock_gitlab):
        mock_commit = MagicMock()
        mock_commit.id = "abc123def"
        mock_commit.message = "Test commit message"
        mock_gitlab.projects.get.return_value.commits.list.return_value = [mock_commit]

        # Mock commit detail
        mock_commit_detail = MagicMock()
        mock_commit_detail.diff.return_value = [
            {"new_path": "test.py", "new_file": False, "deleted_file": False, "renamed_file": False}
        ]
        mock_gitlab.projects.get.return_value.commits.get.return_value = mock_commit_detail

        result = gitlab_client.get_commit_history("test.py", max_commits=1)

        assert len(result) == 1
        assert "abc123d" in result[0]
        assert "Test commit message" in result[0]

    def test_get_commit_patch_for_file(self, gitlab_client, mock_gitlab):
        mock_commit = MagicMock()
        mock_commit.diff.return_value = [
            {"old_path": "test.py", "new_path": "test.py", "diff": "@@ -1,5 +1,7 @@\n+new line"}
        ]
        mock_gitlab.projects.get.return_value.commits.get.return_value = mock_commit

        result = gitlab_client.get_commit_patch_for_file("test.py", "commit_sha")

        assert "@@ -1,5 +1,7 @@" in result

    def test_get_commit_patch_for_file_not_found(self, gitlab_client, mock_gitlab):
        mock_commit = MagicMock()
        mock_commit.diff.return_value = [
            {"old_path": "other.py", "new_path": "other.py", "diff": "some diff"}
        ]
        mock_gitlab.projects.get.return_value.commits.get.return_value = mock_commit

        result = gitlab_client.get_commit_patch_for_file("test.py", "commit_sha")

        assert result is None

    def test_build_commit_action_for_patch_create(self, gitlab_client):
        mock_patch = MagicMock()
        mock_patch.path = "new_file.py"
        mock_patch.type = "create"
        mock_patch.apply.return_value = "new content"

        result = gitlab_client._build_commit_action_for_patch(mock_patch, "main")

        assert result["action"] == "create"
        assert result["file_path"] == "new_file.py"
        assert result["content"] == "new content"

    def test_build_commit_action_for_patch_update(self, gitlab_client, mock_gitlab):
        mock_file = MagicMock()
        mock_file.decode.return_value = b"old content"
        mock_gitlab.projects.get.return_value.files.get.return_value = mock_file

        mock_patch = MagicMock()
        mock_patch.path = "existing.py"
        mock_patch.type = "edit"
        mock_patch.apply.return_value = "updated content"

        result = gitlab_client._build_commit_action_for_patch(mock_patch, "main")

        assert result["action"] == "update"
        assert result["file_path"] == "existing.py"
        assert result["content"] == "updated content"

    def test_build_commit_action_for_patch_delete(self, gitlab_client, mock_gitlab):
        mock_file = MagicMock()
        mock_file.decode.return_value = b"old content"
        mock_gitlab.projects.get.return_value.files.get.return_value = mock_file

        mock_patch = MagicMock()
        mock_patch.path = "to_delete.py"
        mock_patch.type = "delete"
        mock_patch.apply.return_value = None

        result = gitlab_client._build_commit_action_for_patch(mock_patch, "main")

        assert result["action"] == "delete"
        assert result["file_path"] == "to_delete.py"
        assert "content" not in result

    def test_does_file_exist(self, gitlab_client):
        gitlab_client.get_valid_file_paths = MagicMock(
            return_value={"src/main.py", "tests/test.py"}
        )

        assert gitlab_client.does_file_exist("src/main.py") is True
        assert gitlab_client.does_file_exist("/src/main.py") is True
        assert gitlab_client.does_file_exist("./src/main.py") is True
        assert gitlab_client.does_file_exist("nonexistent.py") is False

    def test_get_branch_ref_success(self, gitlab_client, mock_gitlab):
        mock_branch = MagicMock()
        mock_branch.commit = {"id": "sha123456"}
        mock_gitlab.projects.get.return_value.branches.get.return_value = mock_branch

        result = gitlab_client.get_branch_ref("feature-branch")

        assert result is not None
        assert result.name == "feature-branch"
        assert result.sha == "sha123456"
        assert result.ref == "refs/heads/feature-branch"

    def test_get_branch_ref_not_found(self, gitlab_client, mock_gitlab):
        mock_error = gitlab.exceptions.GitlabGetError()
        mock_error.response_code = 404
        mock_gitlab.projects.get.return_value.branches.get.side_effect = mock_error

        result = gitlab_client.get_branch_ref("nonexistent-branch")

        assert result is None
