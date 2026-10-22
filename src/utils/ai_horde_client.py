import aiohttp
from .http_session import SharedSessionMixin
import asyncio
import logging

from .llm_formatting import flatten_system_prompt
from typing import Dict, Any, Optional, List

logger = logging.getLogger('ai_horde_client')

class AIHordeClient(SharedSessionMixin):
    """Client for interacting with AI Horde API for text and image generation."""

    def __init__(self, api_key: str = "", default_text_max_wait: int = 300):
        self.api_key = api_key
        self.base_url = "https://stablehorde.net/api/v2" # Use stablehorde.net as primary endpoint
        self.default_text_max_wait = default_text_max_wait # Max wait time for text generation

    async def generate_image(self,
                           prompt: str, 
                           negative_prompt: str = "",
                           width: int = 512, 
                           height: int = 512,
                           steps: int = 30,
                           model: str = "stable_diffusion_2.1",
                           nsfw: bool = False,
                           max_wait_time: int = 300) -> Dict[str, Any]:
        """
        Generate an image using AI Horde.
        
        Args:
            prompt: Text description of the desired image
            negative_prompt: What the image should not contain
            width: Image width (multiple of 64, max 1024)
            height: Image height (multiple of 64, max 1024)
            steps: Generation steps (higher = more detail but slower)
            model: AI model to use
            nsfw: Whether to allow NSFW content
            max_wait_time: Maximum time to wait for generation in seconds
            
        Returns:
            Dict containing image data or error information
        """
        try:
            # Setup headers - API key is optional but gives better priority
            headers = {
                "Content-Type": "application/json",
            }
            
            if self.api_key:
                headers["apikey"] = self.api_key
            
            # Prepare the generation parameters
            payload = {
                "prompt": prompt,
                "params": {
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "steps": steps,
                    "sampler_name": "k_euler_a",
                    "cfg_scale": 7.5,
                },
                "nsfw": nsfw,
                "models": [model],
                "r2": True,  # Use R2 storage for images
            }
            
            async with self.shared_session() as session:
                # Step 1: Submit the generation request
                async with session.post(
                    f"{self.base_url}/generate/async",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status != 202:
                        error_text = await response.text()
                        logger.error(f"Failed to submit generation: ({response.status}) {error_text}")
                        return {"error": f"API Error ({response.status}): {error_text}"}
                    
                    submission = await response.json()
                    request_id = submission.get("id")
                    
                    if not request_id:
                        return {"error": "Failed to get request ID from AI Horde"}
                    
                    logger.info(f"Image generation submitted with ID: {request_id}")
                
                # Step 2: Poll for results
                start_time = asyncio.get_event_loop().time()
                while asyncio.get_event_loop().time() - start_time < max_wait_time:
                    async with session.get(
                        f"{self.base_url}/generate/check/{request_id}",
                        headers=headers
                    ) as check_response:
                        status = await check_response.json()
                        
                        # Check if generation failed
                        if "faulted" in status and status["faulted"]:
                            return {"error": "Generation failed on AI Horde"}
                        
                        # Check if generation is done
                        if "done" in status and status["done"]:
                            break
                        
                        # If not done, wait and continue polling
                        wait_time = min(5, max(1, status.get("wait_time", 2)))
                        logger.debug(f"Waiting for image, estimated time: {status.get('wait_time', '?')}s")
                        await asyncio.sleep(wait_time)
                
                # Check if we timed out
                if asyncio.get_event_loop().time() - start_time >= max_wait_time:
                    return {"error": f"Generation timed out after {max_wait_time} seconds"}
                
                # Step 3: Retrieve the results
                async with session.get(
                    f"{self.base_url}/generate/status/{request_id}",
                    headers=headers
                ) as status_response:
                    result = await status_response.json()
                    
                    # Process and return the image data
                    if "generations" in result and result["generations"]:
                        generation = result["generations"][0]
                        return {
                            "success": True,
                            "image_url": generation.get("img"),
                            "model": generation.get("model"),
                            "seed": generation.get("seed"),
                        }
                    else:
                        return {"error": "No image was generated"}
                        
        except Exception as e:
            logger.error(f"Error generating image: {str(e)}")
            return {"error": f"Error generating image: {str(e)}"}

    async def get_available_models(self) -> Dict[str, Any]:
        """Get a list of available models on AI Horde."""
        try:
            async with self.shared_session() as session:
                headers = {}
                if self.api_key:
                    headers["apikey"] = self.api_key
                
                async with session.get(
                    f"{self.base_url}/status/models",
                    headers=headers
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Failed to get models: ({response.status}) {error_text}")
                        return {"error": f"API Error ({response.status}): {error_text}"}
                    
                    raw_models_list = await response.json()
                    # Transform the list: AI Horde uses 'name', model_manager expects 'id'
                    formatted_models = []
                    if isinstance(raw_models_list, list):
                        for model_data in raw_models_list:
                            # Transform the list: AI Horde uses 'name', model_manager expects 'id'
                            if isinstance(model_data, dict) and "name" in model_data:
                                formatted_models.append({"id": model_data["name"]}) # Create dict with 'id' key
                            else:
                                logger.warning(f"Skipping unexpected model data format: {model_data}")
                    else:
                         logger.error(f"AI Horde /status/models did not return a list: {type(raw_models_list)}")

                    return {"success": True, "models": formatted_models}
        except Exception as e:
            logger.error(f"Error getting models: {str(e)}")
            return {"success": False, "error": f"Error getting models: {str(e)}"}

    def model_supports_vision(self, model_name: str) -> bool:
        """Checks if the specified AI Horde model name supports vision."""
        # AI Horde text models generally don't support direct image input via this API flow
        return False

    async def send_message_with_history(self, messages: List[Dict[str, str]], model: str, system_prompt: Optional[str] = None, **kwargs) -> str:
        """
        Sends a message to the AI Horde Text Generation API using conversation history.

        Args:
            messages: A list of message dictionaries, e.g., [{"role": "user", "content": "Hello"}].
            model: The specific AI Horde model name to use.
            system_prompt: An optional system prompt to guide the AI.
            **kwargs: Potential additional arguments (ignored for AI Horde text generation).

        Returns:
            The text content of the AI's response, or a formatted error string starting with "⚠️ Error: ".
        """
        # Format the prompt for AI Horde (simple concatenation)
        # Note: Different models might prefer different formatting. This is a basic approach.
        horde_prompt = ""
        if system_prompt:
            # AI Horde uses ### Instruction / ### Input / ### Response format for some models
            # We'll prepend the system prompt simply for now.
            horde_prompt += f"System: {flatten_system_prompt(system_prompt)}\n\n"

        for msg in messages:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            horde_prompt += f"{role}: {content}\n"
        # Add final prompt for the assistant to respond
        horde_prompt += "Assistant:"

        logger.debug(f"Formatted AI Horde Prompt:\n{horde_prompt}")

        # Prepare payload for text generation
        payload = {
            "prompt": horde_prompt,
            "params": {
                # Common parameters, adjust as needed
                "max_context_length": kwargs.get("max_context_length", 2048), # Example: make configurable?
                "max_length": kwargs.get("max_length", 180), # Example: make configurable?
                "temperature": kwargs.get("temperature", 0.7),
                # Add other relevant params like top_p, top_k, etc. if desired
            },
            "models": [model],
            # "workers": [], # Optional: Specify worker IDs
            # "trusted_workers": False, # Optional
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        if self.api_key:
            headers["apikey"] = self.api_key
        else:
            headers["Client-Agent"] = "GideonBot:1.0:github.com/w-a-v-e-s/gideon" # Identify non-API key requests


        logger.info(f"Requesting AI Horde text generation with model '{model}'")

        try:
            async with self.shared_session() as session:
                # Step 1: Submit the generation request
                async with session.post(
                    f"{self.base_url}/generate/text/async",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status != 202:
                        error_text = await response.text()
                        logger.error(f"Failed to submit AI Horde text generation: ({response.status}) {error_text}")
                        return f"⚠️ AI Horde Error: Failed to submit request ({response.status}) - {error_text}"

                    submission = await response.json()
                    request_id = submission.get("id")

                    if not request_id:
                        logger.error(f"AI Horde submission response missing 'id': {submission}")
                        return "⚠️ AI Horde Error: Failed to get request ID from submission."

                    logger.info(f"AI Horde text generation submitted with ID: {request_id}")

                # Step 2: Poll for results
                start_time = asyncio.get_event_loop().time()
                max_wait_time = self.default_text_max_wait
                while asyncio.get_event_loop().time() - start_time < max_wait_time:
                    await asyncio.sleep(3) # Wait 3 seconds between checks
                    async with session.get(
                        f"{self.base_url}/generate/text/status/{request_id}",
                        headers=headers # API key might not be needed for status check, but include for consistency
                    ) as check_response:

                        if check_response.status != 200:
                             # Handle potential errors during status check itself
                             error_text = await check_response.text()
                             logger.warning(f"AI Horde status check failed ({check_response.status}): {error_text}")
                             # Continue polling unless it's a fatal error like 404 Not Found?
                             if check_response.status == 404:
                                 return f"⚠️ AI Horde Error: Generation ID {request_id} not found."
                             continue # Try polling again

                        status = await check_response.json()

                        if status.get("faulted"):
                            logger.error(f"AI Horde text generation faulted for ID {request_id}. Status: {status}")
                            return f"⚠️ AI Horde Error: Generation failed on the Horde."

                        if status.get("done"):
                            logger.info(f"AI Horde text generation complete for ID {request_id}.")
                            # Retrieve the final result
                            generation_data = status.get("generations", [])
                            if generation_data and isinstance(generation_data, list) and len(generation_data) > 0:
                                generated_text = generation_data[0].get("text", "").strip()
                                worker_name = generation_data[0].get("worker_name", "Unknown")
                                logger.info(f"Received text from worker {worker_name}")
                                return generated_text
                            else:
                                logger.error(f"AI Horde generation done but no text found for ID {request_id}. Status: {status}")
                                return "⚠️ AI Horde Error: Generation finished but no text was returned."

                        # Still waiting
                        queue_pos = status.get('queue_position', '?')
                        wait_est = status.get('wait', '?')
                        logger.debug(f"AI Horde text generation status for {request_id}: Waiting (Queue: {queue_pos}, Est Wait: {wait_est}s)")


                # If loop finishes, we timed out
                logger.error(f"AI Horde text generation timed out for ID {request_id} after {max_wait_time} seconds.")
                return f"⚠️ AI Horde Error: Generation timed out after {max_wait_time} seconds."

        except aiohttp.ClientError as e:
             logger.error(f"AI Horde network error during text generation: {e}", exc_info=True)
             return f"⚠️ AI Horde Error: Network error - {e}"
        except asyncio.TimeoutError:
             logger.error(f"AI Horde request timed out during text generation.")
             return f"⚠️ AI Horde Error: Request timed out."
        except Exception as e:
            logger.exception(f"Unexpected error during AI Horde text generation: {e}")
            return f"⚠️ Error: An unexpected error occurred - {str(e)}"