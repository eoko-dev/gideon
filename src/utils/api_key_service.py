import logging
import os
import uuid
import aiohttp
from datetime import datetime
from typing import Optional, List, Dict, Any

from .encryption import EncryptionManager

logger = logging.getLogger(__name__)


class APIKeyService:
    """Service layer for API key management — coordinates encryption, storage, and validation."""

    # Maps provider names to their .env variable names for fallback
    PROVIDER_ENV_MAP = {
        'openrouter': 'OPENROUTER_API_KEY',
        'openai': 'OPENAI_API_KEY',
        'ai_horde': 'AI_HORDE_API_KEY',
        'cloudflare': 'CLOUDFLARE_API_KEY',
        'cloudflare_url': 'CLOUDFLARE_WORKER_URL',
        'comfyui': 'COMFYUI_URL',
        'discord': 'DISCORD_TOKEN',
    }

    # Human-readable provider labels
    PROVIDER_LABELS = {
        'openrouter': 'OpenRouter',
        'openai': 'OpenAI',
        'ai_horde': 'AI Horde',
        'cloudflare': 'Cloudflare API Key',
        'cloudflare_url': 'Cloudflare Worker URL',
        'comfyui': 'ComfyUI URL',
        'discord': 'Discord Bot Token',
    }

    # Provider usage dashboard links
    PROVIDER_USAGE_LINKS = {
        'openrouter': 'https://openrouter.ai/usage',
        'openai': 'https://platform.openai.com/usage',
        'ai_horde': 'https://stablehorde.net',
    }

    def __init__(self, db_manager, encryption_manager: EncryptionManager):
        self.db = db_manager
        self.encryption = encryption_manager

    def get_key_for_provider(self, provider: str) -> str:
        """
        Get decrypted API key for a provider.
        Priority: 1) Active DB key  2) .env fallback
        """
        # Try DB first
        key_record = self.db.get_active_api_key_for_provider(provider)
        if key_record:
            try:
                decrypted = self.encryption.decrypt(key_record['encrypted_key'])
                self.db.update_api_key_last_used(key_record['key_id'])
                return decrypted
            except ValueError:
                logger.error(f"Failed to decrypt key for provider '{provider}' — "
                             "master key may have changed")

        # Fallback to .env
        env_var = self.PROVIDER_ENV_MAP.get(provider)
        if env_var:
            return os.getenv(env_var, '')
        return ''

    def add_key(self, provider: str, plaintext_key: str,
                alias: Optional[str] = None, user_id: str = 'dashboard') -> Dict[str, Any]:
        """Add a new API key (encrypts before storage)."""
        key_id = str(uuid.uuid4())
        encrypted = self.encryption.encrypt(plaintext_key)
        self.db.add_api_key(key_id, provider, encrypted, alias)
        self.db.add_api_key_audit_entry(key_id, 'created', user_id,
                                        f"Key added for provider '{provider}'")
        logger.info(f"API key added for provider '{provider}' by {user_id}")
        return {'key_id': key_id, 'provider': provider, 'alias': alias}

    def update_key(self, key_id: str, plaintext_key: Optional[str] = None,
                   alias: Optional[str] = None, user_id: str = 'dashboard') -> bool:
        """Update an existing API key."""
        encrypted = None
        if plaintext_key:
            encrypted = self.encryption.encrypt(plaintext_key)
        updated = self.db.update_api_key(key_id, encrypted, alias)
        if updated:
            details = []
            if plaintext_key:
                details.append('key rotated')
            if alias is not None:
                details.append(f'alias set to "{alias}"')
            self.db.add_api_key_audit_entry(key_id, 'updated', user_id,
                                            ', '.join(details))
        return updated

    def delete_key(self, key_id: str, user_id: str = 'dashboard') -> bool:
        """Delete an API key."""
        # Log audit before deletion (since ON DELETE CASCADE would remove audit too)
        key_record = self.db.get_api_key(key_id)
        provider = key_record['provider'] if key_record else 'unknown'
        self.db.add_api_key_audit_entry(key_id, 'deleted', user_id,
                                        f"Key deleted for provider '{provider}'")
        deleted = self.db.delete_api_key(key_id)
        if deleted:
            logger.info(f"API key '{key_id}' deleted for provider '{provider}' by {user_id}")
        return deleted

    def list_keys(self, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all API keys with masked values (never exposes raw keys)."""
        if provider:
            keys = self.db.get_api_keys_by_provider(provider)
        else:
            keys = self.db.get_all_api_keys()

        result = []
        for key in keys:
            # Decrypt to generate mask, then discard plaintext
            try:
                decrypted = self.encryption.decrypt(key['encrypted_key'])
                masked = EncryptionManager.mask_key(decrypted)
            except ValueError:
                masked = '*** (decrypt error)'

            result.append({
                'key_id': key['key_id'],
                'provider': key['provider'],
                'masked_key': masked,
                'key_alias': key.get('key_alias'),
                'created_at': key.get('created_at'),
                'last_used': key.get('last_used'),
                'last_validated': key.get('last_validated'),
                'is_active': bool(key.get('is_active', 0)),
                'validation_status': key.get('validation_status', 'untested'),
            })
        return result

    def get_status_summary(self) -> Dict[str, int]:
        """Count stored keys by validation status without decrypting anything."""
        keys = self.db.get_all_api_keys()
        statuses = [k.get('validation_status') or 'untested' for k in keys]
        return {
            'total': len(keys),
            'invalid': statuses.count('invalid'),
            'untested': statuses.count('untested'),
        }

    def get_audit_log(self, key_id: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
        """Get the audit log for API key operations."""
        return self.db.get_api_key_audit_log(key_id, limit)

    async def validate_key(self, provider: str, plaintext_key: str) -> Dict[str, Any]:
        """Test an API key by making a lightweight provider API call."""
        try:
            if provider == 'openrouter':
                return await self._validate_openrouter(plaintext_key)
            elif provider == 'openai':
                return await self._validate_openai(plaintext_key)
            elif provider == 'ai_horde':
                return await self._validate_ai_horde(plaintext_key)
            elif provider == 'cloudflare':
                return await self._validate_cloudflare(plaintext_key)
            elif provider == 'cloudflare_url':
                return await self._validate_url_reachable(plaintext_key)
            elif provider == 'comfyui':
                return await self._validate_comfyui(plaintext_key)
            else:
                return {'valid': False, 'message': f'Unknown provider: {provider}'}
        except Exception as e:
            logger.error(f"Validation error for provider '{provider}': {e}")
            return {'valid': False, 'message': str(e)}

    async def validate_and_update_status(self, key_id: str) -> Dict[str, Any]:
        """Validate a stored key and update its status in the database."""
        key_record = self.db.get_api_key(key_id)
        if not key_record:
            return {'valid': False, 'message': 'Key not found'}

        try:
            decrypted = self.encryption.decrypt(key_record['encrypted_key'])
        except ValueError:
            self.db.update_api_key_validation_status(key_id, 'invalid')
            return {'valid': False, 'message': 'Decryption failed'}

        result = await self.validate_key(key_record['provider'], decrypted)
        status = 'valid' if result['valid'] else 'invalid'
        self.db.update_api_key_validation_status(key_id, status)
        self.db.add_api_key_audit_entry(key_id, 'validated', 'dashboard',
                                        f"Validation result: {status}")
        return result

    async def import_from_env(self, user_id: str = 'migration') -> Dict[str, Any]:
        """One-time migration: import keys from .env into encrypted DB storage."""
        imported = []
        skipped = []
        errors = []

        for provider, env_var in self.PROVIDER_ENV_MAP.items():
            if provider == 'discord':
                # Skip Discord token — should always stay in .env
                skipped.append(provider)
                continue

            value = os.getenv(env_var, '')
            if not value or value.startswith('your_'):
                skipped.append(provider)
                continue

            # Check if provider already has a key in DB
            existing = self.db.get_active_api_key_for_provider(provider)
            if existing:
                skipped.append(provider)
                continue

            try:
                label = self.PROVIDER_LABELS.get(provider, provider)
                self.add_key(provider, value, alias=f'{label} (imported from .env)',
                             user_id=user_id)
                imported.append(provider)
            except Exception as e:
                logger.error(f"Error importing key for '{provider}': {e}")
                errors.append({'provider': provider, 'error': str(e)})

        return {'imported': imported, 'skipped': skipped, 'errors': errors}

    # --- Private validation methods ---

    async def _validate_openrouter(self, api_key: str) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://openrouter.ai/api/v1/auth/key',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {'valid': True, 'message': 'Key is valid',
                            'details': data.get('data', {})}
                else:
                    return {'valid': False,
                            'message': f'API returned status {resp.status}'}

    async def _validate_openai(self, api_key: str) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://api.openai.com/v1/models',
                headers={'Authorization': f'Bearer {api_key}'},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return {'valid': True, 'message': 'Key is valid'}
                elif resp.status == 401:
                    return {'valid': False, 'message': 'Invalid API key'}
                else:
                    return {'valid': False,
                            'message': f'API returned status {resp.status}'}

    async def _validate_ai_horde(self, api_key: str) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://stablehorde.net/api/v2/find_user',
                headers={'apikey': api_key},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {'valid': True, 'message': 'Key is valid',
                            'details': {'username': data.get('username')}}
                else:
                    return {'valid': False,
                            'message': f'API returned status {resp.status}'}

    async def _validate_cloudflare(self, api_key: str) -> Dict[str, Any]:
        # For Cloudflare, we just check the key format is non-empty
        # Actual validation requires the worker URL
        if api_key and len(api_key) > 10:
            return {'valid': True,
                    'message': 'Key format looks valid (full validation requires worker URL)'}
        return {'valid': False, 'message': 'Key appears too short or empty'}

    async def _validate_url_reachable(self, url: str) -> Dict[str, Any]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    return {'valid': True,
                            'message': f'URL is reachable (status {resp.status})'}
        except aiohttp.ClientError as e:
            return {'valid': False, 'message': f'URL unreachable: {e}'}

    async def _validate_comfyui(self, url: str) -> Dict[str, Any]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f'{url.rstrip("/")}/system_stats',
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return {'valid': True, 'message': 'ComfyUI server is reachable'}
                    return {'valid': False,
                            'message': f'Server returned status {resp.status}'}
        except aiohttp.ClientError as e:
            return {'valid': False, 'message': f'Server unreachable: {e}'}
