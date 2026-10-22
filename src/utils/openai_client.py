import base64
import logging
import asyncio
from openai import AsyncOpenAI, OpenAIError
from typing import Dict, Any, Optional, List # Added List import

from . import llm_result
from .llm_formatting import prepare_llm_messages
from .llm_result import ChatResult
from .usage import record_usage

logger = logging.getLogger('openai_client')

def _kind_for_openai_error(error: Exception) -> str:
    """Maps an openai SDK exception to a ChatResult error kind."""
    status = getattr(error, "status_code", None)
    name = type(error).__name__
    if name == "APITimeoutError":
        return llm_result.TIMEOUT
    if name == "APIConnectionError":
        return llm_result.NETWORK
    if isinstance(status, int):
        return llm_result.kind_for_status(status, str(error))
    return llm_result.PROVIDER


def _usage_from(response) -> Dict[str, Any]:
    """Token counts from an SDK response (OpenAI reports no cost)."""
    usage = getattr(response, "usage", None)
    if not usage:
        return {}
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0) if details else 0,
        "cost": 0.0,
    }


class OpenAIClient:
    """Client for interacting with the OpenAI API for chat completions and image generation."""

    # chat() returns a ChatResult; see llm_result.call_chat
    supports_chat_result = True

    # OpenAI's models endpoint exposes no capability metadata, so vision
    # support is matched by family prefix rather than looked up.
    VISION_MODEL_PREFIXES = (
        "gpt-4o", "gpt-4-turbo", "gpt-4.1", "gpt-4.5", "gpt-5",
        "chatgpt-4o", "o1", "o3", "o4",
    )
    # Reasoning models that are text-only despite matching a prefix above.
    NON_VISION_MODELS = ("o1-mini", "o1-preview", "o3-mini")

    def __init__(self, api_key: str):
        """
        Initializes the OpenAI client.

        Args:
            api_key: The OpenAI API key.
        """
        if not api_key:
            logger.warning("OpenAI API key is not configured. Image generation will not work.")
            self.client = None
        else:
            try:
                # Use AsyncOpenAI for compatibility with discord.py's async nature
                self.client = AsyncOpenAI(api_key=api_key)
                logger.info("OpenAI client initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")
                self.client = None

    async def generate_image(self, prompt: str, model: str, size: str, quality: str = None, style: str = None) -> dict:
        """
        Generates an image using the OpenAI API (Dall-E 2 or Dall-E 3).

        Args:
            prompt: The text prompt describing the image.
            model: The model to use ('dall-e-2' or 'dall-e-3').
            size: The desired size ('1024x1024', '1792x1024', '1024x1792' for D3; '1024x1024', '512x512', '256x256' for D2).
                  Note: The unified command will likely enforce '1024x1024'.
            quality: The quality ('standard' or 'hd'). Only applicable for 'dall-e-3'.
            style: The style ('vivid' or 'natural'). Only applicable for 'dall-e-3'.

        Returns:
            A dictionary containing either the image URL on success or an error message on failure.
            Example success: {"success": True, "image_url": "...", "model_used": "dall-e-3", "revised_prompt": "..."}
            Example failure: {"success": False, "error": "API key invalid."}
        """
        if not self.client:
            return {"success": False, "error": "OpenAI client is not initialized (API key missing or invalid)."}

        params = {
            "prompt": prompt,
            "model": model,
            "size": size,
            "n": 1,
            "response_format": "url",
        }

        # Add quality and style only if model is dall-e-3 and they are provided
        if model == "dall-e-3":
            if quality:
                params["quality"] = quality
            if style:
                params["style"] = style
        elif quality or style:
             logger.warning(f"Quality/Style parameters provided for non-Dall-E 3 model ({model}). Ignoring.")


        logger.info(f"Requesting OpenAI image generation with params: {params}")

        try:
            response = await self.client.images.generate(**params)

            if response.data and len(response.data) > 0:
                image_data = response.data[0]
                result = {
                    "success": True,
                    "image_url": image_data.url,
                    "model_used": model, # Return the requested model
                    "revised_prompt": getattr(image_data, 'revised_prompt', None) # DALL-E 3 might revise prompts
                }
                logger.info(f"OpenAI image generated successfully: {image_data.url}")
                return result
            else:
                logger.error("OpenAI API returned success but no image data.")
                return {"success": False, "error": "API returned success but no image data found."}

        except OpenAIError as e:
            logger.error(f"OpenAI API error during image generation: {e}")
            # Try to provide a more specific error message if available
            error_message = str(e)
            if hasattr(e, 'message'):
                error_message = e.message
            elif hasattr(e, 'body') and e.body and 'message' in e.body:
                 error_message = e.body['message']

            return {"success": False, "error": f"OpenAI API Error: {error_message}"}
        except Exception as e:
            logger.exception(f"Unexpected error during OpenAI image generation: {e}")
            return {"success": False, "error": f"An unexpected error occurred: {str(e)}"}

    def model_supports_vision(self, model_name: str) -> bool:
        """Checks if the specified OpenAI model name supports vision."""
        name = (model_name or "").lower()
        if name.startswith(self.NON_VISION_MODELS):
            return False
        return name.startswith(self.VISION_MODEL_PREFIXES)

    async def send_message_with_history(self, messages: List[Dict[str, str]], model: str,
                                        system_prompt: Optional[str] = None, **kwargs):
        """Legacy interface: returns text, a tool-call dict, or a "⚠️ ..." error string."""
        result = await self.chat(messages=messages, model=model, system_prompt=system_prompt, **kwargs)
        return result.to_legacy()

    async def chat(self, messages: List[Dict[str, Any]], model: str,
                   system_prompt: Optional[str] = None, **kwargs) -> ChatResult:
        """
        Sends a message to the OpenAI Chat Completions API using conversation history.

        Args:
            messages: A list of message dictionaries, e.g., [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi!"}].
                      For vision models, content can be a list: [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]
            model: The specific OpenAI model to use (e.g., "gpt-4o", "gpt-3.5-turbo").
            system_prompt: An optional system prompt to guide the AI.
            **kwargs: Potential additional arguments:
                      'images' for vision models: [{'data': bytes, 'type': 'image/jpeg'}]
                      'tools' for native tool calling: List of tool definitions (OpenAI function schema)
                      'tool_choice' for tool calling: "auto", "none", or specific tool

        Returns:
            The text content of the AI's response, a dict with tool_calls if the model
            requested tool execution, or a formatted error string starting with "⚠️ Error: ".
        """
        if not self.client:
            return ChatResult.failure(llm_result.UNAVAILABLE, model, provider="openai")

        api_messages = []

        # Add system prompt if provided
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})

        # Process images if provided and model supports vision
        images_to_include = []
        if self.model_supports_vision(model):
            images_arg = kwargs.get('images', [])
            if images_arg:
                logger.info(f"Processing {len(images_arg)} image(s) for vision model {model}")
                for img in images_arg:
                    try:
                        base64_image = base64.b64encode(img['data']).decode('utf-8')
                        images_to_include.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{img['type']};base64,{base64_image}"}
                        })
                    except Exception as e:
                        logger.error(f"Failed to encode image for OpenAI: {e}")
                        return ChatResult.failure(llm_result.BAD_RESPONSE, model, detail=repr(e),
                                                  provider="openai")

        # Label user turns with their speaker and drop bookkeeping keys
        messages = prepare_llm_messages(messages)

        # Format conversation history
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            # Assistant tool-call turns legitimately have empty content;
            # dropping them would orphan the following role="tool" messages
            # and the API would reject the request.
            if not role or (not content and not tool_calls):
                logger.warning(f"Skipping message with missing role or content: {msg}")
                continue

            # For the last user message, combine text and images if applicable
            if role == "user" and msg == messages[-1] and images_to_include:
                 # Content becomes a list for multimodal input
                 user_content_list = [{"type": "text", "text": content}]
                 user_content_list.extend(images_to_include)
                 api_messages.append({"role": "user", "content": user_content_list})
            elif role == "assistant" and tool_calls:
                 # Tool-call turn: tool_calls must round-trip for the API to
                 # accept the tool results that follow
                 api_messages.append({"role": role, "content": content or "", "tool_calls": tool_calls})
            elif role == "tool":
                 api_messages.append({"role": role, "content": content, "tool_call_id": msg.get("tool_call_id", "")})
            else:
                 # Standard text message
                 api_messages.append({"role": role, "content": content})


        logger.debug(f"Sending request to OpenAI model '{model}' with {len(api_messages)} messages.")

        # Build API call parameters
        api_params = {
            "model": model,
            "messages": api_messages,
        }

        # Add tools if provided (native function/tool calling)
        tools = kwargs.get('tools')
        if tools:
            api_params["tools"] = tools
            tool_choice = kwargs.get('tool_choice', 'auto')
            api_params["tool_choice"] = tool_choice
            logger.info(f"Tool calling enabled with {len(tools)} tool(s)")

        try:
            response = await self.client.chat.completions.create(**api_params)

            if response.choices and response.choices[0].message:
                message = response.choices[0].message
                # Check for tool calls first
                if hasattr(message, 'tool_calls') and message.tool_calls:
                    logger.info(f"Model requested {len(message.tool_calls)} tool call(s)")
                    # Convert tool_calls to serializable format
                    tool_calls_data = []
                    for tc in message.tool_calls:
                        tool_calls_data.append({
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        })
                    result = ChatResult(content=message.content, tool_calls=tool_calls_data,
                                        usage=_usage_from(response), model=model, provider="openai")
                else:
                    ai_response = message.content
                    result = ChatResult(content=ai_response.strip() if ai_response else "",
                                        usage=_usage_from(response), model=model, provider="openai")
                record_usage(result)
                return result
            logger.error(f"OpenAI API returned no response choices for model '{model}'. Full response: {response}")
            return ChatResult.failure(llm_result.BAD_RESPONSE, model, detail=repr(response)[:500],
                                      provider="openai")

        except OpenAIError as e:
            # The SDK already retries rate limits, timeouts and 5xx (max_retries)
            logger.error(f"OpenAI API error for model '{model}': {e}")
            return ChatResult.failure(_kind_for_openai_error(e), model, detail=str(e)[:500],
                                      provider="openai")
        except Exception as e:
            logger.exception(f"Unexpected error during OpenAI chat completion for model '{model}': {e}")
            return ChatResult.failure(llm_result.BAD_RESPONSE, model, detail=repr(e), provider="openai")
