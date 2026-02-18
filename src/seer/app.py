import logging
import os
import time

import datadog
import flask
import sentry_sdk
from datadog.dogstatsd.base import statsd
from flask import Blueprint, Flask, jsonify
from openai import APITimeoutError
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from werkzeug.exceptions import GatewayTimeout, InternalServerError

from integrations.codecov.codecov_auth import CodecovAuthentication
from integrations.overwatch.overwatch_auth import OverwatchAuthentication
from seer.anomaly_detection.models.external import (
    AlertInSeer,
    DeleteAlertDataRequest,
    DeleteAlertDataResponse,
    DetectAnomaliesRequest,
    DetectAnomaliesResponse,
    StoreDataRequest,
    StoreDataResponse,
    TimeSeriesWithHistory,
)
from seer.automation.assisted_query.assisted_query import translate_query
from seer.automation.assisted_query.create_cache import create_cache
from seer.automation.assisted_query.models import (
    CreateCacheRequest,
    CreateCacheResponse,
    TranslateRequest,
    TranslateResponses,
)
from seer.automation.autofix.models import (
    AutofixEndpointResponse,
    AutofixEvaluationRequest,
    AutofixNoopRequest,
    AutofixPrIdRequest,
    AutofixRequest,
    AutofixStateRequest,
    AutofixStateResponse,
    AutofixUpdateEndpointResponse,
    AutofixUpdateRequest,
    AutofixUpdateType,
)
from seer.automation.autofix.runs import update_repo_access_and_properties
from seer.automation.autofix.tasks import (
    check_and_mark_if_timed_out,
    comment_on_thread,
    get_autofix_state,
    get_autofix_state_from_pr_id,
    receive_feedback,
    receive_user_message,
    resolve_comment_thread,
    restart_from_point_with_feedback,
    run_autofix_coding,
    run_autofix_evaluation,
    run_autofix_push_changes,
    run_autofix_root_cause,
    run_autofix_solution,
    update_code_change,
)
from seer.automation.codebase.models import RepoAccessCheckRequest, RepoAccessCheckResponse
from seer.automation.codebase.repo_client import RepoClient
from seer.automation.codebase.tasks import (
    collect_all_repos_for_backfill,
    run_repo_archive_cleanup,
    run_repo_sync,
)
from seer.automation.codegen.evals.models import (
    CodegenRelevantWarningsEvaluationRequest,
    CodegenRelevantWarningsEvaluationSummary,
)
from seer.automation.codegen.evals.tasks import run_relevant_warnings_evaluation
from seer.automation.codegen.models import (
    CodecovTaskRequest,
    CodegenBaseRequest,
    CodegenBaseResponse,
    CodegenPrClosedResponse,
    CodegenPrReviewResponse,
    CodegenPrReviewStateRequest,
    CodegenPrReviewStateResponse,
    CodegenRelevantWarningsRequest,
    CodegenRelevantWarningsResponse,
    CodegenUnitTestsResponse,
    CodegenUnitTestsStateRequest,
    CodegenUnitTestsStateResponse,
)
from seer.automation.codegen.tasks import (
    codegen_bug_prediction,
    codegen_pr_closed,
    codegen_pr_review,
    codegen_relevant_warnings,
    codegen_retry_unittest,
    codegen_unittest,
    get_unittest_state,
)
from seer.automation.explorer.models import (
    AnomalyDetectionAlertDataRequest,
    AnomalyDetectionAlertDataResponse,
    AssistedQueryStartRequest,
    AssistedQueryStartResponse,
    AssistedQueryStateRequest,
    AssistedQueryStateResponse,
    AssistedQueryTranslateAgenticRequest,
    AssistedQueryTranslateAgenticResponse,
    AutofixPromptRequest,
    AutofixPromptResponse,
    CodegenPrReviewRerunRequest,
    CodegenPrReviewRerunResponse,
    CodingAgentStateSetRequest,
    CodingAgentStateSetResponse,
    CodingAgentStateUpdateRequest,
    CodingAgentStateUpdateResponse,
    ExplorerChatRequest,
    ExplorerChatResponse,
    ExplorerRunsRequest,
    ExplorerRunsResponse,
    ExplorerStateRequest,
    ExplorerStateResponse,
    ExplorerUpdateRequest,
    ExplorerUpdateResponse,
    LlmGenerateRequest,
    LlmGenerateResponse,
    ProjectPreferenceBulkRequest,
    ProjectPreferenceBulkResponse,
    ProjectPreferenceBulkSetRequest,
    ProjectPreferenceBulkSetResponse,
    SupergroupsRequest,
    SupergroupsResponse,
    WorkflowsCompareCohortRequest,
    WorkflowsCompareCohortResponse,
)
from seer.automation.explorer.state import ExplorerRunState
from seer.automation.explorer.tasks import process_explorer_chat
from seer.automation.preferences import (
    GetSeerProjectPreferenceRequest,
    GetSeerProjectPreferenceResponse,
    SetSeerProjectPreferenceRequest,
    SetSeerProjectPreferenceResponse,
    get_seer_project_preference,
    set_seer_project_preference,
)
from seer.automation.summarize.issue import run_fixability_score, run_summarize_issue
from seer.automation.summarize.models import (
    GetFixabilityScoreRequest,
    SummarizeIssueRequest,
    SummarizeIssueResponse,
    SummarizeTraceRequest,
    SummarizeTraceResponse,
)
from seer.automation.summarize.traces import summarize_trace
from seer.automation.utils import ConsentError, raise_if_no_genai_consent
from seer.bootup import bootup, module
from seer.configuration import AppConfig
from seer.dependency_injection import inject, injected, resolve
from seer.exceptions import ClientError, ServerError
from seer.grouping.grouping import (
    BulkCreateGroupingRecordsResponse,
    CreateGroupingRecordsRequest,
    DeleteGroupingRecordsByHashRequest,
    DeleteGroupingRecordsByHashResponse,
    GroupingRequest,
    SimilarityResponse,
)
from seer.inference_models import (
    autofixability_model,
    embeddings_model,
    grouping_lookup,
    load_anomaly_detection,
    test_grouping_model,
)
from seer.json_api import json_api
from seer.loading import LoadingResult
from seer.severity.severity_inference import SeverityRequest, SeverityResponse
from seer.smoke_test import check_smoke_test
from seer.tags import AnomalyDetectionModes, AnomalyDetectionTags
from seer.trend_detection.trend_detector import BreakpointRequest, BreakpointResponse, find_trends
from seer.workflows.compare.models import CompareCohortsRequest, CompareCohortsResponse
from seer.workflows.compare.service import compare_cohort

