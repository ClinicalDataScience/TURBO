"""ManualToolCallingModel wrapper and LLM client initialization."""
import json
import logging
import os
import httpx
from openai import OpenAI
from smolagents import OpenAIModel

from config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY

logger = logging.getLogger("medgemma-agent")

# Timeout with generous connect timeout for external endpoints
LLM_TIMEOUT = httpx.Timeout(1200.0, connect=120.0)

# Initialize LLM client for keypoint extraction (OpenAI-compatible)
llm_client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL if LLM_BASE_URL.endswith("/v1") else LLM_BASE_URL + "/v1",
    timeout=LLM_TIMEOUT,
)


class ManualToolCallingModel(OpenAIModel):
    """Wrapper for OpenAI-compatible models that lack native tool calling.

    Strips the ``tools`` / ``tool_choice`` parameters from the API request,
    injects tool descriptions into the prompt instead, and returns
    ``tool_calls=None`` so smolagents' built-in fallback
    (``parse_tool_calls`` → ``parse_json_blob``) extracts the JSON tool
    call from the model's text output.
    """

    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs):
        from smolagents.models import ChatMessage, TokenUsage

        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            response_format=response_format,
            tools_to_call_from=tools_to_call_from,
            model=self.model_id,
            custom_role_conversions=self.custom_role_conversions,
            convert_images_to_image_urls=True,
            **kwargs,
        )

        # Strip native tool params
        tool_schemas = completion_kwargs.pop("tools", None)
        completion_kwargs.pop("tool_choice", None)

        # Inject tool descriptions into the messages
        if tool_schemas and tools_to_call_from:
            tool_prompt = self._build_tool_prompt(tool_schemas)
            msgs = completion_kwargs["messages"]
            if msgs and msgs[0].get("role") == "system":
                cur = msgs[0]["content"]
                if isinstance(cur, list):
                    cur.append({"type": "text", "text": "\n\n" + tool_prompt})
                else:
                    msgs[0]["content"] = str(cur) + "\n\n" + tool_prompt
            else:
                msgs.insert(0, {"role": "system", "content": tool_prompt})

        response = self.client.chat.completions.create(**completion_kwargs)
        content = response.choices[0].message.content

        from agent.streaming import tool_logger
        tool_logger.info("LLM_RESPONSE | %s", content)

        return ChatMessage(
            role=response.choices[0].message.role,
            content=content,
            tool_calls=None,
            raw=response,
            token_usage=TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            ),
        )

    @staticmethod
    def _build_tool_prompt(tool_schemas: list[dict]) -> str:
        lines = ["You have access to the following tools:\n"]
        for schema in tool_schemas:
            func = schema["function"]
            lines.append(f"Tool: {func['name']}")
            lines.append(f"Description: {func['description']}")
            lines.append(f"Parameters: {json.dumps(func['parameters'], indent=2)}\n")
        lines.append(
            'To call a tool, respond with ONLY a JSON object like:\n'
            '{"name": "tool_name", "arguments": {"param1": "value1"}}\n\n'
            'If you do not need to call a tool, respond with:\n'
            '{"name": "final_answer", "arguments": {"answer": "your answer here"}}'
        )
        return "\n".join(lines)


# Initialize LLM model for agent
model = ManualToolCallingModel(
    model_id=LLM_MODEL,
    api_base=LLM_BASE_URL,
    api_key=LLM_API_KEY,
    client_kwargs={"timeout": LLM_TIMEOUT},
)
