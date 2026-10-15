"""Client for interacting with OpenRouter API for image generation."""
import aiohttp
from .http_session import SharedSessionMixin
import base64
import json
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('openrouter_image_client')

MODELS_CACHE_TTL = 600


class OpenRouterImageClient(SharedSessionMixin):
    """Client for generating images using OpenRouter API.

    OpenRouter provides access to various image generation models through a unified API.
    This client uses the /chat/completions endpoint with modalities=["image", "text"]
    to generate images from text prompts.

    Supported models include Google Gemini variants and other models with 'image'
    in their output_modalities.
    """

    def __init__(self, api_key: str):
        """
        Initialize OpenRouter Image client.

        Args:
            api_key: OpenRouter API key
        """
        self._models_cache: List[Dict[str, Any]] = []
        self._models_cache_at: float = 0.0
        self._models_lock = asyncio.Lock()
        if not api_key:
            logger.warning("OpenRouter API key is not configured. Image generation will not work.")
            self.client = None
        else:
            self.api_key = api_key
            self.base_url = "https://openrouter.ai/api/v1"
            self.client = True  # Flag to track if client is initialized
            logger.info("OpenRouter Image client initialized successfully.")

    async def generate_image(self,
                            prompt: str,
                            negative_prompt: str = "",
                            width: int = 1024,
                            height: int = 1024,
                            steps: int = 20,
                            model: Optional[str] = None,
                            **kwargs) -> Dict[str, Any]:
        """
        Generate an image using OpenRouter API.

        Args:
            prompt: Text description of the desired image
            negative_prompt: What to exclude (appended to prompt for models that support it)
            width: Image width (not used by OpenRouter, kept for signature compatibility)
            height: Image height (not used by OpenRouter, kept for signature compatibility)
            steps: Generation steps (not used by OpenRouter, kept for signature compatibility)
            model: OpenRouter model ID (e.g., "google/gemini-2.0-flash-exp")
            **kwargs: Additional parameters:
                - aspect_ratio: OpenRouter aspect ratio ("1:1", "16:9", "9:16", "4:3", "3:4")
                - image_size: OpenRouter image size ("1K", "2K", "4K")

        Returns:
            Dict containing:
                - success: True if generation succeeded
                - image_data: bytes of the PNG image (decoded from base64)
                - image_url: URL if returned instead of base64
                - model_used: The model that generated the image
            Or on failure:
                - success: False
                - error: Error message
        """
        if not self.client:
            return {
                "success": False,
                "error": "OpenRouter client is not initialized (API key missing or invalid)."
            }

        # Model is required for OpenRouter - no default
        if not model:
            return {
                "success": False,
                "error": "No model specified. OpenRouter requires an explicit model selection."
            }

        try:
            # Build the API request headers
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/eoko-dev/gideon",
                "X-Title": "Gideon Discord Bot"
            }

            # Prepare the message content
            # OpenRouter image generation uses chat completion format with modalities parameter
            message_content = prompt

            # Add negative prompt to the message if provided
            # Note: This is model-dependent - some models may not honor it
            if negative_prompt:
                message_content += f"\n\nNegative prompt (avoid these elements): {negative_prompt}"

            messages = [
                {
                    "role": "user",
                    "content": message_content
                }
            ]

            # Build the payload
            # Modalities can be overridden via kwargs (from dashboard config)
            # Default: ["image", "text"] which works with most image-capable models
            if not self._models_cache:
                await self.get_image_models()
            modalities = self._resolve_modalities(model, kwargs.get("modalities") or ["image", "text"])

            payload = {
                "model": model,
                "messages": messages,
                "modalities": modalities,
            }

            # Add OpenRouter-specific parameters if provided
            # These work primarily with Gemini models
            if "aspect_ratio" in kwargs and kwargs["aspect_ratio"]:
                payload["aspect_ratio"] = kwargs["aspect_ratio"]

            if "image_size" in kwargs and kwargs["image_size"]:
                payload["image_size"] = kwargs["image_size"]

            logger.info(f"Requesting OpenRouter image generation with model: {model}")
            logger.debug(f"Payload: {json.dumps(payload, default=str)}")

            # Make the API request
            # 2 minutes timeout for image generation
            timeout = aiohttp.ClientTimeout(total=120)
            async with self.shared_session() as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"OpenRouter API Error ({response.status}): {error_text}")

                        # Parse common errors for better user feedback
                        if response.status == 401:
                            return {"success": False, "error": "Invalid API key"}
                        elif response.status == 400:
                            # Try to extract error message from JSON
                            try:
                                error_json = json.loads(error_text)
                                error_msg = error_json.get("error", {}).get("message", error_text)
                                return {"success": False, "error": f"Bad Request: {error_msg}"}
                            except json.JSONDecodeError:
                                return {"success": False, "error": f"Bad Request: {error_text}"}
                        elif response.status == 429:
                            return {"success": False, "error": "Rate limit exceeded. Please try again later."}
                        elif response.status == 402:
                            return {"success": False, "error": "Insufficient credits. Please add credits to your OpenRouter account."}
                        else:
                            return {"success": False, "error": f"API Error ({response.status}): {error_text[:200]}"}

                    # Parse the response
                    result = await response.json()
                    logger.debug(f"OpenRouter response keys: {result.keys()}")

                    # Extract the image from the response
                    # OpenRouter returns images in message.images array as base64 data URLs
                    if "choices" not in result or len(result["choices"]) == 0:
                        return {"success": False, "error": "No response from OpenRouter API"}

                    choice = result["choices"][0]
                    message = choice.get("message", {})

                    # Check for images in the response
                    images = message.get("images", [])
                    if not images or len(images) == 0:
                        # Model may not support image generation
                        # Check if there's text content to provide context
                        text_content = message.get("content", "")
                        if text_content:
                            logger.warning(f"Model returned text instead of image: {text_content[:100]}")
                        return {
                            "success": False,
                            "error": f"Model '{model}' did not return an image. It may not support image generation."
                        }

                    # Get the first image
                    image_item = images[0]
                    logger.debug(f"Image item type: {type(image_item)}, content: {str(image_item)[:200]}")

                    # Handle different response formats from OpenRouter
                    # Format 1: Direct data URL string
                    # Format 2: Object with nested image_url.url structure: {"image_url": {"url": "data:..."}}
                    # Format 3: Object with direct url: {"url": "data:..."}
                    image_data_url = None

                    if isinstance(image_item, str):
                        image_data_url = image_item
                    elif isinstance(image_item, dict):
                        # Check for nested structure: {"image_url": {"url": "..."}}
                        image_url_field = image_item.get("image_url")
                        if isinstance(image_url_field, dict):
                            image_data_url = image_url_field.get("url", "")
                        elif isinstance(image_url_field, str):
                            image_data_url = image_url_field
                        else:
                            # Fallback to direct url field
                            image_data_url = image_item.get("url", "")
                    else:
                        return {"success": False, "error": f"Unexpected image format: {type(image_item)}"}

                    if not image_data_url:
                        return {"success": False, "error": f"Empty image data in response. Item: {str(image_item)[:200]}"}

                    # Check if it's a URL or base64 data URL
                    if not image_data_url.startswith("data:"):
                        # It's a direct URL - return it as image_url
                        logger.info(f"OpenRouter returned image URL directly")
                        return {
                            "success": True,
                            "image_url": image_data_url,
                            "model_used": model
                        }

                    # Parse data URL: data:image/png;base64,<base64data>
                    try:
                        # Split on comma to separate metadata from data
                        if "," not in image_data_url:
                            return {"success": False, "error": "Invalid image data URL format"}

                        header, base64_data = image_data_url.split(",", 1)

                        # Decode base64 to bytes
                        image_bytes = base64.b64decode(base64_data)

                        logger.info(f"Successfully decoded image: {len(image_bytes)} bytes")

                        return {
                            "success": True,
                            "image_data": image_bytes,
                            "model_used": model
                            # OpenRouter doesn't return seed in responses
                        }

                    except Exception as decode_error:
                        logger.error(f"Failed to decode base64 image: {decode_error}")
                        return {
                            "success": False,
                            "error": f"Failed to decode image data: {str(decode_error)}"
                        }

        except aiohttp.ClientError as e:
            logger.error(f"Network error during OpenRouter image generation: {e}")
            return {"success": False, "error": f"Network error: {str(e)}"}
        except Exception as e:
            logger.exception(f"Unexpected error during OpenRouter image generation: {e}")
            return {"success": False, "error": f"Unexpected error: {str(e)}"}

    @staticmethod
    def _format_image_model(model_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one OpenRouter /models entry, or None if it cannot output images."""
        if not isinstance(model_info, dict) or not model_info.get("id"):
            return None
        architecture = model_info.get("architecture") or {}
        output_modalities = architecture.get("output_modalities") or []
        modality_str = architecture.get("modality") or ""
        outputs = [str(m).lower() for m in output_modalities]
        if not outputs and "->" in modality_str:
            outputs = modality_str.split("->", 1)[1].lower().split("+")
        if "image" not in outputs:
            return None
        return {
            "id": model_info["id"],
            "name": model_info.get("name") or model_info["id"],
            "output_modalities": output_modalities,
            "modality": modality_str,
        }

    def get_cached_image_models(self) -> List[Dict[str, Any]]:
        """Last successfully fetched model list, without network I/O."""
        return list(self._models_cache)

    def models_cache_is_stale(self) -> bool:
        return not self._models_cache or time.monotonic() - self._models_cache_at >= MODELS_CACHE_TTL

    def _resolve_modalities(self, model: str, requested: List[str]) -> List[str]:
        """Drop requested modalities the model cannot output (e.g. "text" for image-only models)."""
        entry = next((m for m in self._models_cache if m["id"] == model), None)
        if not entry or not entry.get("output_modalities"):
            return requested
        supported = set(entry["output_modalities"])
        resolved = [m for m in requested if m in supported]
        return resolved if "image" in resolved else ["image"]

    async def get_image_models(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Get the live list of OpenRouter models that can generate images.

        Uses OpenRouter's server-side ``output_modalities=image`` filter; the
        unfiltered /models endpoint only returns text-output models and so
        omits image-only models (FLUX, Seedream, Recraft, etc.).

        Returns:
            Dict with "success": True and "models" list, or "success": False with "error"
        """
        if not self.client:
            return {"success": False, "error": "Client not initialized"}

        if not force_refresh and not self.models_cache_is_stale():
            return {"success": True, "models": self.get_cached_image_models(), "source": "cache"}

        async with self._models_lock:
            if not force_refresh and not self.models_cache_is_stale():
                return {"success": True, "models": self.get_cached_image_models(), "source": "cache"}
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "HTTP-Referer": "https://github.com/eoko-dev/gideon",
                    "X-Title": "Gideon Discord Bot"
                }
                timeout = aiohttp.ClientTimeout(total=15)
                async with self.shared_session() as session:
                    async with session.get(
                        f"{self.base_url}/models",
                        params={"output_modalities": "image"},
                        headers=headers,
                        timeout=timeout
                    ) as response:
                        if response.status != 200:
                            return {"success": False, "error": f"Failed to fetch model list (HTTP {response.status})"}
                        models_data = await response.json()

                raw = models_data.get("data", []) if isinstance(models_data, dict) else []
                image_models = [m for m in (self._format_image_model(r) for r in raw) if m]
                if not image_models:
                    return {"success": False, "error": "OpenRouter returned no image generation models"}

                self._models_cache = image_models
                self._models_cache_at = time.monotonic()
                logger.info(f"Fetched {len(image_models)} image generation models from OpenRouter")
                return {"success": True, "models": self.get_cached_image_models(), "source": "api"}
            except Exception as e:
                logger.error(f"Error fetching image models: {e}")
                return {"success": False, "error": str(e)}