logger = logging.getLogger(__name__)

app = flask.current_app
blueprint = Blueprint("app", __name__)
app_module = module

# Initialize Datadog client for metrics
datadog.initialize(
    statsd_host=os.environ.get("STATSD_HOST", "127.0.0.1"),
    statsd_port=int(os.environ.get("STATSD_PORT", "8126")),
)
# Workaround for https://github.com/DataDog/datadogpy/issues/764 as described in https://github.com/getsentry/sentry/pull/68644/files#
statsd.disable_telemetry()
statsd.disable_buffering = False
statsd._container_id = None


@json_api(blueprint, "/v0/issues/severity-score")
def severity_endpoint(data: SeverityRequest) -> SeverityResponse:
    if data.trigger_error:
        raise Exception("oh no")
    elif data.trigger_timeout:
        time.sleep(0.5)

    response = embeddings_model().severity_score(data)
    sentry_sdk.set_tag("severity", str(response.severity))
    return response


@json_api(blueprint, "/trends/breakpoint-detector")
def breakpoint_trends_endpoint(data: BreakpointRequest) -> BreakpointResponse:
    txns_data = data.data

    sort_function = data.sort
    allow_midpoint = data.allow_midpoint == "1"
    validate_tail_hours = data.validate_tail_hours

    min_pct_change = data.trend_percentage
    min_change = data.min_change

    trend_percentage_list = find_trends(
        txns_data,
        sort_function,
        allow_midpoint,
        min_pct_change,
        min_change,
        validate_tail_hours,
    )

    trends = BreakpointResponse(data=[x[1] for x in trend_percentage_list])

    return trends


@json_api(blueprint, "/v0/issues/similar-issues")
def similarity_endpoint(data: GroupingRequest) -> SimilarityResponse:
    with sentry_sdk.start_span(op="seer.grouping", description="grouping lookup") as span:
        sentry_sdk.set_tag("read_only", data.read_only)
        sentry_sdk.set_tag("request_hash", data.hash)
        span.set_data("stacktrace_len", len(data.stacktrace))
        similar_issues = grouping_lookup().get_nearest_neighbors(data)
    return similar_issues


@json_api(blueprint, "/v0/issues/similar-issues/grouping-record")
def similarity_grouping_record_endpoint(
    data: CreateGroupingRecordsRequest,
) -> BulkCreateGroupingRecordsResponse:
    sentry_sdk.set_tag(
        "stacktrace_len_sum", sum([len(stacktrace) for stacktrace in data.stacktrace_list])
    )
    success = grouping_lookup().bulk_create_and_insert_grouping_records(data)
    return success


@blueprint.route(
    "/v0/issues/similar-issues/grouping-record/delete/<int:project_id>", methods=["GET"]
)
def delete_grouping_record_endpoint(project_id: int):
    success = grouping_lookup().delete_grouping_records_for_project(project_id)
    return jsonify(success=success)


@json_api(blueprint, "/v0/issues/similar-issues/grouping-record/delete-by-hash")
def delete_grouping_records_by_hash_endpoint(
    data: DeleteGroupingRecordsByHashRequest,
) -> DeleteGroupingRecordsByHashResponse:
    success = grouping_lookup().delete_grouping_records_by_hash(data)
    return success


@json_api(blueprint, "/v1/automation/codebase/repo/check-access")
def repo_access_check_endpoint(data: RepoAccessCheckRequest) -> RepoAccessCheckResponse:
    return RepoAccessCheckResponse(
        has_access=RepoClient.check_repo_write_access(data.repo) or False
    )


@json_api(blueprint, "/v1/automation/autofix/start")
def autofix_start_endpoint(data: AutofixRequest) -> AutofixEndpointResponse:
    raise_if_no_genai_consent(data.organization_id)
    run_id = run_autofix_root_cause(data)
    return AutofixEndpointResponse(started=True, run_id=run_id or -1)


@json_api(blueprint, "/v1/automation/autofix/update")
def autofix_update_endpoint(
    data: AutofixUpdateRequest,
) -> AutofixUpdateEndpointResponse:
    if data.payload.type == AutofixUpdateType.SELECT_ROOT_CAUSE:
        run_autofix_solution(data)
    elif data.payload.type == AutofixUpdateType.SELECT_SOLUTION:
        run_autofix_coding(data)
    elif data.payload.type == AutofixUpdateType.CREATE_PR:
        run_autofix_push_changes(data)
    elif data.payload.type == AutofixUpdateType.CREATE_BRANCH:
        run_autofix_push_changes(data)
    elif data.payload.type == AutofixUpdateType.USER_MESSAGE:
        receive_user_message(data)
    elif data.payload.type == AutofixUpdateType.RESTART_FROM_POINT_WITH_FEEDBACK:
        restart_from_point_with_feedback(data)
    elif data.payload.type == AutofixUpdateType.UPDATE_CODE_CHANGE:
        update_code_change(data)
    elif data.payload.type == AutofixUpdateType.COMMENT_THREAD:
        comment_on_thread(data)
    elif data.payload.type == AutofixUpdateType.RESOLVE_COMMENT_THREAD:
        resolve_comment_thread(data)
    elif data.payload.type == AutofixUpdateType.FEEDBACK:
        receive_feedback(data)

    return AutofixUpdateEndpointResponse(run_id=data.run_id)


@json_api(blueprint, "/v1/automation/autofix/state")
def get_autofix_state_endpoint(data: AutofixStateRequest) -> AutofixStateResponse:
    state = get_autofix_state(group_id=data.group_id, run_id=data.run_id)

    if state:
        check_and_mark_if_timed_out(state)

        if data.check_repo_access:
            update_repo_access_and_properties(state)

        cur_state = state.get()

        return AutofixStateResponse(
            group_id=cur_state.request.issue.id,
            run_id=cur_state.run_id,
            state=cur_state.model_dump(mode="json"),
        )

    return AutofixStateResponse(group_id=None, run_id=None, state=None)


