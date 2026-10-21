import sqlite3
import logging

logger = logging.getLogger(__name__)

class SchemaManager:
    """Handles database schema initialization and migrations."""

    # Core table definitions (excluding news feed tables)
    CORE_TABLES = [
        """
        CREATE TABLE IF NOT EXISTS GLOBAL_CONFIG (
            key TEXT PRIMARY KEY,
            value TEXT,
            type TEXT NOT NULL CHECK(type IN ('string', 'int', 'float', 'bool', 'json'))
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS CHANNELS (
            channel_id TEXT PRIMARY KEY,
            name TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS CHANNEL_CONFIG (
            channel_id TEXT PRIMARY KEY,
            model TEXT,
            provider TEXT,
            system_prompt TEXT,
            FOREIGN KEY (channel_id) REFERENCES CHANNELS(channel_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS THREADS (
            thread_id TEXT PRIMARY KEY,
            channel_id TEXT,
            name TEXT,
            created_at DATETIME NOT NULL,
            model TEXT,
            system_prompt TEXT,
            FOREIGN KEY (channel_id) REFERENCES CHANNELS(channel_id) ON DELETE SET NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS MESSAGES (
            message_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            thread_id TEXT,
            user_id TEXT,
            user_name TEXT,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
            content TEXT NOT NULL,
            timestamp DATETIME NOT NULL,
            FOREIGN KEY (channel_id) REFERENCES CHANNELS(channel_id) ON DELETE CASCADE,
            FOREIGN KEY (thread_id) REFERENCES THREADS(thread_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS USERS (
            user_id TEXT PRIMARY KEY,
            preferences_json TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS REMINDERS (
            reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message TEXT NOT NULL,
            due_timestamp DATETIME NOT NULL,
            created_at DATETIME NOT NULL,
            sent BOOLEAN NOT NULL DEFAULT 0,
            FOREIGN KEY (channel_id) REFERENCES CHANNELS(channel_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS TRIVIA_GAME_SESSIONS (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL UNIQUE,
            channel_id TEXT NOT NULL,
            user_id TEXT,
            game_mode TEXT NOT NULL CHECK(game_mode IN ('solo', 'competitive')),
            category TEXT,
            difficulty TEXT NOT NULL CHECK(difficulty IN ('easy', 'medium', 'hard')),
            questions_total INTEGER NOT NULL DEFAULT 10,
            questions_answered INTEGER NOT NULL DEFAULT 0,
            started_at DATETIME NOT NULL,
            ended_at DATETIME,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            FOREIGN KEY (channel_id) REFERENCES CHANNELS(channel_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS TRIVIA_LEADERBOARD (
            user_id TEXT NOT NULL,
            server_id TEXT NOT NULL,
            total_games INTEGER NOT NULL DEFAULT 0,
            total_questions INTEGER NOT NULL DEFAULT 0,
            total_correct INTEGER NOT NULL DEFAULT 0,
            total_points INTEGER NOT NULL DEFAULT 0,
            current_streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            average_response_time REAL NOT NULL DEFAULT 0.0,
            last_played DATETIME,
            PRIMARY KEY (user_id, server_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS TRIVIA_ACHIEVEMENTS (
            achievement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            server_id TEXT NOT NULL,
            achievement_type TEXT NOT NULL,
            achievement_name TEXT NOT NULL,
            earned_at DATETIME NOT NULL,
            metadata_json TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS API_KEYS (
            key_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            encrypted_key TEXT NOT NULL,
            key_alias TEXT,
            created_at DATETIME NOT NULL,
            last_used DATETIME,
            last_validated DATETIME,
            is_active BOOLEAN DEFAULT 1,
            validation_status TEXT DEFAULT 'untested'
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS API_KEY_AUDIT (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT NOT NULL,
            action TEXT NOT NULL,
            timestamp DATETIME NOT NULL,
            user_identifier TEXT,
            details TEXT,
            FOREIGN KEY (key_id) REFERENCES API_KEYS(key_id) ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS PERSONA_TEMPLATES (
            template_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            avatar_url TEXT,
            system_prompt TEXT,
            model TEXT,
            provider TEXT,
            response_style TEXT,
            description TEXT,
            is_builtin BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS CHANNEL_PERSONAS (
            channel_id TEXT PRIMARY KEY,
            template_id TEXT,
            display_name TEXT NOT NULL,
            avatar_url TEXT,
            system_prompt TEXT,
            model TEXT,
            provider TEXT,
            response_style TEXT,
            webhook_id TEXT,
            webhook_token TEXT,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME,
            FOREIGN KEY (channel_id) REFERENCES CHANNELS(channel_id) ON DELETE CASCADE,
            FOREIGN KEY (template_id) REFERENCES PERSONA_TEMPLATES(template_id) ON DELETE SET NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS CHANNEL_MEMORY (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            conversation_start DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        # Per-user notes, scoped to one channel: (channel_id, user_id) is the
        # key, so a user's notes in one channel are never visible in another
        """
        CREATE TABLE IF NOT EXISTS CHANNEL_USER_MEMORY (
            channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            notes TEXT NOT NULL,
            display_name TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (channel_id, user_id)
        );
        """
    ]

    # Index definitions
    INDEXES = [
        """CREATE INDEX IF NOT EXISTS idx_messages_channel_timestamp ON MESSAGES (channel_id, timestamp);""",
        """CREATE INDEX IF NOT EXISTS idx_messages_thread_timestamp ON MESSAGES (thread_id, timestamp);""",
        """CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON MESSAGES (timestamp);""",
        """CREATE INDEX IF NOT EXISTS idx_reminders_due_timestamp ON REMINDERS (due_timestamp);""",
        """CREATE INDEX IF NOT EXISTS idx_reminders_sent ON REMINDERS (sent);""",
        """CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON REMINDERS (user_id);""",
        """CREATE INDEX IF NOT EXISTS idx_trivia_sessions_thread_id ON TRIVIA_GAME_SESSIONS (thread_id);""",
        """CREATE INDEX IF NOT EXISTS idx_trivia_sessions_is_active ON TRIVIA_GAME_SESSIONS (is_active);""",
        """CREATE INDEX IF NOT EXISTS idx_trivia_leaderboard_server_id ON TRIVIA_LEADERBOARD (server_id);""",
        """CREATE INDEX IF NOT EXISTS idx_trivia_achievements_user_server ON TRIVIA_ACHIEVEMENTS (user_id, server_id);""",
        """CREATE INDEX IF NOT EXISTS idx_api_keys_provider ON API_KEYS (provider);""",
        """CREATE INDEX IF NOT EXISTS idx_api_keys_active ON API_KEYS (is_active);""",
        """CREATE INDEX IF NOT EXISTS idx_api_key_audit_key_id ON API_KEY_AUDIT (key_id);""",
        """CREATE INDEX IF NOT EXISTS idx_api_key_audit_timestamp ON API_KEY_AUDIT (timestamp);""",
        """CREATE INDEX IF NOT EXISTS idx_channel_personas_template ON CHANNEL_PERSONAS (template_id);""",
        """CREATE INDEX IF NOT EXISTS idx_channel_personas_active ON CHANNEL_PERSONAS (is_active);""",
        """CREATE INDEX IF NOT EXISTS idx_persona_templates_builtin ON PERSONA_TEMPLATES (is_builtin);""",
        """CREATE INDEX IF NOT EXISTS idx_channel_memory_channel_id ON CHANNEL_MEMORY (channel_id);""",
        """CREATE INDEX IF NOT EXISTS idx_channel_memory_created_at ON CHANNEL_MEMORY (channel_id, created_at);""",
        """CREATE INDEX IF NOT EXISTS idx_channel_user_memory_user ON CHANNEL_USER_MEMORY (user_id);"""
    ]

    @staticmethod
    def initialize(conn):
        """Creates all tables and indexes, runs migrations."""
        try:
            with conn:
                cursor = conn.cursor()

                # Create core tables
                for statement in SchemaManager.CORE_TABLES:
                    cursor.execute(statement)

                # Create indexes
                for statement in SchemaManager.INDEXES:
                    cursor.execute(statement)

                logger.info("Database schema creation/verification complete.")

                # Run migrations
                SchemaManager._migrate_remove_news_feeds(cursor)
                SchemaManager._add_column_if_not_exists(cursor, "CHANNEL_CONFIG", "provider", "TEXT")
                SchemaManager._add_column_if_not_exists(cursor, "CHANNEL_CONFIG", "session_timeout_hours", "INTEGER")
                SchemaManager._add_column_if_not_exists(cursor, "CHANNEL_CONFIG", "memory_summary_enabled", "INTEGER")
                SchemaManager._add_column_if_not_exists(cursor, "CHANNEL_CONFIG", "max_memory_summaries", "INTEGER")
                SchemaManager._add_column_if_not_exists(cursor, "CHANNEL_MEMORY", "conversation_start", "DATETIME")
                SchemaManager._add_column_if_not_exists(cursor, "MESSAGES", "user_name", "TEXT")
                SchemaManager._add_column_if_not_exists(cursor, "MESSAGES", "is_bot", "INTEGER")
                SchemaManager._add_column_if_not_exists(cursor, "CHANNEL_CONFIG", "user_memory_enabled", "INTEGER")
                SchemaManager._create_memory_fts(cursor)

                logger.info("Database schema migration checks complete.")
        except sqlite3.Error as e:
            logger.error(f"Error initializing/migrating database schema: {e}", exc_info=True)
            raise

    @staticmethod
    def _create_memory_fts(cursor):
        """Creates the FTS5 index over CHANNEL_MEMORY summaries (with sync triggers)."""
        try:
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(summary, content='CHANNEL_MEMORY', content_rowid='id');
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_fts_ai AFTER INSERT ON CHANNEL_MEMORY BEGIN
                    INSERT INTO memory_fts(rowid, summary) VALUES (new.id, new.summary);
                END;
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_fts_ad AFTER DELETE ON CHANNEL_MEMORY BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
                END;
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS memory_fts_au AFTER UPDATE ON CHANNEL_MEMORY BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, summary) VALUES ('delete', old.id, old.summary);
                    INSERT INTO memory_fts(rowid, summary) VALUES (new.id, new.summary);
                END;
            """)
            # Index any summaries that predate the FTS table
            cursor.execute("INSERT INTO memory_fts(memory_fts) VALUES ('rebuild');")
            logger.debug("memory_fts FTS5 index verified.")
        except sqlite3.Error as e:
            # FTS5 may be unavailable in exotic SQLite builds — memory search
            # degrades gracefully (search_memories returns []).
            logger.warning(f"Could not create memory_fts FTS5 index: {e}")

    @staticmethod
    def _migrate_remove_news_feeds(cursor):
        """Migration: Remove all news feed related tables."""
        tables_to_drop = [
            'NEWS_FEEDS',
            'NEWS_CHANNEL_SUBSCRIPTIONS',
            'NEWS_ARTICLES_HISTORY',
            'article_summaries',
            'user_article_summaries'
        ]

        for table in tables_to_drop:
            try:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
                logger.info(f"Migration: Dropped news feed table '{table}'")
            except sqlite3.Error as e:
                logger.warning(f"Could not drop table '{table}': {e}")

    @staticmethod
    def _add_column_if_not_exists(cursor: sqlite3.Cursor, table_name: str, column_name: str, column_type: str):
        """Helper to add a column to a table if it doesn't already exist."""
        try:
            # Check if column exists using PRAGMA table_info
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [column[1] for column in cursor.fetchall()]
            if column_name not in columns:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                logger.info(f"Added column '{column_name}' to table '{table_name}'.")
            else:
                logger.debug(f"Column '{column_name}' already exists in table '{table_name}'.")
        except sqlite3.Error as e:
            # Specifically ignore "duplicate column name" error if ALTER TABLE fails concurrently
            if "duplicate column name" in str(e).lower():
                logger.warning(f"Attempted to add duplicate column '{column_name}' to '{table_name}', ignoring. Error: {e}")
            else:
                logger.error(f"Error adding column '{column_name}' to table '{table_name}': {e}", exc_info=True)
                raise
