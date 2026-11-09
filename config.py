"""
VinylFlow - Configuration Management

Loads settings from .env file and provides configuration defaults.
Handles Discogs API credentials, audio processing parameters, and output settings.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

# Change this constant to flip the default for new installs.
# "sequential_disc_per_lp" — A+B share disc 1, C+D share disc 2, etc. TRACKNUMBER
#                            is sequential across both sides of each LP, with track
#                            and disc totals. Single LP appears as 1 disc.
#                            VINYLPOSITION custom tag preserved.
# "vinyl"                  — TRACKNUMBER="A1" (legacy behaviour, no DISCNUMBER or totals written).
DEFAULT_TRACK_NUMBERING = "sequential_disc_per_lp"
TRACK_NUMBERING_SCHEMES = ("sequential_disc_per_lp", "vinyl")
# The default before it changed: what an older settings.json without a saved choice was using.
LEGACY_TRACK_NUMBERING = "vinyl"


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
            config_dir_env = os.getenv("VINYLFLOW_CONFIG_DIR")
            if config_dir_env:
                settings_path = Path(config_dir_env) / "settings.json"
            else:
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
        self.default_output_dir = (
            json_settings.get('DEFAULT_OUTPUT_DIR') or
            os.getenv("DEFAULT_OUTPUT_DIR", str(Path.home() / "Music" / "new 12-inches"))
        )

        # Audio processing settings
        self.default_silence_threshold = float(os.getenv("DEFAULT_SILENCE_THRESHOLD", "-40"))
        self.default_min_silence_duration = float(os.getenv("DEFAULT_MIN_SILENCE_DURATION", "1.5"))
        self.default_min_track_length = float(os.getenv("DEFAULT_MIN_TRACK_LENGTH", "30"))
        self.default_flac_compression = int(os.getenv("DEFAULT_FLAC_COMPRESSION", "8"))

        # Import folder: recordings here are read in place. Not settable through
        # PUT /api/config, so a web page can never widen what is importable.
        self.import_dir = (
            json_settings.get('IMPORT_DIR') or
            os.getenv("VINYLFLOW_IMPORT_DIR") or
            ""
        )

        # Temp file management
        self.temp_ttl_hours = float(os.getenv("TEMP_TTL_HOURS", "2"))

        # Track numbering scheme
        self.track_numbering = self._resolve_track_numbering(json_settings)

    def _resolve_track_numbering(self, json_settings: dict) -> str:
        """The numbering scheme, keeping an older install on the scheme it was using.

        A non-empty settings.json without a scheme comes from before the default changed:
        new installs record the default on their first save (``_write_settings``). Pin
        ``vinyl`` there once, so the next record sorts with the rest of the library. A
        missing or unreadable file loads as empty, so it is never rewritten here. Unknown
        values keep acting as ``vinyl``, as they always have.
        """
        scheme = json_settings.get("TRACK_NUMBERING") or os.getenv("TRACK_NUMBERING")
        if not scheme:
            if not json_settings:
                return DEFAULT_TRACK_NUMBERING
            self._write_settings({"TRACK_NUMBERING": LEGACY_TRACK_NUMBERING})
            return LEGACY_TRACK_NUMBERING
        if scheme not in TRACK_NUMBERING_SCHEMES:
            print(f"Warning: unknown TRACK_NUMBERING {scheme!r}, using {LEGACY_TRACK_NUMBERING!r}")
            return LEGACY_TRACK_NUMBERING
        return scheme

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

        if self.temp_ttl_hours <= 0:
            return False, "Temp file TTL must be positive"

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

    def _write_settings(self, updates: dict) -> bool:
        """Merge *updates* into settings.json and write to disk."""
        settings_path = Path(self._settings_path)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {}
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
            except Exception:
                pass
        if not settings and not os.getenv("TRACK_NUMBERING"):
            # A new install's first save: record the default, or the next start would take
            # this file for one from before it changed (``_resolve_track_numbering``).
            settings["TRACK_NUMBERING"] = DEFAULT_TRACK_NUMBERING
        settings.update(updates)
        try:
            with open(settings_path, 'w') as f:
                json.dump(settings, f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to save settings: {e}")
            return False

    def save_token(self, token: str, user_agent: str = None) -> bool:
        updates = {'DISCOGS_USER_TOKEN': token}
        if user_agent:
            updates['DISCOGS_USER_AGENT'] = user_agent
        return self._write_settings(updates)

    def save_output_dir(self, output_dir: str) -> bool:
        return self._write_settings({'DEFAULT_OUTPUT_DIR': output_dir})

    def save_track_numbering(self, scheme: str) -> bool:
        return self._write_settings({'TRACK_NUMBERING': scheme})

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
            f"  flac_compression={self.default_flac_compression},\n"
            f"  temp_ttl_hours={self.temp_ttl_hours}h\n"
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