@json_api(blueprint, "/v1/automation/autofix/state/pr")
def get_autofix_state_from_pr_endpoint(data: AutofixPrIdRequest) -> AutofixStateResponse:
    state = get_autofix_state_from_pr_id(data.provider, data.pr_id)

    if state:
        cur_state = state.get()
        return AutofixStateResponse(
            group_id=cur_state.request.issue.id,
            run_id=cur_state.run_id,
            state=cur_state.model_dump(mode="json"),
        )
    return AutofixStateResponse(group_id=None, run_id=None, state=None)


@json_api(blueprint, "/v1/automation/autofix/evaluations/start")
def autofix_evaluation_start_endpoint(data: AutofixEvaluationRequest) -> AutofixEndpointResponse:
    config = resolve(AppConfig)
    if not config.DEV:
        raise RuntimeError("The evaluation endpoint is only available in development mode")

    run_autofix_evaluation(data)

    return AutofixEndpointResponse(started=True, run_id=-1)


@json_api(blueprint, "/v1/automation/autofix/backfill/start")
def autofix_backfill_start_endpoint(data: AutofixNoopRequest) -> AutofixEndpointResponse:
    config = resolve(AppConfig)
    if not config.DEV:
        raise RuntimeError("The backfill endpoint is only available in development mode")

    collect_all_repos_for_backfill.apply_async()

    return AutofixEndpointResponse(started=True, run_id=-1)


@json_api(blueprint, "/v1/automation/autofix/sync/start")
def autofix_sync_start_endpoint(data: AutofixNoopRequest) -> AutofixEndpointResponse:
    config = resolve(AppConfig)
    if not config.DEV:
        raise RuntimeError("The sync endpoint is only available in development mode")

    run_repo_sync.apply_async()

    return AutofixEndpointResponse(started=True, run_id=-1)


@json_api(blueprint, "/v1/automation/autofix/cache-ttl/start")
def autofix_cache_ttl_endpoint(data: AutofixNoopRequest) -> AutofixEndpointResponse:
    config = resolve(AppConfig)
    if not config.DEV:
        raise RuntimeError("The cache ttl endpoint is only available in development mode")

    run_repo_archive_cleanup.apply_async()

    return AutofixEndpointResponse(started=True, run_id=-1)


@json_api(blueprint, "/v1/automation/codegen/unit-tests")
def codegen_unit_tests_endpoint(data: CodegenBaseRequest) -> CodegenUnitTestsResponse:
    return codegen_unittest(data)


@json_api(blueprint, "/v1/automation/codegen/pr-closed")
def codegen_pr_closed_endpoint(data: CodegenBaseRequest) -> CodegenPrClosedResponse:
    return codegen_pr_closed(data)


@json_api(blueprint, "/v1/automation/codegen/unit-tests/state")
def codegen_unit_tests_state_endpoint(
    data: CodegenUnitTestsStateRequest,
) -> CodegenUnitTestsStateResponse:
    state = get_unittest_state(data)

    return CodegenUnitTestsStateResponse(
        run_id=state.run_id,
        status=state.status,
        changes=state.file_changes,
        triggered_at=state.last_triggered_at,
        updated_at=state.updated_at,
        completed_at=state.completed_at,
    )


@json_api(blueprint, "/v1/automation/codegen/relevant-warnings")
def codegen_relevant_warnings_endpoint(
    data: CodegenRelevantWarningsRequest,
) -> CodegenRelevantWarningsResponse:
    return codegen_relevant_warnings(data)


@json_api(blueprint, "/v1/automation/codegen/relevant-warnings/evaluation/start")
def codegen_relevant_warnings_evaluation_start_endpoint(
    data: CodegenRelevantWarningsEvaluationRequest,
) -> CodegenRelevantWarningsEvaluationSummary:
    config = resolve(AppConfig)
    if not config.DEV:
        raise RuntimeError("The evaluation endpoint is only available in development mode")

    result = run_relevant_warnings_evaluation(data)

    return result


@json_api(blueprint, "/v1/automation/codegen/bug-prediction")
def codegen_bug_prediction_endpoint(
    data: CodegenRelevantWarningsRequest,
) -> CodegenRelevantWarningsResponse:
    return codegen_bug_prediction(data)


@json_api(blueprint, "/v1/automation/codegen/pr-review")
def codegen_pr_review_endpoint(data: CodegenBaseRequest) -> CodegenPrReviewResponse:
    return codegen_pr_review(data)


@json_api(blueprint, "/v1/automation/codegen/pr-review/state")
def codegen_pr_review_state_endpoint(
    data: CodegenPrReviewStateRequest,
) -> CodegenPrReviewStateResponse:
    raise NotImplementedError("PR Review state is not implemented yet.")


# TODO: Remove this endpoint once we migrate all codecov requests to overwatch
@json_api(blueprint, "/v1/automation/codecov-request")
def codecov_request_endpoint(
    data: CodecovTaskRequest,
) -> CodegenBaseResponse:
    is_valid = CodecovAuthentication.authenticate_codecov_app_install(
        data.external_owner_id, data.data.repo.external_id
    )

    if not is_valid:
        raise ConsentError(f"Invalid permissions for org {data.external_owner_id}.")

    if data.request_type == "pr-review":
        return codegen_pr_review(data.data, is_codecov_request=True)
    elif data.request_type == "unit-tests":
        return codegen_unittest(data.data, is_codecov_request=True)
    elif data.request_type == "pr-closed":
        return codegen_pr_closed_endpoint(data.data)
    elif data.request_type == "retry-unit-tests":
        return codegen_retry_unittest(data.data)

    raise ValueError(f"Unsupported request_type: {data.request_type}")


@json_api(blueprint, "/v1/automation/overwatch-request")
def overwatch_request_endpoint(
    data: CodecovTaskRequest,
) -> CodegenBaseResponse:
    is_valid = OverwatchAuthentication.authenticate_overwatch_app_install(data.external_owner_id)

    if not is_valid:
        raise ConsentError(f"Invalid permissions for org {data.external_owner_id}.")

    if data.request_type == "pr-review":
        return codegen_pr_review(data.data, is_codecov_request=False)
    elif data.request_type == "unit-tests":
        return codegen_unittest(data.data, is_codecov_request=False)

    raise ValueError(f"Unsupported request_type: {data.request_type}")


@json_api(blueprint, "/v1/project-preference")
def get_seer_project_preference_endpoint(
    data: GetSeerProjectPreferenceRequest,
) -> GetSeerProjectPreferenceResponse:
    return get_seer_project_preference(data)


