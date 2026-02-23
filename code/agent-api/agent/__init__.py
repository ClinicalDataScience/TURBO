"""Agent module: LLM model wrapper, streaming helpers, and agent setup."""
from agent.model import ManualToolCallingModel, llm_client, model, LLM_TIMEOUT
from agent.streaming import _tls, _wrap_tool_forward, _tool_status_message
from agent.setup import agent, initialize_agent, shutdown_agent, server_params
