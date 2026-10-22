import aiohttp
import json
import asyncio
import socket
import logging
import base64
from datetime import datetime, timedelta
from io import BytesIO
from typing import List, Dict, Any, Optional

from . import llm_result
from .llm_formatting import prepare_llm_messages
from .llm_result import ChatResult
from .usage import record_usage

logger = logging.getLogger('openrouter_client')

def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Seconds from a Retry-After header (numeric form only)."""
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _normalize_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Keeps the usage fields Gideon records: token counts, cost, cached tokens."""
    if not isinstance(usage, dict) or not usage:
        return {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "cost": float(usage.get("cost") or 0.0),
    }


class OpenRouterClient:
    """Client for interacting with the OpenRouter API."""

    # chat() returns a ChatResult; see llm_result.call_chat
    supports_chat_result = True
    
    def __init__(self, api_key: str, system_prompt: str, default_model: str):
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.model = default_model
        self.base_url = "https://openrouter.ai/api/v1"
        self._session: Optional[aiohttp.ClientSession] = None

        # Vision capability is read from the /models API rather than a
        # maintained list. Populated as a side effect of get_available_models().
        self._vision_capabilities: Dict[str, bool] = {}
        self._capabilities_updated: Optional[datetime] = None
        self._capabilities_ttl = timedelta(hours=12)
        self._capabilities_lock = asyncio.Lock()

    @staticmethod
    def _detect_vision_support(model_info: Dict[str, Any]) -> Optional[bool]:
        """Read image-input support from a /models entry.

        Returns None when the entry carries no modality metadata at all, so
        callers can tell "known to be text-only" apart from "don't know".
        """
        architecture = model_info.get("architecture") or {}

        input_modalities = architecture.get("input_modalities")
        if isinstance(input_modalities, list) and input_modalities:
            return "image" in [str(m).lower() for m in input_modalities]

        # Older entries only carry a combined string like "text+image->text".
        modality_str = architecture.get("modality") or ""
        if "->" in modality_str:
            return "image" in modality_str.split("->")[0].lower()

        return None

    def _normalize_model_id(self, model_name: Optional[str] = None) -> str:
        """Resolve a model name to the ID used by the OpenRouter API."""
        model_id = model_name or self.model or ""
        if model_id.startswith("openrouter/"):
            model_id = model_id.split("/", 1)[1]
        return model_id

    async def _ensure_capabilities(self) -> None:
        """Populate the capability map, refreshing it once the TTL expires."""
        def is_stale() -> bool:
            return (
                self._capabilities_updated is None
                or datetime.now() - self._capabilities_updated > self._capabilities_ttl
            )

        if self._vision_capabilities and not is_stale():
            return

        async with self._capabilities_lock:
            # Another waiter may have refreshed while we queued on the lock.
            if self._vision_capabilities and not is_stale():
                return
            # Errors are logged by get_available_models; a failed refresh
            # leaves any previously fetched map in place.
            await self.get_available_models()

    async def model_supports_vision(self, model_name: Optional[str] = None) -> bool:
        """Check if the given model (or the default) accepts image input."""
        model_to_check = self._normalize_model_id(model_name)

        await self._ensure_capabilities()

        if model_to_check in self._vision_capabilities:
            return self._vision_capabilities[model_to_check]

        # Unknown model, or the models API is unreachable. Assume vision is
        # supported so the request goes out and any rejection is visible,
        # rather than silently dropping the image the user attached.
        logger.warning(
            f"No capability metadata for '{model_to_check}'; assuming image input is supported"
        )
        return True

    def _get_session(self) -> aiohttp.ClientSession:
        """Returns the shared HTTP session, creating it lazily.

        One session per client keeps the connection pool warm across
        requests and applies a sane default timeout.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    async def close(self):
        """Closes the shared HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def verify_dns_resolution(self, domain: str) -> bool:
        """Verify that we can resolve the DNS for the given domain."""
        try:
            await asyncio.get_event_loop().getaddrinfo(domain, 443)
            return True
        except socket.gaierror:
            return False
            
    async def send_message_with_history(
        self,
        messages: List[Dict[str, str]],
        images: List[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        web_search: bool = False,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None
    ):
        """Legacy interface: returns text, a tool-call dict, or a "⚠️ ..." error string.

        New code should call ``chat()`` and use the ChatResult directly.
        """
        result = await self.chat(
            messages=messages, images=images, system_prompt=system_prompt, model=model,
            web_search=web_search, response_format=response_format, tools=tools,
            tool_choice=tool_choice)
        return result.to_legacy()

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        images: List[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        web_search: bool = False,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        max_attempts: int = 3,
    ) -> ChatResult:
        """Sends a conversation to the model and returns a ChatResult.

        Rate limits, timeouts, network failures and 5xx responses are retried
        with jittered backoff. Errors come back as ChatResult.failure with a
        user-facing message; raw provider output is only logged.
        """
        prompt_to_use = system_prompt if system_prompt is not None else self.system_prompt
        model_to_use = model if model is not None else self.model
        api_model_name = self._normalize_model_id(model_to_use)

        logger.debug(f"Using model: {api_model_name}; system prompt length: {len(prompt_to_use or '')}")

        conversation = [{"role": "system", "content": prompt_to_use}]
        # Label each user turn with its speaker and drop bookkeeping keys
        conversation.extend(prepare_llm_messages(messages))

        # Attach images to the last user message using the OpenAI-style
        # content array — OpenRouter normalizes this for every provider.
        if images and await self.model_supports_vision(model_to_use):
            for i in range(len(conversation) - 1, -1, -1):
                if conversation[i]["role"] == "user":
                    content_array = [{"type": "text", "text": conversation[i]["content"]}]
                    for img in images:
                        base64_image = base64.b64encode(img['data']).decode('utf-8')
                        content_array.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{img['type']};base64,{base64_image}"}
                        })
                    conversation[i]["content"] = content_array
                    break

        payload = {
            "model": api_model_name,
            "messages": conversation,
            # Ask OpenRouter to include token counts and cost in the response
            "usage": {"include": True},
        }
        if web_search:
            payload["plugins"] = [{"id": "web"}]
        if response_format:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/eoko-dev/gideon",
            "X-Title": "Gideon Discord Bot",
            "X-Client": "openrouter-python"
        }

        result = ChatResult.failure(llm_result.PROVIDER, api_model_name, provider="openrouter")
        for attempt in range(max_attempts):
            result, retry_after = await self._post_chat(payload, headers, api_model_name)
            if result.ok or result.error_kind not in llm_result.RETRYABLE_KINDS:
                break
            if attempt + 1 < max_attempts:
                delay = llm_result.backoff_delay(attempt, retry_after)
                logger.warning(f"[OpenRouter] {result.error_kind} on attempt {attempt + 1}/{max_attempts}; "
                               f"retrying in {delay:.1f}s")
                await asyncio.sleep(delay)

        if not result.ok:
            logger.error(f"[OpenRouter] Chat failed ({result.error_kind}) for model {api_model_name}: "
                         f"{(result.detail or '')[:500]}")
        record_usage(result)
        return result

    async def _post_chat(self, payload: Dict[str, Any], headers: Dict[str, str],
                         model: str):
        """One chat completion request. Returns (ChatResult, retry_after_seconds)."""
        try:
            session = self._get_session()
            async with session.post(f"{self.base_url}/chat/completions",
                                    headers=headers, json=payload) as response:
                if response.status != 200:
                    error_text = await response.text()
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    kind = llm_result.kind_for_status(response.status, error_text)
                    return ChatResult.failure(kind, model, detail=f"{response.status}: {error_text}",
                                              provider="openrouter"), retry_after
                data = await response.json(content_type=None)
        except asyncio.TimeoutError as e:
            return ChatResult.failure(llm_result.TIMEOUT, model, detail=repr(e), provider="openrouter"), None
        except aiohttp.ClientError as e:
            return ChatResult.failure(llm_result.NETWORK, model, detail=repr(e), provider="openrouter"), None
        except Exception as e:
            logger.exception(f"[OpenRouter] Unexpected error sending chat request: {e}")
            return ChatResult.failure(llm_result.BAD_RESPONSE, model, detail=repr(e), provider="openrouter"), None

        return self._parse_chat_response(data, model), None

    @staticmethod
    def _parse_chat_response(data: Any, model: str) -> ChatResult:
        """Turns a /chat/completions JSON body into a ChatResult."""
        if not isinstance(data, dict):
            return ChatResult.failure(llm_result.BAD_RESPONSE, model, detail=repr(data)[:500],
                                      provider="openrouter")
        usage = data.get("usage") or {}
        if data.get("error"):
            error = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
            code = error.get("code")
            status = code if isinstance(code, int) else 500
            kind = llm_result.kind_for_status(status, str(error.get("message", "")))
            return ChatResult.failure(kind, model, detail=json.dumps(error)[:500], provider="openrouter")

        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict) or "message" not in choices[0]:
            return ChatResult.failure(llm_result.BAD_RESPONSE, model, detail=json.dumps(data)[:500],
                                      provider="openrouter")
        choice = choices[0]
        message = choice["message"] or {}
        # A choice can carry its own mid-stream provider error
        if choice.get("error"):
            return ChatResult.failure(llm_result.PROVIDER, model, detail=json.dumps(choice["error"])[:500],
                                      provider="openrouter")
        return ChatResult(
            content=message.get("content"),
            tool_calls=message.get("tool_calls") or None,
            usage=_normalize_usage(usage),
            model=data.get("model") or model,
            provider="openrouter",
        )

    async def get_available_models(self) -> Dict[str, Any]:
        """Fetch available models from OpenRouter API."""
        try:
            session = self._get_session()
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/eoko-dev/gideon",
                "X-Title": "Gideon Discord Bot"
            }
            async with session.get(
                f"{self.base_url}/models",
                headers=headers
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Failed to get models: ({response.status}) {error_text}")
                    return {"success": False, "error": f"API Error ({response.status}): {error_text}"}
                    
                models_data = await response.json()
                    
                # Process data to extract useful info and identify vision-capable models
                processed_models = []

                for model in models_data.get("data", []):
                    model_id = model.get("id")
                    context_length = model.get("context_length", 0)
                    pricing = model.get("pricing", {})
                    architecture = model.get("architecture") or {}

                    # Models advertised as vision-capable are only those the API
                    # confirms; unknown metadata is treated as "no" here, while
                    # the per-model runtime check errs the other way.
                    supports_vision = self._detect_vision_support(model) is True

                    processed_models.append({
                        "id": model_id,
                        "name": model.get("name", "Unknown"),
                        "description": model.get("description", ""),
                        "context_length": context_length,
                        "supports_vision": supports_vision, # This is the flag used by ProviderManager
                        "input_modalities": architecture.get("input_modalities", []),
                        "pricing": pricing
                    })

                self._vision_capabilities = {
                    m["id"]: m["supports_vision"] for m in processed_models if m["id"]
                }
                self._capabilities_updated = datetime.now()
                logger.info(
                    f"Loaded capabilities for {len(self._vision_capabilities)} models "
                    f"({sum(self._vision_capabilities.values())} accept image input)"
                )

                return {
                    "success": True,
                    "models": processed_models,
                    "raw_data": models_data
                }
        except Exception as e:
            logger.error(f"Error getting models: {str(e)}")
            return {"success": False, "error": f"Error getting models: {str(e)}"}