@json_api(blueprint, "/v1/project-preference/set")
def set_seer_project_preference_endpoint(
    data: SetSeerProjectPreferenceRequest,
) -> SetSeerProjectPreferenceResponse:
    return set_seer_project_preference(data)


@json_api(blueprint, "/v1/automation/summarize/issue")
def summarize_issue_endpoint(data: SummarizeIssueRequest) -> SummarizeIssueResponse:
    try:
        return run_summarize_issue(data)
    except APITimeoutError as e:
        raise GatewayTimeout from e
    except Exception as e:
        logger.exception("Error summarizing issue")
        raise InternalServerError from e


@json_api(blueprint, "/v1/automation/summarize/trace")
def summarize_trace_endpoint(data: SummarizeTraceRequest) -> SummarizeTraceResponse:
    try:
        response = summarize_trace(data)
        statsd.increment("seer.automation.summarize.trace.success")
        return response
    except APITimeoutError as e:
        statsd.increment("seer.automation.summarize.trace.api_timeout")
        raise GatewayTimeout from e
    except Exception as e:
        statsd.increment("seer.automation.summarize.trace.server_error")
        logger.exception("Error summarizing trace")
        raise InternalServerError from e


@json_api(blueprint, "/v1/automation/summarize/fixability")
def get_fixability_score_endpoint(data: GetFixabilityScoreRequest) -> SummarizeIssueResponse:
    model = autofixability_model()
    try:
        return run_fixability_score(data, model)
    except APITimeoutError as e:
        raise GatewayTimeout from e
    except Exception as e:
        logger.exception("Error calculating fixability score")
        raise InternalServerError from e


@json_api(blueprint, "/v1/anomaly-detection/detect")
@sentry_sdk.trace
def detect_anomalies_endpoint(data: DetectAnomaliesRequest) -> DetectAnomaliesResponse:
    sentry_sdk.set_tag(AnomalyDetectionTags.SEER_FUNCTIONALITY, "anomaly_detection")
    sentry_sdk.set_tag("organization_id", data.organization_id)
    sentry_sdk.set_tag("project_id", data.project_id)

    BASE_TRANSACTION_NAME = "seer.anomaly_detection.detect_endpoint"
    if isinstance(data.context, AlertInSeer):
        mode = AnomalyDetectionModes.STREAMING_ALERT
        transaction_name = f"{BASE_TRANSACTION_NAME}.streaming"
    elif isinstance(data.context, TimeSeriesWithHistory):
        mode = AnomalyDetectionModes.STREAMING_TS_WITH_HISTORY
        transaction_name = f"{BASE_TRANSACTION_NAME}.combo"
    else:
        mode = AnomalyDetectionModes.BATCH_TS_FULL
        transaction_name = f"{BASE_TRANSACTION_NAME}.batch"

    sentry_sdk.set_tag(AnomalyDetectionTags.MODE, mode)
    scope = sentry_sdk.get_current_scope()
    scope.set_transaction_name(transaction_name)

    try:
        with statsd.timed(f"{transaction_name}.duration"):
            response = load_anomaly_detection().detect_anomalies(data)
            statsd.increment(f"{transaction_name}.success")
    except ClientError as e:
        statsd.increment(f"{transaction_name}.client_error")
        response = DetectAnomaliesResponse(success=False, message=str(e))
    except ServerError:
        statsd.increment(f"{transaction_name}.server_error")
        raise

    return response


@json_api(blueprint, "/v1/anomaly-detection/store")
@sentry_sdk.trace
def store_data_endpoint(data: StoreDataRequest) -> StoreDataResponse:
    sentry_sdk.set_tag(AnomalyDetectionTags.SEER_FUNCTIONALITY, "anomaly_detection")
    sentry_sdk.set_tag("organization_id", data.organization_id)
    sentry_sdk.set_tag("project_id", data.project_id)
    sentry_sdk.set_tag("alert_id", data.alert.id)
    sentry_sdk.set_tag("alert_source_id", data.alert.source_id)
    sentry_sdk.set_tag("alert_source_type", data.alert.source_type)
    try:
        with statsd.timed("seer.anomaly_detection.store.duration"):
            response = load_anomaly_detection().store_data(data)
            statsd.increment("seer.anomaly_detection.store.success")
    except ClientError as e:
        statsd.increment("seer.anomaly_detection.store.client_error")
        response = StoreDataResponse(success=False, message=str(e))
    except ServerError:
        statsd.increment("seer.anomaly_detection.store.server_error")
        raise

    return response


@json_api(blueprint, "/v1/anomaly-detection/delete-alert-data")
@sentry_sdk.trace
def delete_alert__data_endpoint(
    data: DeleteAlertDataRequest,
) -> DeleteAlertDataResponse:
    sentry_sdk.set_tag(AnomalyDetectionTags.SEER_FUNCTIONALITY, "anomaly_detection")
    sentry_sdk.set_tag("organization_id", data.organization_id)
    if data.project_id is not None:
        sentry_sdk.set_tag("project_id", data.project_id)
    sentry_sdk.set_tag("alert_id", data.alert.id)
    sentry_sdk.set_tag("alert_source_id", data.alert.source_id)
    sentry_sdk.set_tag("alert_source_type", data.alert.source_type)
    try:
        with statsd.timed("seer.anomaly_detection.delete_alert_data.duration"):
            response = load_anomaly_detection().delete_alert_data(data)
            statsd.increment("seer.anomaly_detection.delete_alert_data.success")
    except ClientError as e:
        statsd.increment("seer.anomaly_detection.delete_alert_data.client_error")
        response = DeleteAlertDataResponse(success=False, message=str(e))
    except ServerError:
        statsd.increment("seer.anomaly_detection.delete_alert_data.server_error")
        raise

    return response


@json_api(blueprint, "/v1/anomaly-detection/compare-cohorts")
def compare_cohorts_endpoint(
    data: CompareCohortsRequest,
) -> CompareCohortsResponse:
    return compare_cohort(data)


@json_api(blueprint, "/v1/assisted-query/create-cache")
@sentry_sdk.trace
def create_cache_endpoint(data: CreateCacheRequest) -> CreateCacheResponse:
    try:
        with statsd.timed("seer.automation.assisted_query.create_cache.duration"):
            response = create_cache(data)
            statsd.increment("seer.automation.assisted_query.create_cache.success")
    except APITimeoutError as e:
        statsd.increment("seer.automation.assisted_query.create_cache.api_timeout")
        raise GatewayTimeout from e
    except Exception as e:
        statsd.increment("seer.automation.assisted_query.create_cache.server_error")
        logger.exception("Error creating cache")
        raise InternalServerError from e

    return response


