"""
VinylFlow - Configuration Management

Loads settings from .env file and provides configuration defaults.
Handles Discogs API credentials, audio processing parameters, and output settings.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv


class Config:
    """Configuration manager for VinylFlow."""

    def __init__(self, env_path=None, settings_path=None):
        """
        Initialize configuration with fallback chain:
        1. settings.json (persistent, user-editable via UI)
        2. .env file (backward compatibility)
        3. Environment variables (Docker)

        Args:
            env_path: Optional path to .env file. If None, looks in current directory.
            settings_path: Optional path to settings.json. If None, uses config/settings.json.
        """
        # Store paths for reload
        if env_path is None:
            env_path = Path(__file__).parent / ".env"
        else:
            env_path = Path(env_path)

        if settings_path is None:
            settings_path = Path(__file__).parent / "config" / "settings.json"
        else:
            settings_path = Path(settings_path)

        self._env_path = env_path
        self._settings_path = settings_path

        # Load .env file if it exists
        if env_path.exists():
            load_dotenv(env_path, override=True)

        # Load JSON settings (takes precedence over .env)
        json_settings = self._load_from_json(settings_path)

        # Discogs API settings (priority: JSON > env var > default)
        self.discogs_token = (
            json_settings.get('DISCOGS_USER_TOKEN') or
            os.getenv("DISCOGS_USER_TOKEN", "")
        )
        self.discogs_user_agent = (
            json_settings.get('DISCOGS_USER_AGENT') or
            os.getenv("DISCOGS_USER_AGENT", "VinylFlow/1.0")
        )

        # Output settings
        self.default_output_dir = os.getenv(
            "DEFAULT_OUTPUT_DIR", str(Path.home() / "Music" / "new 12-inches")
        )

        # Audio processing settings
        self.default_silence_threshold = float(os.getenv("DEFAULT_SILENCE_THRESHOLD", "-40"))
        self.default_min_silence_duration = float(os.getenv("DEFAULT_MIN_SILENCE_DURATION", "1.5"))
        self.default_min_track_length = float(os.getenv("DEFAULT_MIN_TRACK_LENGTH", "30"))
        self.default_flac_compression = int(os.getenv("DEFAULT_FLAC_COMPRESSION", "8"))

    def validate(self):
        """
        Validate configuration.

        Returns:
            tuple: (is_valid, error_message)
        """
        if not self.discogs_token:
            return False, "DISCOGS_USER_TOKEN not set in .env file"

        if self.default_flac_compression < 0 or self.default_flac_compression > 8:
            return False, "FLAC compression level must be between 0 and 8"

        if self.default_silence_threshold > 0:
            return False, "Silence threshold should be negative (dB)"

        if self.default_min_silence_duration <= 0:
            return False, "Minimum silence duration must be positive"

        if self.default_min_track_length <= 0:
            return False, "Minimum track length must be positive"

        return True, None

    def test_discogs_connection(self):
        """
        Test Discogs API connection with current token.

        Returns:
            tuple: (success, message)
        """
        try:
            import discogs_client

            client = discogs_client.Client(self.discogs_user_agent, user_token=self.discogs_token)
            identity = client.identity()
            return True, f"Connected as: {identity.username}"
        except Exception as e:
            return False, f"Discogs connection failed: {str(e)}"

    def _load_from_json(self, settings_path: Path) -> dict:
        """
        Load settings from JSON file if it exists.

        Args:
            settings_path: Path to settings.json file

        Returns:
            dict: Settings dictionary, or empty dict if file doesn't exist
        """
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load settings.json: {e}")
        return {}

    def save_token(self, token: str, user_agent: str = None) -> bool:
        """
        Save Discogs token to settings.json.

        Args:
            token: Discogs API token
            user_agent: Optional user agent string

        Returns:
            bool: True if successful, False otherwise
        """
        settings_path = Path(self._settings_path)
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing settings or create new
        settings = {}
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
            except:
                pass

        # Update token
        settings['DISCOGS_USER_TOKEN'] = token
        if user_agent:
            settings['DISCOGS_USER_AGENT'] = user_agent

        # Write to file
        try:
            with open(settings_path, 'w') as f:
                json.dump(settings, f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to save settings: {e}")
            return False

    def reload(self):
        """
        Reload configuration from all sources (settings.json, .env, environment).
        """
        self.__init__(env_path=self._env_path, settings_path=self._settings_path)

    def __repr__(self):
        """String representation of config (safe - no token)."""
        return (
            f"Config(\n"
            f"  discogs_token={'*' * len(self.discogs_token) if self.discogs_token else 'NOT SET'},\n"
            f"  output_dir={self.default_output_dir},\n"
            f"  silence_threshold={self.default_silence_threshold}dB,\n"
            f"  min_silence_duration={self.default_min_silence_duration}s,\n"
            f"  min_track_length={self.default_min_track_length}s,\n"
            f"  flac_compression={self.default_flac_compression}\n"
            f")"
        )


def create_default_env_file(path=None):
    """
    Create a default .env template file.

    Args:
        path: Path where to create the .env file. Defaults to current directory.
    """
    if path is None:
        path = Path.cwd() / ".env"
    else:
        path = Path(path)

    template = """# VinylFlow Configuration

# Discogs API Settings
# Get your token from: https://www.discogs.com/settings/developers
DISCOGS_USER_TOKEN=your_token_here
DISCOGS_USER_AGENT=VinylFlow/1.0

# Output Settings
DEFAULT_OUTPUT_DIR=/app/output

# Audio Processing Settings
# Silence threshold in dB (negative value)
DEFAULT_SILENCE_THRESHOLD=-40

# Minimum silence duration in seconds to consider as track boundary
DEFAULT_MIN_SILENCE_DURATION=1.5

# Minimum track length in seconds (filter out false positives)
DEFAULT_MIN_TRACK_LENGTH=30

# FLAC compression level (0-8, higher = more compression)
DEFAULT_FLAC_COMPRESSION=8
"""

    if path.exists():
        raise FileExistsError(f"{path} already exists. Not overwriting.")

    path.write_text(template)
    return path
