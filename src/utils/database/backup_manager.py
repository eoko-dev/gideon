import sqlite3
import threading
import json
import os
import shutil
import logging
from datetime import datetime
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class BackupManager:
    """Handles backup export and import operations for the database."""

    SCHEMA_VERSION = 1
    EXPORT_VERSION = "1.0"

    # Tables included in JSON config exports
    CONFIG_TABLES = [
        'GLOBAL_CONFIG', 'CHANNELS', 'CHANNEL_CONFIG',
        'PERSONA_TEMPLATES', 'CHANNEL_PERSONAS', 'USERS'
    ]

    # Fields to exclude from CHANNEL_PERSONAS export (Discord-specific ephemeral credentials)
    CHANNEL_PERSONA_EXCLUDE_FIELDS = {'webhook_id', 'webhook_token'}

    # Expected tables for SQLite backup validation
    EXPECTED_TABLES = [
        'GLOBAL_CONFIG', 'CHANNELS', 'CHANNEL_CONFIG', 'THREADS', 'MESSAGES',
        'USERS', 'REMINDERS', 'TRIVIA_GAME_SESSIONS', 'TRIVIA_LEADERBOARD',
        'TRIVIA_ACHIEVEMENTS', 'API_KEYS', 'API_KEY_AUDIT',
        'PERSONA_TEMPLATES', 'CHANNEL_PERSONAS'
    ]

    def __init__(self, conn, db_path: str):
        self._conn = conn
        self._db_path = db_path

    def _table_to_dicts(self, table_name: str, exclude_fields: set = None) -> List[Dict[str, Any]]:
        """Reads all rows from a table, returning a list of dicts."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(f"SELECT * FROM {table_name}")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            result = []
            for row in rows:
                row_dict = {}
                for i, col in enumerate(columns):
                    if exclude_fields and col in exclude_fields:
                        continue
                    row_dict[col] = row[i]
                result.append(row_dict)
            return result
        except sqlite3.Error as e:
            logger.error(f"Error reading table '{table_name}': {e}", exc_info=True)
            return []

    # ── Export ─────────────────────────────────────────────────

    def export_config_json(self) -> Dict[str, Any]:
        """Export all configuration tables as a JSON-serializable dict.

        Includes: GLOBAL_CONFIG, CHANNELS, CHANNEL_CONFIG,
                  PERSONA_TEMPLATES, CHANNEL_PERSONAS, USERS.
        Excludes: messages, threads, reminders, trivia, API keys, webhooks.
        """
        export_data = {
            "export_type": "gideon_config",
            "version": self.EXPORT_VERSION,
            "schema_version": self.SCHEMA_VERSION,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "api_keys_excluded": True,
            "api_keys_note": (
                "API keys are excluded from config exports for security. "
                "Use full database backup to include encrypted keys."
            ),
            "tables": {}
        }

        for table_name in self.CONFIG_TABLES:
            exclude = None
            if table_name == 'CHANNEL_PERSONAS':
                exclude = self.CHANNEL_PERSONA_EXCLUDE_FIELDS
            export_data["tables"][table_name] = self._table_to_dicts(table_name, exclude)

        logger.info(
            "Exported config JSON: %s",
            ", ".join(f"{t}: {len(export_data['tables'][t])}" for t in self.CONFIG_TABLES)
        )
        return export_data

    # ── Validate Config Import ────────────────────────────────

    def validate_config_json(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate imported JSON config structure.

        Returns:
            dict with keys: valid (bool), errors (list), preview (dict of counts)
        """
        errors = []
        preview = {}

        # Check top-level structure
        if not isinstance(data, dict):
            return {"valid": False, "errors": ["Data must be a JSON object"], "preview": {}}

        if data.get("export_type") != "gideon_config":
            errors.append("Missing or invalid 'export_type' (expected 'gideon_config')")

        if "version" not in data:
            errors.append("Missing 'version' field")

        schema_version = data.get("schema_version")
        if schema_version is None:
            errors.append("Missing 'schema_version' field")
        elif not isinstance(schema_version, int):
            errors.append("'schema_version' must be an integer")
        elif schema_version > self.SCHEMA_VERSION:
            errors.append(
                f"Export schema version ({schema_version}) is newer than "
                f"this bot's version ({self.SCHEMA_VERSION}). Please upgrade the bot first."
            )

        if "timestamp" not in data:
            errors.append("Missing 'timestamp' field")

        # Check tables section
        tables = data.get("tables")
        if not isinstance(tables, dict):
            errors.append("Missing or invalid 'tables' section")
            return {"valid": False, "errors": errors, "preview": {}}

        # Validate each table section
        valid_types = {'string', 'int', 'float', 'bool', 'json'}

        for table_name in self.CONFIG_TABLES:
            table_data = tables.get(table_name)
            if table_data is None:
                continue  # Table section is optional
            if not isinstance(table_data, list):
                errors.append(f"'{table_name}' must be an array")
                continue

            preview[table_name] = len(table_data)

            # Validate GLOBAL_CONFIG entries have required fields
            if table_name == 'GLOBAL_CONFIG':
                for i, entry in enumerate(table_data):
                    if not isinstance(entry, dict):
                        errors.append(f"GLOBAL_CONFIG[{i}] must be an object")
                        continue
                    if 'key' not in entry:
                        errors.append(f"GLOBAL_CONFIG[{i}] missing 'key'")
                    if 'type' in entry and entry['type'] not in valid_types:
                        errors.append(
                            f"GLOBAL_CONFIG[{i}] has invalid type '{entry.get('type')}'"
                        )

            # Validate CHANNELS entries
            elif table_name == 'CHANNELS':
                for i, entry in enumerate(table_data):
                    if not isinstance(entry, dict):
                        errors.append(f"CHANNELS[{i}] must be an object")
                        continue
                    if 'channel_id' not in entry:
                        errors.append(f"CHANNELS[{i}] missing 'channel_id'")

            # Validate CHANNEL_CONFIG entries
            elif table_name == 'CHANNEL_CONFIG':
                for i, entry in enumerate(table_data):
                    if not isinstance(entry, dict):
                        errors.append(f"CHANNEL_CONFIG[{i}] must be an object")
                        continue
                    if 'channel_id' not in entry:
                        errors.append(f"CHANNEL_CONFIG[{i}] missing 'channel_id'")

            # Validate PERSONA_TEMPLATES entries
            elif table_name == 'PERSONA_TEMPLATES':
                for i, entry in enumerate(table_data):
                    if not isinstance(entry, dict):
                        errors.append(f"PERSONA_TEMPLATES[{i}] must be an object")
                        continue
                    if 'template_id' not in entry:
                        errors.append(f"PERSONA_TEMPLATES[{i}] missing 'template_id'")
                    if 'name' not in entry:
                        errors.append(f"PERSONA_TEMPLATES[{i}] missing 'name'")
                    if 'display_name' not in entry:
                        errors.append(f"PERSONA_TEMPLATES[{i}] missing 'display_name'")

            # Validate CHANNEL_PERSONAS entries
            elif table_name == 'CHANNEL_PERSONAS':
                for i, entry in enumerate(table_data):
                    if not isinstance(entry, dict):
                        errors.append(f"CHANNEL_PERSONAS[{i}] must be an object")
                        continue
                    if 'channel_id' not in entry:
                        errors.append(f"CHANNEL_PERSONAS[{i}] missing 'channel_id'")
                    if 'display_name' not in entry:
                        errors.append(f"CHANNEL_PERSONAS[{i}] missing 'display_name'")

            # Validate USERS entries
            elif table_name == 'USERS':
                for i, entry in enumerate(table_data):
                    if not isinstance(entry, dict):
                        errors.append(f"USERS[{i}] must be an object")
                        continue
                    if 'user_id' not in entry:
                        errors.append(f"USERS[{i}] missing 'user_id'")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "preview": preview
        }

    # ── Import Config ─────────────────────────────────────────

    def import_config_json(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply imported JSON config to the database.

        Uses INSERT OR REPLACE / upsert patterns wrapped in a transaction.

        Returns:
            dict with keys: applied (dict of table: count), errors (list)
        """
        tables = data.get("tables", {})
        applied = {}
        errors = []

        try:
            with self._conn:
                cursor = self._conn.cursor()

                # GLOBAL_CONFIG - upsert each key
                gc_data = tables.get("GLOBAL_CONFIG", [])
                count = 0
                for entry in gc_data:
                    try:
                        cursor.execute(
                            """INSERT INTO GLOBAL_CONFIG (key, value, type)
                               VALUES (?, ?, ?)
                               ON CONFLICT(key) DO UPDATE SET
                                   value = excluded.value, type = excluded.type""",
                            (entry['key'], entry.get('value'), entry.get('type', 'string'))
                        )
                        count += 1
                    except (sqlite3.Error, KeyError) as e:
                        errors.append(f"GLOBAL_CONFIG entry error: {e}")
                applied['GLOBAL_CONFIG'] = count

                # CHANNELS - insert or ignore
                ch_data = tables.get("CHANNELS", [])
                count = 0
                for entry in ch_data:
                    try:
                        cursor.execute(
                            "INSERT OR IGNORE INTO CHANNELS (channel_id, name) VALUES (?, ?)",
                            (entry['channel_id'], entry.get('name'))
                        )
                        count += 1
                    except (sqlite3.Error, KeyError) as e:
                        errors.append(f"CHANNELS entry error: {e}")
                applied['CHANNELS'] = count

                # CHANNEL_CONFIG - upsert
                cc_data = tables.get("CHANNEL_CONFIG", [])
                count = 0
                for entry in cc_data:
                    try:
                        # Ensure channel record exists first
                        cursor.execute(
                            "INSERT OR IGNORE INTO CHANNELS (channel_id, name) VALUES (?, ?)",
                            (entry['channel_id'], None)
                        )
                        cursor.execute(
                            """INSERT INTO CHANNEL_CONFIG (channel_id, model, provider, system_prompt)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(channel_id) DO UPDATE SET
                                   model = excluded.model,
                                   provider = excluded.provider,
                                   system_prompt = excluded.system_prompt""",
                            (entry['channel_id'], entry.get('model'),
                             entry.get('provider'), entry.get('system_prompt'))
                        )
                        count += 1
                    except (sqlite3.Error, KeyError) as e:
                        errors.append(f"CHANNEL_CONFIG entry error: {e}")
                applied['CHANNEL_CONFIG'] = count

                # PERSONA_TEMPLATES - insert or replace (skip builtins)
                pt_data = tables.get("PERSONA_TEMPLATES", [])
                count = 0
                for entry in pt_data:
                    try:
                        now = datetime.utcnow().isoformat()
                        cursor.execute(
                            """INSERT OR REPLACE INTO PERSONA_TEMPLATES
                                (template_id, name, display_name, avatar_url, system_prompt,
                                 model, provider, response_style, description, is_builtin,
                                 created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (entry['template_id'], entry['name'], entry['display_name'],
                             entry.get('avatar_url'), entry.get('system_prompt'),
                             entry.get('model'), entry.get('provider'),
                             entry.get('response_style'), entry.get('description'),
                             1 if entry.get('is_builtin') else 0,
                             entry.get('created_at', now), now)
                        )
                        count += 1
                    except (sqlite3.Error, KeyError) as e:
                        errors.append(f"PERSONA_TEMPLATES entry error: {e}")
                applied['PERSONA_TEMPLATES'] = count

                # CHANNEL_PERSONAS - upsert, preserving webhook fields
                cp_data = tables.get("CHANNEL_PERSONAS", [])
                count = 0
                for entry in cp_data:
                    try:
                        now = datetime.utcnow().isoformat()
                        # Ensure channel exists
                        cursor.execute(
                            "INSERT OR IGNORE INTO CHANNELS (channel_id, name) VALUES (?, ?)",
                            (entry['channel_id'], None)
                        )
                        # Check for existing webhook data to preserve
                        cursor.execute(
                            "SELECT webhook_id, webhook_token FROM CHANNEL_PERSONAS WHERE channel_id = ?",
                            (entry['channel_id'],)
                        )
                        existing = cursor.fetchone()
                        webhook_id = existing[0] if existing else None
                        webhook_token = existing[1] if existing else None

                        cursor.execute(
                            """INSERT OR REPLACE INTO CHANNEL_PERSONAS
                                (channel_id, template_id, display_name, avatar_url, system_prompt,
                                 model, provider, response_style, webhook_id, webhook_token,
                                 is_active, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (entry['channel_id'], entry.get('template_id'),
                             entry['display_name'], entry.get('avatar_url'),
                             entry.get('system_prompt'), entry.get('model'),
                             entry.get('provider'), entry.get('response_style'),
                             webhook_id, webhook_token,
                             1 if entry.get('is_active', True) else 0,
                             entry.get('created_at', now), now)
                        )
                        count += 1
                    except (sqlite3.Error, KeyError) as e:
                        errors.append(f"CHANNEL_PERSONAS entry error: {e}")
                applied['CHANNEL_PERSONAS'] = count

                # USERS - upsert
                u_data = tables.get("USERS", [])
                count = 0
                for entry in u_data:
                    try:
                        cursor.execute(
                            """INSERT INTO USERS (user_id, preferences_json)
                               VALUES (?, ?)
                               ON CONFLICT(user_id) DO UPDATE SET
                                   preferences_json = excluded.preferences_json""",
                            (entry['user_id'], entry.get('preferences_json'))
                        )
                        count += 1
                    except (sqlite3.Error, KeyError) as e:
                        errors.append(f"USERS entry error: {e}")
                applied['USERS'] = count

            logger.info("Config import complete: %s", applied)

        except sqlite3.Error as e:
            logger.error(f"Transaction error during config import: {e}", exc_info=True)
            errors.append(f"Transaction error: {e}")

        return {"applied": applied, "errors": errors}

    # ── SQLite Backup ─────────────────────────────────────────

    def create_sqlite_backup(self, backup_path: str) -> str:
        """Create a consistent SQLite backup using the online backup API.

        Args:
            backup_path: Destination file path for the backup.

        Returns:
            The backup file path.
        """
        backup_conn = sqlite3.connect(backup_path)
        try:
            with getattr(self._conn, "lock", threading.RLock()):
                getattr(self._conn, "raw", self._conn).backup(backup_conn)
            logger.info(f"SQLite backup created: {backup_path}")
        finally:
            backup_conn.close()
        return backup_path

    # ── Validate SQLite Backup ────────────────────────────────

    def validate_sqlite_backup(self, backup_path: str) -> Dict[str, Any]:
        """Validate an uploaded SQLite file.

        Checks:
            - File has valid SQLite magic bytes
            - Expected tables exist
            - Returns table list and row counts

        Returns:
            dict with keys: valid (bool), errors (list), info (dict)
        """
        errors = []
        info = {}

        # Check file exists and size
        if not os.path.exists(backup_path):
            return {"valid": False, "errors": ["File not found"], "info": {}}

        file_size = os.path.getsize(backup_path)
        info['size_bytes'] = file_size

        # Check SQLite magic bytes
        try:
            with open(backup_path, 'rb') as f:
                header = f.read(16)
            if not header.startswith(b'SQLite format 3\x00'):
                errors.append("File is not a valid SQLite database")
                return {"valid": False, "errors": errors, "info": info}
        except IOError as e:
            errors.append(f"Cannot read file: {e}")
            return {"valid": False, "errors": errors, "info": info}

        # Open and inspect the database
        try:
            conn = sqlite3.connect(backup_path)
            cursor = conn.cursor()

            # Get table list
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [row[0] for row in cursor.fetchall()]
            info['tables'] = tables

            # Check for expected tables
            missing = [t for t in self.EXPECTED_TABLES if t not in tables]
            if missing:
                errors.append(f"Missing expected tables: {', '.join(missing)}")

            # Get row counts for key tables
            table_counts = {}
            for table in tables:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM [{table}]")
                    table_counts[table] = cursor.fetchone()[0]
                except sqlite3.Error:
                    table_counts[table] = -1
            info['table_counts'] = table_counts

            conn.close()
        except sqlite3.Error as e:
            errors.append(f"Cannot open as SQLite database: {e}")
            return {"valid": False, "errors": errors, "info": info}

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "info": info
        }

    # ── Restore SQLite Backup ─────────────────────────────────

    def restore_sqlite_backup(self, backup_path: str) -> Dict[str, Any]:
        """Restore from a SQLite backup file.

        Creates a .pre_restore.bak of the current database, then uses the
        SQLite backup API to copy the uploaded file into the live connection.

        Args:
            backup_path: Path to the validated backup file.

        Returns:
            dict with keys: success (bool), error (str or None), bak_path (str)
        """
        bak_path = self._db_path + ".pre_restore.bak"

        try:
            # Step 1: Back up current database
            bak_conn = sqlite3.connect(bak_path)
            try:
                getattr(self._conn, "raw", self._conn).backup(bak_conn)
            finally:
                bak_conn.close()
            logger.info(f"Pre-restore backup saved to: {bak_path}")

            # Step 2: Copy uploaded backup into the live connection
            source_conn = sqlite3.connect(backup_path)
            try:
                # Hold the connection lock so no other thread uses it mid-restore
                with getattr(self._conn, "lock", threading.RLock()):
                    source_conn.backup(getattr(self._conn, "raw", self._conn))
            finally:
                source_conn.close()
            logger.info(f"Database restored from: {backup_path}")

            return {"success": True, "error": None, "bak_path": bak_path}

        except sqlite3.Error as e:
            logger.error(f"Database restore failed: {e}", exc_info=True)
            return {"success": False, "error": str(e), "bak_path": bak_path}