@json_api(blueprint, "/v1/assisted-query/translate")
def translate_endpoint(data: TranslateRequest) -> TranslateResponses:
    try:
        with statsd.timed("seer.automation.assisted_query.translate.duration"):
            response = translate_query(data)
            statsd.increment("seer.automation.assisted_query.translate.success")
    except APITimeoutError as e:
        statsd.increment("seer.automation.assisted_query.translate.api_timeout")
        raise GatewayTimeout from e
    except Exception as e:
        statsd.increment("seer.automation.assisted_query.translate.error")
        logger.exception("Error translating query")
        raise InternalServerError from e

    return response


# =============================================================================
# Explorer Endpoints
# =============================================================================
# These endpoints provide LLM-powered chat functionality for issue analysis.
# Requires ANTHROPIC_API_KEY environment variable to be set.


@json_api(blueprint, "/v1/automation/explorer/runs")
def explorer_runs_endpoint(data: ExplorerRunsRequest) -> ExplorerRunsResponse:
    """List explorer runs for an organization."""
    try:
        runs = ExplorerRunState.list(
            organization_id=data.organization_id,
            category_key=data.category_key,
            category_value=data.category_value,
        )
        return ExplorerRunsResponse(data=runs)
    except Exception as e:
        logger.exception(f"Error listing explorer runs: {e}")
        return ExplorerRunsResponse(data=[])


@json_api(blueprint, "/v1/automation/explorer/chat")
def explorer_chat_endpoint(data: ExplorerChatRequest) -> ExplorerChatResponse:
    """Process an explorer chat message."""
    import os

    app_config = resolve(AppConfig)

    # Check if Anthropic API key is configured
    if not app_config.ANTHROPIC_API_KEY and not os.environ.get("ANTHROPIC_API_KEY"):
        return ExplorerChatResponse(
            status="not_available",
            message="Explorer requires ANTHROPIC_API_KEY to be configured",
            run_id=None,
        )

    try:
        # Create new run or get existing
        if data.run_id is None:
            state = ExplorerRunState.create(
                organization_id=data.organization_id,
                category_key=data.category_key,
                category_value=data.category_value,
                metadata=data.metadata,
            )
            run_id = state.run_id
        else:
            state = ExplorerRunState.get(data.run_id)
            if state is None:
                return ExplorerChatResponse(
                    status="error",
                    message=f"Run {data.run_id} not found",
                    run_id=None,
                )
            run_id = data.run_id

        # Get query from request
        query = data.get_query()
        if not query:
            return ExplorerChatResponse(
                status="error",
                message="No query provided",
                run_id=run_id,
            )

        # Convert tools to serializable format
        tools_data = None
        if data.tools:
            tools_data = [t.model_dump(mode="json") for t in data.tools]

        # Queue the processing task
        process_explorer_chat.delay(
            run_id=run_id,
            query=query,
            artifact_key=data.artifact_key,
            artifact_schema=data.artifact_schema,
            tools=tools_data,
            metadata=data.metadata,
        )

        return ExplorerChatResponse(
            status="processing",
            run_id=run_id,
            message=None,
        )

    except Exception as e:
        logger.exception(f"Error processing explorer chat: {e}")
        sentry_sdk.capture_exception(e)
        return ExplorerChatResponse(
            status="error",
            message=str(e),
            run_id=data.run_id,
        )


@json_api(blueprint, "/v1/automation/explorer/state")
def explorer_state_endpoint(data: ExplorerStateRequest) -> ExplorerStateResponse:
    """Get the current state of an explorer run."""
    try:
        state = ExplorerRunState.get(data.run_id)
        if state is None:
            return ExplorerStateResponse(
                session=None,
                status="not_found",
                message=f"Run {data.run_id} not found",
            )

        return ExplorerStateResponse(
            session=state.to_seer_run_state(),
            status="ok",
            message=None,
        )

    except Exception as e:
        logger.exception(f"Error getting explorer state: {e}")
        return ExplorerStateResponse(
            session=None,
            status="error",
            message=str(e),
        )


@json_api(blueprint, "/v1/automation/explorer/update")
def explorer_update_endpoint(data: ExplorerUpdateRequest) -> ExplorerUpdateResponse:
    """Update an explorer run."""
    try:
        state = ExplorerRunState.get(data.run_id)
        if state is None:
            return ExplorerUpdateResponse(
                status="error",
                message=f"Run {data.run_id} not found",
            )

        # Handle different update types
        if data.update_type == "cancel":
            from seer.automation.explorer.models import ExplorerStatus

            state.set_status(ExplorerStatus.COMPLETED)
            state.set_loading(False)

        return ExplorerUpdateResponse(status="ok", message=None)

    except Exception as e:
        logger.exception(f"Error updating explorer run: {e}")
        return ExplorerUpdateResponse(
            status="error",
            message=str(e),
        )


# ============================================
# Coding agent endpoints (required for Cursor/Copilot integration)
# ============================================


@json_api(blueprint, "/v1/automation/autofix/coding-agent/state/set")
def coding_agent_state_set_endpoint(
    data: CodingAgentStateSetRequest,
) -> CodingAgentStateSetResponse:
    """Store coding agent states in the autofix run.

    Sentry calls this after launching external coding agents (Cursor, GitHub Copilot)
    to persist their initial state in the autofix run.
    """
    from seer.automation.autofix.models import ExternalCodingAgentResult, ExternalCodingAgentState
    from seer.automation.autofix.state import ContinuationState

    try:
        state = ContinuationState(data.run_id)
        with state.update() as cur:
            for agent_data in data.coding_agent_states:
                agent_id = agent_data.get("id", "")
                if not agent_id:
                    continue
                results = [ExternalCodingAgentResult(**r) for r in agent_data.get("results", [])]
                agent_state = ExternalCodingAgentState(
                    id=agent_id,
                    status=agent_data.get("status", "pending"),
                    agent_url=agent_data.get("agent_url"),
                    provider=agent_data.get("provider", ""),
                    name=agent_data.get("name", ""),
                    started_at=agent_data.get("started_at"),
                    results=results,
                )
                cur.coding_agents[agent_id] = agent_state

        logger.info(
            "coding_agent.state_set",
            extra={
                "run_id": data.run_id,
                "num_agents": len(data.coding_agent_states),
            },
        )
        return CodingAgentStateSetResponse(status="ok")
    except Exception as e:
        logger.exception(f"Error setting coding agent state: {e}")
        return CodingAgentStateSetResponse(status="error", message=str(e))


