# Seer Explorer - LLM-powered chat for issue analysis
#
# This module provides full Explorer functionality with Claude integration.
# Requires ANTHROPIC_API_KEY environment variable to be set.
#
# Components:
# - models.py: Request/response models
# - state.py: State management using DbRunState
# - agent.py: ExplorerAgent class for LLM integration
# - tasks.py: Celery tasks for async processing
# - prompts.py: System prompts for Claude
