"""Agent initialization and lifecycle management."""
import os
import sys
import logging
import yaml
import smolagents
from smolagents import ToolCallingAgent, ToolCollection
from mcp import StdioServerParameters

from config import (
    MILVUS_URI, MILVUS_TOKEN, EMBEDDING_BASE_URL,
    EMBEDDING_MODEL, EMBEDDING_API_KEY, FHIR_BASE_URL,
)
from agent.model import model
from agent.streaming import _wrap_tool_forward

logger = logging.getLogger("medgemma-agent")

# Global agent state
agent = None
tool_collection_context = None

# MCP Server parameters for StdIO transport (Milvus + FHIR tools)
server_params = StdioServerParameters(
    command=sys.executable,
    args=[os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp_server.py")],
    env={
        "MILVUS_URI": MILVUS_URI,
        "MILVUS_TOKEN": MILVUS_TOKEN,
        "EMBEDDING_BASE_URL": EMBEDDING_BASE_URL,
        "EMBEDDING_MODEL": EMBEDDING_MODEL,
        "EMBEDDING_API_KEY": EMBEDDING_API_KEY,
        "FHIR_BASE_URL": FHIR_BASE_URL,
        **os.environ,
    },
)


def initialize_agent():
    """Initialize the MCP tool collection and ToolCallingAgent.

    Must be called during app lifespan startup.
    """
    global agent, tool_collection_context

    logger.info("Starting MCP tool collection...")
    tool_collection_context = ToolCollection.from_mcp(server_params, trust_remote_code=True)
    tool_collection = tool_collection_context.__enter__()

    logger.info("MCP tools discovered: %s", [t.name for t in tool_collection.tools])
    for t in tool_collection.tools:
        logger.info("  Tool '%s': %s", t.name, t.description)

    all_tools = list(tool_collection.tools)

    # Wrap tool forward() methods to emit streaming status events
    for t in all_tools:
        _wrap_tool_forward(t)
    logger.info("Wrapped %d tools for streaming status events", len(all_tools))

    # Load default prompt templates and inject efficiency instruction into initial_plan
    _prompts_path = os.path.join(
        os.path.dirname(smolagents.__file__), "prompts", "toolcalling_agent.yaml"
    )
    with open(_prompts_path) as f:
        prompt_templates = yaml.safe_load(f)
    _efficiency_note = (
        "IMPORTANT: You must be efficient with tool calls. Plan your approach to use "
        "at most 5 tool calls total before providing a final answer. Prioritize the most "
        "informative tool calls first and combine information from fewer calls rather than "
        "making many small queries."
    )
    prompt_templates["planning"]["initial_plan"] = (
        _efficiency_note + "\n\n" + prompt_templates["planning"]["initial_plan"]
    )

    agent = ToolCallingAgent(
        tools=all_tools,
        model=model,
        max_steps=15,
        verbosity_level=2,
        planning_interval=50,
        prompt_templates=prompt_templates,
    )
    logger.info("Agent initialized with %d tools", len(all_tools))


def shutdown_agent():
    """Clean up MCP tool collection. Call during app shutdown."""
    global tool_collection_context
    if tool_collection_context:
        tool_collection_context.__exit__(None, None, None)
        tool_collection_context = None