@json_api(blueprint, "/v1/automation/autofix/coding-agent/state/update")
def coding_agent_state_update_endpoint(
    data: CodingAgentStateUpdateRequest,
) -> CodingAgentStateUpdateResponse:
    """Update a coding agent's state in its autofix run.

    Sentry calls this from webhook handlers (e.g., Cursor webhook) when an
    external coding agent reports status changes, completions, or failures.
    """
    from seer.automation.autofix.models import ExternalCodingAgentResult
    from seer.db import DbRunState, Session

    try:
        # Find the run containing this agent_id by scanning recent runs
        with Session() as session:
            # Search recent runs for the agent
            recent_runs = (
                session.query(DbRunState)
                .filter(DbRunState.type == "autofix")
                .order_by(DbRunState.id.desc())
                .limit(100)
                .all()
            )
            target_run_id = None
            for run_state in recent_runs:
                from seer.automation.autofix.state import ContinuationState

                try:
                    cs = ContinuationState(run_state.id)
                    cur = cs.get()
                    if data.agent_id in cur.coding_agents:
                        target_run_id = run_state.id
                        break
                except Exception:
                    continue

        if target_run_id is None:
            logger.warning(
                "coding_agent.state_update.agent_not_found",
                extra={"agent_id": data.agent_id},
            )
            return CodingAgentStateUpdateResponse(
                status="error", message=f"Agent {data.agent_id} not found"
            )

        from seer.automation.autofix.state import ContinuationState

        state = ContinuationState(target_run_id)
        with state.update() as cur:
            agent = cur.coding_agents.get(data.agent_id)
            if agent is None:
                return CodingAgentStateUpdateResponse(
                    status="error", message=f"Agent {data.agent_id} not found in run"
                )

            if data.updates.status is not None:
                agent.status = data.updates.status
            if data.updates.agent_url is not None:
                agent.agent_url = data.updates.agent_url
            if data.updates.results is not None:
                agent.results = [ExternalCodingAgentResult(**r) for r in data.updates.results]

        logger.info(
            "coding_agent.state_updated",
            extra={
                "agent_id": data.agent_id,
                "run_id": target_run_id,
                "status": data.updates.status,
            },
        )
        return CodingAgentStateUpdateResponse(status="ok")
    except Exception as e:
        logger.exception(f"Error updating coding agent state: {e}")
        return CodingAgentStateUpdateResponse(status="error", message=str(e))


@json_api(blueprint, "/v1/automation/autofix/prompt")
def autofix_prompt_endpoint(data: AutofixPromptRequest) -> AutofixPromptResponse:
    """Build and return the autofix prompt from the run's state.

    Sentry calls this to get the prompt text for external coding agents.
    The prompt includes the issue context, root cause analysis (if selected),
    and solution plan (if selected).
    """
    from seer.automation.autofix.state import ContinuationState

    try:
        state = ContinuationState(data.run_id)
        cur = state.get()

        parts: list[str] = []

        # Issue context from the request
        issue = cur.request.issue
        parts.append(f"Issue: {issue.title}")

        if hasattr(issue, "events") and issue.events:
            event = issue.events[0]
            # Extract exception info if available
            for entry in event.get("entries", []):
                if entry.get("type") == "exception":
                    for exc in entry.get("data", {}).get("values", []):
                        exc_type = exc.get("type", "")
                        exc_value = exc.get("value", "")
                        if exc_type or exc_value:
                            parts.append(f"\nException: {exc_type}: {exc_value}")
                        stacktrace = exc.get("stacktrace")
                        if stacktrace and stacktrace.get("frames"):
                            frames = stacktrace["frames"]
                            # Show last 5 in-app frames
                            in_app = [f for f in frames if f.get("in_app", False)]
                            relevant = in_app[-5:] if in_app else frames[-5:]
                            trace_lines = []
                            for frame in relevant:
                                filename = frame.get("filename", "?")
                                lineno = frame.get("lineNo") or frame.get("lineno", "?")
                                func = frame.get("function", "?")
                                trace_lines.append(f"  {filename}:{lineno} in {func}")
                            if trace_lines:
                                parts.append("Stacktrace (most relevant):")
                                parts.extend(trace_lines)

        # Root cause
        if data.include_root_cause:
            root_cause, instruction = cur.get_selected_root_cause()
            if root_cause is not None:
                parts.append("\n--- Root Cause Analysis ---")
                if isinstance(root_cause, str):
                    parts.append(root_cause)
                else:
                    if root_cause.description:
                        parts.append(root_cause.description)
                    if root_cause.root_cause_reproduction:
                        for i, timeline_event in enumerate(root_cause.root_cause_reproduction):
                            parts.append(f"\nStep {i + 1}: {timeline_event.title}")
                            if timeline_event.code_snippet_and_analysis:
                                parts.append(timeline_event.code_snippet_and_analysis)
                            code_file = getattr(timeline_event, "relevant_code_file", None)
                            if (
                                code_file
                                and hasattr(code_file, "file_path")
                                and code_file.file_path
                            ):
                                parts.append(f"File: {code_file.file_path}")
                if instruction:
                    parts.append(f"\nAdditional instruction: {instruction}")

        # Solution
        if data.include_solution:
            solution, mode = cur.get_selected_solution()
            if solution is not None:
                parts.append("\n--- Solution Plan ---")
                if isinstance(solution, str):
                    parts.append(solution)
                else:
                    for i, step in enumerate(solution):
                        if step.is_active:
                            parts.append(f"\nStep {i + 1}: {step.title}")
                            if step.code_snippet_and_analysis:
                                parts.append(step.code_snippet_and_analysis)

        # Repo context
        if cur.request.repos:
            parts.append("\n--- Repositories ---")
            for repo in cur.request.repos:
                repo_name = (
                    f"{repo.owner}/{repo.name}" if hasattr(repo, "owner") else repo.full_name
                )
                parts.append(f"- {repo_name}")

        prompt_text = "\n".join(parts)
        return AutofixPromptResponse(prompt=prompt_text)
    except Exception as e:
        logger.exception(f"Error building autofix prompt: {e}")
        return AutofixPromptResponse(prompt=None, message=str(e))


