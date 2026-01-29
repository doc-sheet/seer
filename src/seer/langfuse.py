import logging
from typing import Any

from langfuse import Langfuse, get_client

from seer.configuration import AppConfig
from seer.dependency_injection import Module, inject, injected

logger = logging.getLogger(__name__)

langfuse_module = Module()


class LangfuseContext:
    """
    Compatibility layer for langfuse 3.x.
    Provides backward-compatible methods that were in langfuse.decorators.langfuse_context.
    """

    def get_current_trace_id(self) -> str | None:
        return get_client().get_current_trace_id()

    def get_current_observation_id(self) -> str | None:
        return get_client().get_current_observation_id()

    def get_current_trace_url(self) -> str | None:
        return get_client().get_trace_url()

    def update_current_trace(self, **kwargs) -> None:
        get_client().update_current_trace(**kwargs)

    def update_current_observation(
        self,
        *,
        name: str | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        """
        Backward-compatible method for updating current observation.
        In langfuse 3.x, this maps to update_current_generation for model/usage
        or update_current_span for other updates.
        """
        client = get_client()
        if model is not None or usage is not None:
            # Use update_current_generation for model/usage updates
            client.update_current_generation(
                name=name,
                model=model,
                usage_details=usage,
                metadata=metadata,
            )
        else:
            # Use update_current_span for other updates
            client.update_current_span(
                name=name,
                metadata=metadata,
            )


# Global instance for backward compatibility
langfuse_context = LangfuseContext()


@langfuse_module.provider
def provide_langfuse(config: AppConfig = injected) -> Langfuse:
    return Langfuse(
        public_key=config.LANGFUSE_PUBLIC_KEY,
        secret_key=config.LANGFUSE_SECRET_KEY,
        host=config.LANGFUSE_HOST,
        enabled=bool(config.LANGFUSE_HOST),
    )


@inject
def append_langfuse_trace_tags(new_tags: list[str], langfuse: Langfuse = injected):
    """
    Appends traces to the current trace in the context.
    MUST BE RUN WITHIN A LANGFUSE TRACE!
    """
    try:
        trace_id = langfuse_context.get_current_trace_id()
        if trace_id:
            trace = langfuse.get_trace(trace_id)
            langfuse_context.update_current_trace(
                tags=(trace.tags or []) + new_tags,
            )
    except Exception as e:
        logger.exception(e)


@inject
def append_langfuse_observation_metadata(new_metadata: dict, langfuse: Langfuse = injected):
    """
    Appends metadata to the current observation in the context.
    MUST BE RUN WITHIN A LANGFUSE OBSERVATION!
    """
    try:
        langfuse_context.update_current_observation(
            metadata=new_metadata,
        )
    except Exception as e:
        logger.exception(e)


langfuse_module.enable()
