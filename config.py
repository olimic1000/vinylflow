"""
Configuration management for Vinyl Digitizer.
Loads settings from .env file and provides defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv


class Config:
    """Configuration manager for Vinyl Digitizer."""

    def __init__(self, env_path=None):
        """
        Initialize configuration.

        Args:
            env_path: Optional path to .env file. If None, looks in current directory.
        """
        if env_path is None:
            env_path = Path(__file__).parent / '.env'
        else:
            env_path = Path(env_path)

        # Load .env file if it exists
        if env_path.exists():
            load_dotenv(env_path)

        # Discogs API settings
        self.discogs_token = os.getenv('DISCOGS_USER_TOKEN', '')
        self.discogs_user_agent = os.getenv('DISCOGS_USER_AGENT', 'VinylDigitizer/1.0')

        # Output settings
        self.default_output_dir = os.getenv(
            'DEFAULT_OUTPUT_DIR',
            str(Path.home() / 'Music' / 'new 12-inches')
        )

        # Audio processing settings
        self.default_silence_threshold = float(os.getenv('DEFAULT_SILENCE_THRESHOLD', '-40'))
        self.default_min_silence_duration = float(os.getenv('DEFAULT_MIN_SILENCE_DURATION', '1.5'))
        self.default_min_track_length = float(os.getenv('DEFAULT_MIN_TRACK_LENGTH', '30'))
        self.default_flac_compression = int(os.getenv('DEFAULT_FLAC_COMPRESSION', '8'))

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
        path = Path.cwd() / '.env'
    else:
        path = Path(path)

    template = """# Vinyl Digitizer Configuration

# Discogs API Settings
# Get your token from: https://www.discogs.com/settings/developers
DISCOGS_USER_TOKEN=your_token_here
DISCOGS_USER_AGENT=VinylDigitizer/1.0

# Output Settings
DEFAULT_OUTPUT_DIR=/Users/oliviermichelet/Music/new 12-inches

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