# ============================================
# Additional stub endpoints for Sentry 26.x compatibility
# ============================================


@json_api(blueprint, "/v1/automation/codegen/pr-review/rerun")
def codegen_pr_review_rerun_endpoint(
    data: CodegenPrReviewRerunRequest,
) -> CodegenPrReviewRerunResponse:
    """Rerun PR review - stub for self-hosted."""
    return CodegenPrReviewRerunResponse(
        status="not_available", message="PR review rerun not available in self-hosted mode"
    )


@json_api(blueprint, "/v1/project-preference/bulk")
def get_project_preference_bulk_endpoint(
    data: ProjectPreferenceBulkRequest,
) -> ProjectPreferenceBulkResponse:
    """Get bulk project preferences - stub for self-hosted."""
    return ProjectPreferenceBulkResponse(preferences={}, message="Bulk preferences retrieved")


@json_api(blueprint, "/v1/project-preference/bulk-set")
def set_project_preference_bulk_endpoint(
    data: ProjectPreferenceBulkSetRequest,
) -> ProjectPreferenceBulkSetResponse:
    """Set bulk project preferences - stub for self-hosted."""
    return ProjectPreferenceBulkSetResponse(status="ok", message="Bulk preferences set")


# ============================================
# Sentry 26.2.0 endpoints — implemented using existing Seer infrastructure
# ============================================


@json_api(blueprint, "/v1/llm/generate")
def llm_generate_endpoint(data: LlmGenerateRequest) -> LlmGenerateResponse:
    """LLM text generation for issue view titles and similar features.

    Uses OpenAI via the existing LlmClient infrastructure. Sentry sends
    provider="gemini" but we route to OpenAI for self-hosted.
    """
    from seer.automation.agent.client import LlmClient, OpenAiProvider

    try:
        # Map Sentry's model names to OpenAI models
        # Sentry sends "flash" (Gemini Flash) — we use gpt-4o-mini as equivalent
        model_mapping = {
            "flash": "gpt-4o-mini",
            "pro": "gpt-4o",
        }
        model_name = model_mapping.get(data.model, "gpt-4o-mini")
        model = OpenAiProvider.model(model_name)

        llm_client = LlmClient()
        response = llm_client.generate_text(
            prompt=data.prompt,
            model=model,
            system_prompt=data.system_prompt or None,
            temperature=data.temperature,
            max_tokens=data.max_tokens,
        )

        content = response.message.content if response.message else None
        logger.info(
            "llm_generate.success",
            extra={"referrer": data.referrer, "model": model_name},
        )
        return LlmGenerateResponse(content=content)
    except Exception as e:
        logger.exception(f"Error in LLM generate: {e}")
        return LlmGenerateResponse(content=None)


@json_api(blueprint, "/v1/assisted-query/translate-agentic")
def assisted_query_translate_agentic_endpoint(
    data: AssistedQueryTranslateAgenticRequest,
) -> AssistedQueryTranslateAgenticResponse:
    """Translate natural language query to Sentry query syntax using LLM.

    Uses the existing assisted_query translate_query infrastructure.
    """
    from seer.automation.assisted_query.assisted_query import translate_query as do_translate
    from seer.automation.assisted_query.models import TranslateRequest

    try:
        if not data.natural_language_query:
            return AssistedQueryTranslateAgenticResponse(query=None)

        result = do_translate(
            TranslateRequest(
                org_id=data.org_id or 0,
                project_ids=data.project_ids,
                natural_language_query=data.natural_language_query,
            )
        )

        if result.responses:
            first = result.responses[0]
            logger.info(
                "assisted_query.translate_agentic.success",
                extra={"query": first.query, "strategy": data.strategy},
            )
            return AssistedQueryTranslateAgenticResponse(query=first.query)

        return AssistedQueryTranslateAgenticResponse(query=None)
    except Exception as e:
        logger.exception(f"Error in agentic query translation: {e}")
        return AssistedQueryTranslateAgenticResponse(query=None)


@json_api(blueprint, "/v1/assisted-query/start")
def assisted_query_start_endpoint(
    data: AssistedQueryStartRequest,
) -> AssistedQueryStartResponse:
    """Start an async assisted query run.

    Creates a run state and queues translation via the Explorer run infrastructure.
    Returns a run_id for subsequent state polling.
    """
    try:
        if not data.natural_language_query:
            return AssistedQueryStartResponse(run_id=None)

        state = ExplorerRunState.create(
            organization_id=data.org_id,
            category_key="assisted-query",
            category_value=data.strategy,
            metadata={
                "natural_language_query": data.natural_language_query,
                "project_ids": data.project_ids,
                "org_slug": data.org_slug,
                "strategy": data.strategy,
            },
        )

        # Queue async processing using the explorer chat task infrastructure
        process_explorer_chat.delay(
            run_id=state.run_id,
            query=data.natural_language_query,
            organization_id=data.org_id,
            category_key="assisted-query",
            category_value=data.strategy,
        )

        logger.info(
            "assisted_query.start.success",
            extra={"run_id": state.run_id, "strategy": data.strategy},
        )
        return AssistedQueryStartResponse(run_id=state.run_id)
    except Exception as e:
        logger.exception(f"Error starting assisted query: {e}")
        return AssistedQueryStartResponse(run_id=None)


@json_api(blueprint, "/v1/assisted-query/state")
def assisted_query_state_endpoint(
    data: AssistedQueryStateRequest,
) -> AssistedQueryStateResponse:
    """Get the current state of an assisted query run.

    Loads state from DB using the Explorer run state infrastructure.
    """
    try:
        run_state = ExplorerRunState.get(data.run_id)
        if run_state is None:
            return AssistedQueryStateResponse(session=None)

        cur = run_state.get_state()
        session_data = cur.model_dump(mode="json")
        return AssistedQueryStateResponse(session=session_data)
    except Exception as e:
        logger.warning(
            "assisted_query.state.error",
            extra={"run_id": data.run_id, "error": str(e)},
        )
        return AssistedQueryStateResponse(session=None)


@json_api(blueprint, "/v1/anomaly-detection/alert-data")
def anomaly_detection_alert_data_endpoint(
    data: AnomalyDetectionAlertDataRequest,
) -> AnomalyDetectionAlertDataResponse:
    """Get anomaly detection alert threshold data.

    Queries the existing anomaly detection DB for time series data
    and prediction bounds for a given alert in a time window.
    """
    from seer.anomaly_detection.accessors import DbAlertDataAccessor

    try:
        alert_info = data.alert
        alert_id = alert_info.get("id")
        source_id = alert_info.get("source_id")
        source_type = alert_info.get("source_type")

        if alert_id is None and source_id is None:
            return AnomalyDetectionAlertDataResponse(
                success=False, message="Either alert id or source_id required", data=[]
            )

        accessor = DbAlertDataAccessor()
        alert = accessor.query(
            external_alert_id=alert_id,
            external_alert_source_id=source_id,
            external_alert_source_type=source_type,
        )

        if alert is None:
            return AnomalyDetectionAlertDataResponse(
                success=True, message="Alert not found", data=[]
            )

        # TimeSeries has timestamps/values as numpy arrays + prophet_predictions
        ts = alert.timeseries
        result_data = []

        for i in range(len(ts.timestamps)):
            timestamp = float(ts.timestamps[i])
            if data.start <= timestamp <= data.end:
                point = {
                    "timestamp": timestamp,
                    "value": float(ts.values[i]),
                }
                # Include prediction bounds from prophet if available
                predictions = ts.prophet_predictions or alert.prophet_predictions
                if predictions and i < len(predictions.yhat_upper):
                    point["yhat_upper"] = float(predictions.yhat_upper[i])
                    point["yhat_lower"] = float(predictions.yhat_lower[i])
                result_data.append(point)

        return AnomalyDetectionAlertDataResponse(success=True, data=result_data)
    except Exception as e:
        logger.exception(f"Error getting anomaly detection alert data: {e}")
        return AnomalyDetectionAlertDataResponse(success=False, message=str(e), data=[])


@json_api(blueprint, "/v1/workflows/compare/cohort")
def workflows_compare_cohort_endpoint(
    data: WorkflowsCompareCohortRequest,
) -> WorkflowsCompareCohortResponse:
    """Compare cohort distributions and rank attributes by suspiciousness.

    Uses the existing CompareService with KL divergence and entropy metrics.
    """
    from seer.workflows.compare.models import (
        AttributeDistributions,
        CompareCohortsConfig,
        CompareCohortsMeta,
        CompareCohortsRequest,
        StatsAttribute,
        StatsAttributeBucket,
        StatsCohort,
    )
    from seer.workflows.compare.service import compare_cohort

    try:
        # Build the proper request model from the raw data
        # Sentry sends: baseline, outliers (lists), total_baseline, total_outliers, config, meta
        baseline_attrs = []
        for item in data.baseline:
            if isinstance(item, dict) and "attributeName" in item:
                buckets = [
                    StatsAttributeBucket(
                        attributeValue=b.get("attributeValue", ""),
                        attributeValueCount=b.get("attributeValueCount", 0),
                    )
                    for b in item.get("buckets", [])
                ]
                baseline_attrs.append(
                    StatsAttribute(attributeName=item["attributeName"], buckets=buckets)
                )

        outlier_attrs = []
        for item in data.outliers:
            if isinstance(item, dict) and "attributeName" in item:
                buckets = [
                    StatsAttributeBucket(
                        attributeValue=b.get("attributeValue", ""),
                        attributeValueCount=b.get("attributeValueCount", 0),
                    )
                    for b in item.get("buckets", [])
                ]
                outlier_attrs.append(
                    StatsAttribute(attributeName=item["attributeName"], buckets=buckets)
                )

        config_data = data.config or {}
        meta_data = data.meta or {}

        request = CompareCohortsRequest(
            baseline=StatsCohort(
                totalCount=data.total_baseline,
                attributeDistributions=AttributeDistributions(attributes=baseline_attrs),
            ),
            selection=StatsCohort(
                totalCount=data.total_outliers,
                attributeDistributions=AttributeDistributions(attributes=outlier_attrs),
            ),
            config=CompareCohortsConfig(**{k: v for k, v in config_data.items() if v is not None}),
            meta=CompareCohortsMeta(referrer=meta_data.get("referrer", "unknown")),
        )

        result = compare_cohort(request)
        return WorkflowsCompareCohortResponse(results=result.results)
    except Exception as e:
        logger.exception(f"Error in cohort comparison: {e}")
        return WorkflowsCompareCohortResponse(results=[])


@json_api(blueprint, "/v0/issues/supergroups")
def supergroups_endpoint(data: SupergroupsRequest) -> SupergroupsResponse:
    """Supergroups embedding for issue clustering.

    TODO: Implement with embedding infrastructure.
    Currently accepts and acknowledges the request without processing.
    """
    logger.info(
        "supergroups.acknowledged",
        extra={
            "organization_id": data.organization_id,
            "group_id": data.group_id,
        },
    )
    return SupergroupsResponse(status="ok")


@blueprint.route("/health/live", methods=["GET"])
@inject
def health_check(app_config: AppConfig = injected):
    from seer.inference_models import models_loading_status

    status = models_loading_status()

    if status == LoadingResult.FAILED:
        statsd.increment("seer.health.live.500")
        return "Models failed to load", 500

    # Only run model tests if models are already loaded
    if status == LoadingResult.DONE:
        if app_config.is_grouping_enabled and not test_grouping_model():
            return "Grouping model inference failed", 500

    statsd.increment("seer.health.live.200")
    return "", 200


@blueprint.route("/health/ready", methods=["GET"])
@inject
def ready_check(app_config: AppConfig = injected):
    from seer.inference_models import models_loading_status

    status = models_loading_status()
    if app_config.SMOKE_CHECK:
        smoke_status = check_smoke_test()
        logger.info(f"Celery smoke status: {smoke_status}")
        status = min(status, smoke_status)

    # Only run model tests if models are already loaded
    if status == LoadingResult.DONE:
        if app_config.is_grouping_enabled and not test_grouping_model():
            return "Grouping model inference failed", 500

    if status == LoadingResult.FAILED:
        statsd.increment("seer.health.ready.500")
        return "", 500
    if status == LoadingResult.DONE:
        statsd.increment("seer.health.ready.200")
        return "", 200
    statsd.increment("seer.health.ready.503")
    return "", 503


@module.provider
def base_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    return app


@inject
def start_app(app: Flask = injected) -> Flask:
    bootup(
        start_model_loading=True,
        integrations=[
            FlaskIntegration(),
            LoggingIntegration(
                level=logging.DEBUG,  # Capture debug and above as breadcrumbs
            ),
        ],
    )
    return app
