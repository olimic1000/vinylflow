#!/usr/bin/env python3
"""
Vinyl Digitizer - Automated vinyl record digitization tool.
Converts WAV recordings to FLAC with silence detection and Discogs metadata.
"""

import argparse
import sys
import tempfile
import shutil
from pathlib import Path
from typing import List, Optional, Dict

from config import Config, create_default_env_file
from audio_processor import AudioProcessor, Track
from metadata_handler import MetadataHandler, DiscogsRelease, compare_track_durations


class VinylDigitizer:
    """Main application class."""

    def __init__(self, config: Config, dry_run=False, verbose=False):
        """
        Initialize digitizer.

        Args:
            config: Configuration object
            dry_run: If True, don't actually process files
            verbose: Print detailed output
        """
        self.config = config
        self.dry_run = dry_run
        self.verbose = verbose

        # Initialize processors
        self.audio_processor = AudioProcessor(
            silence_threshold=config.default_silence_threshold,
            min_silence_duration=config.default_min_silence_duration,
            min_track_length=config.default_min_track_length,
            flac_compression=config.default_flac_compression
        )

        self.metadata_handler = MetadataHandler(
            discogs_token=config.discogs_token,
            user_agent=config.discogs_user_agent
        )

    def process_file(self, input_file: Path, output_dir: Optional[Path] = None) -> bool:
        """
        Process a single WAV file.

        Args:
            input_file: Path to input WAV file
            output_dir: Output directory (uses config default if None)

        Returns:
            True if successful
        """
        if output_dir is None:
            output_dir = Path(self.config.default_output_dir)

        print(f"\n{'='*70}")
        print(f"Processing: {input_file.name}")
        print(f"{'='*70}\n")

        # Step 1: Validate input file
        print("Step 1: Validating input file...")
        is_valid, msg = self.audio_processor.validate_audio_file(input_file)
        if not is_valid:
            print(f"❌ {msg}")
            return False
        print(f"✓ {msg}")

        if self.dry_run:
            print("\n[DRY RUN MODE - Not actually processing]\n")

        # Step 2: Detect silence and find tracks
        print("\nStep 2: Detecting tracks...")
        try:
            detected_tracks = self.audio_processor.detect_silence(input_file, verbose=self.verbose)
        except Exception as e:
            print(f"❌ {e}")
            return False

        if not detected_tracks:
            print("❌ No tracks detected")
            return False

        print(f"✓ Detected {len(detected_tracks)} tracks")

        # Step 3: Search Discogs
        print("\nStep 3: Searching Discogs...")
        query = self.metadata_handler.clean_filename(input_file.name)
        print(f"Search query: \"{query}\"")

        release = self._interactive_discogs_search(query)
        if release is None:
            print("❌ Skipped (no release selected)")
            return False

        # Step 4: Map tracks (allow re-detection with adjusted parameters)
        print("\nStep 4: Mapping tracks to Discogs...")
        while True:
            result = self._map_tracks(detected_tracks, release, input_file)
            if result == 'retry':
                # Re-detect with new parameters
                print("\nRe-detecting tracks with new parameters...")
                try:
                    detected_tracks = self.audio_processor.detect_silence(input_file, verbose=self.verbose)
                    if not detected_tracks:
                        print("❌ No tracks detected with new parameters")
                        return False
                    print(f"✓ Detected {len(detected_tracks)} tracks")
                    continue
                except Exception as e:
                    print(f"❌ Re-detection failed: {e}")
                    return False
            elif result:
                break  # Mapping successful
            else:
                print("❌ Track mapping failed or cancelled")
                return False

        if self.dry_run:
            print("\n✓ Dry run complete - all checks passed")
            return True

        # Step 5: Extract tracks
        print("\nStep 5: Extracting tracks to FLAC...")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            output_files = self.audio_processor.extract_all_tracks(
                input_file, detected_tracks, temp_path, verbose=self.verbose
            )

            if len(output_files) != len(detected_tracks):
                print(f"❌ Only extracted {len(output_files)}/{len(detected_tracks)} tracks")
                return False

            print(f"✓ Extracted {len(output_files)} tracks")

            # Step 6: Download cover art
            print("\nStep 6: Downloading cover art...")
            cover_data = None
            if release.cover_url:
                cover_temp = temp_path / 'cover.jpg'
                if self.metadata_handler.download_cover_art(release.cover_url, cover_temp):
                    print(f"✓ Downloaded cover art")
                    cover_data = self.metadata_handler.prepare_cover_for_embedding(cover_temp)
                else:
                    print("⚠️  Failed to download cover art (continuing without)")
            else:
                print("⚠️  No cover art available")

            # Step 7: Tag files
            print("\nStep 7: Writing metadata tags...")
            for track, temp_file in zip(detected_tracks, output_files):
                success = self.metadata_handler.tag_flac_file(
                    temp_file, track, release, cover_data
                )
                if not success:
                    print(f"⚠️  Failed to tag {track.vinyl_number}")

            print(f"✓ Tagged {len(output_files)} files")

            # Step 8: Move to final location
            print("\nStep 8: Moving files to output directory...")
            album_folder_name = self.metadata_handler.create_album_folder_name(release)
            album_dir = output_dir / album_folder_name
            album_dir.mkdir(parents=True, exist_ok=True)

            for track, temp_file in zip(detected_tracks, output_files):
                final_filename = self.metadata_handler.create_track_filename(track, release)
                final_path = album_dir / final_filename
                shutil.move(str(temp_file), str(final_path))
                if self.verbose:
                    print(f"  {final_filename}")

            # Save cover art as folder.jpg
            if cover_data:
                folder_jpg = album_dir / 'folder.jpg'
                with open(folder_jpg, 'wb') as f:
                    f.write(cover_data)

            print(f"✓ Saved to: {album_dir}")

        # Summary
        print(f"\n{'='*70}")
        print("✓ SUCCESS")
        print(f"{'='*70}")
        print(f"Album: {release.artist} - {release.title} ({release.year})")
        print(f"Tracks: {len(detected_tracks)} ({', '.join([t.vinyl_number for t in detected_tracks])})")
        print(f"Output: {album_dir}")
        print()

        return True

    def _interactive_discogs_search(self, query: str) -> Optional[DiscogsRelease]:
        """
        Interactive Discogs search with user selection.

        Args:
            query: Search query

        Returns:
            DiscogsRelease or None
        """
        while True:
            results = self.metadata_handler.search_releases(query, max_results=5)

            if not results:
                print(f"No results found for: {query}")
                action = input("Enter new search query, or [s]kip: ").strip()
                if action.lower() == 's':
                    return None
                query = action
                continue

            print(f"\nSearch: \"{query}\"\n")
            for idx, release in results:
                print(f"{idx}. {release.display_summary()}")

            print()
            choice = input("Select [1-5], enter custom search, or [s]kip: ").strip()

            if choice.lower() == 's':
                return None

            if choice.isdigit():
                idx = int(choice)
                for result_idx, release in results:
                    if result_idx == idx:
                        print(f"\nSelected: {release.display_summary()}")
                        return release
                print(f"Invalid selection: {choice}")
            else:
                # Custom search
                query = choice

    def _map_tracks(self, detected_tracks: List[Track], release: DiscogsRelease, input_file: Path):
        """
        Map detected tracks to Discogs vinyl positions.

        Args:
            detected_tracks: List of detected Track objects
            release: DiscogsRelease object
            input_file: Path to input file (for re-detection)

        Returns:
            True if mapping successful, False if cancelled, 'retry' if should re-detect
        """
        print(f"\nDetected: {len(detected_tracks)} tracks")
        print(f"Discogs:  {len(release.tracks)} tracks")

        # Debug: Show release info
        if len(release.tracks) == 0:
            print(f"\nDEBUG: Release info:")
            print(f"  ID: {release.id}")
            print(f"  Title: {release.title}")
            print(f"  Artist: {release.artist}")
            print(f"  Format: {release.format}")
            print(f"  This release appears to have no tracklist data on Discogs.")
            print(f"  Try selecting a different version/pressing of the release.")

        # Check for count mismatch
        if len(detected_tracks) != len(release.tracks):
            print(f"\n⚠️  Track count mismatch!")
            print(f"Detected {len(detected_tracks)} tracks, but Discogs lists {len(release.tracks)}")

            # Run duration comparison
            comparison = compare_track_durations(detected_tracks, release.tracks)

            if comparison['warnings']:
                print()
                for warning in comparison['warnings']:
                    print(warning)

            print("\nOptions:")
            print("  [c] Continue anyway and manually map")
            print("  [a] Adjust silence parameters and re-detect")
            print("  [d] Use duration-based splitting")
            print("  [s] Skip this file")

            choice = input("\nChoice: ").strip().lower()

            if choice == 's':
                return False
            elif choice == 'a':
                # Adjust silence parameters
                print("\nCurrent parameters:")
                print(f"  Silence threshold: {self.audio_processor.silence_threshold} dB")
                print(f"  Min silence duration: {self.audio_processor.min_silence_duration}s")
                print()

                try:
                    threshold_input = input(f"New threshold (press Enter to keep {self.audio_processor.silence_threshold}): ").strip()
                    if threshold_input:
                        new_threshold = float(threshold_input)
                        self.audio_processor.silence_threshold = new_threshold

                    duration_input = input(f"New min silence duration (press Enter to keep {self.audio_processor.min_silence_duration}): ").strip()
                    if duration_input:
                        new_duration = float(duration_input)
                        self.audio_processor.min_silence_duration = new_duration

                    print(f"\nUpdated parameters:")
                    print(f"  Silence threshold: {self.audio_processor.silence_threshold} dB")
                    print(f"  Min silence duration: {self.audio_processor.min_silence_duration}s")

                    return 'retry'  # Signal to re-detect tracks
                except ValueError:
                    print("Invalid input. Please enter numeric values.")
                    return False
            elif choice == 'd':
                # Use duration-based splitting
                if all(t.duration_seconds for t in release.tracks):
                    durations = [t.duration_seconds for t in release.tracks]
                    detected_tracks.clear()
                    detected_tracks.extend(
                        self.audio_processor.split_tracks_duration_based(
                            Path("dummy"),  # File path not needed for creating track list
                            durations,
                            verbose=self.verbose
                        )
                    )
                else:
                    print("Cannot use duration-based split: Discogs missing duration data")
                    return False
            elif choice != 'c':
                return False

        # Simple 1:1 mapping
        if len(detected_tracks) == len(release.tracks):
            # Allow user to invert track order or manually reorder
            inverted = False
            custom_mapping = None  # Will store custom order if user specifies

            while True:
                print("\nTrack mapping:")
                print(f"{'Detected':<30} {'Discogs':<50}")
                print(f"{'-'*30} {'-'*50}")

                # Determine Discogs track order
                if custom_mapping:
                    # Use custom mapping order
                    discogs_order = custom_mapping
                elif inverted:
                    # Use reversed order
                    discogs_order = list(reversed(release.tracks))
                else:
                    # Use original order
                    discogs_order = release.tracks

                for det_track, discogs_track in zip(detected_tracks, discogs_order):
                    det_track.vinyl_number = discogs_track.position
                    det_track.title = discogs_track.title

                    det_str = f"Track {det_track.number} ({det_track.format_time(det_track.duration)})"
                    disc_str = f"{discogs_track.position}. {discogs_track.title}"
                    if discogs_track.duration_str:
                        disc_str += f" ({discogs_track.duration_str})"

                    print(f"{det_str:<30} → {disc_str:<50}")

                print()
                if custom_mapping:
                    print("(Custom mapping)")
                elif inverted:
                    print("(Track order reversed)")

                confirm = input("Mapping looks good? [Y/n/r=reverse/m=manual]: ").strip().lower()

                if confirm == 'r':
                    inverted = not inverted
                    custom_mapping = None  # Clear custom mapping
                    continue
                elif confirm == 'm':
                    # Manual reordering
                    print("\nManual track reordering")
                    print("Available Discogs tracks:")
                    for i, dt in enumerate(release.tracks, 1):
                        print(f"  {i}. {dt.position} - {dt.title}")
                    print()
                    print("Enter the order for your detected tracks.")
                    print(f"Example: For {len(detected_tracks)} tracks, enter: 1,2,3,4 or 3,4,1,2 etc.")
                    print("(Use track numbers from the list above)")

                    order_input = input("\nEnter order (comma-separated): ").strip()

                    try:
                        # Parse the input
                        indices = [int(x.strip()) - 1 for x in order_input.split(',')]

                        # Validate
                        if len(indices) != len(release.tracks):
                            print(f"❌ Error: You must specify {len(release.tracks)} tracks")
                            continue

                        if any(i < 0 or i >= len(release.tracks) for i in indices):
                            print(f"❌ Error: Track numbers must be between 1 and {len(release.tracks)}")
                            continue

                        # Create custom mapping
                        custom_mapping = [release.tracks[i] for i in indices]
                        inverted = False  # Clear inverted flag
                        continue

                    except (ValueError, IndexError) as e:
                        print(f"❌ Invalid input: {e}")
                        continue
                elif not confirm or confirm == 'y':
                    return True
                else:
                    return False

            return True

        return False

    def batch_process(self, input_dir: Path, output_dir: Optional[Path] = None) -> Dict:
        """
        Batch process all WAV files in directory.

        Args:
            input_dir: Directory containing WAV files
            output_dir: Output directory

        Returns:
            Dict with statistics
        """
        wav_files = sorted(input_dir.glob('*.wav'))

        if not wav_files:
            print(f"No WAV files found in: {input_dir}")
            return {'success': 0, 'failed': 0, 'skipped': 0}

        print(f"\nFound {len(wav_files)} WAV files")
        print(f"{'='*70}\n")

        stats = {'success': 0, 'failed': 0, 'skipped': 0}

        for i, wav_file in enumerate(wav_files, 1):
            print(f"\n[{i}/{len(wav_files)}]")

            try:
                if self.process_file(wav_file, output_dir):
                    stats['success'] += 1
                else:
                    stats['skipped'] += 1
            except KeyboardInterrupt:
                print("\n\nBatch processing interrupted by user")
                break
            except Exception as e:
                print(f"\n❌ Unexpected error: {e}")
                stats['failed'] += 1

        # Print summary
        print(f"\n{'='*70}")
        print("BATCH PROCESSING COMPLETE")
        print(f"{'='*70}")
        print(f"Success:  {stats['success']}")
        print(f"Skipped:  {stats['skipped']}")
        print(f"Failed:   {stats['failed']}")
        print(f"Total:    {len(wav_files)}")
        print()

        return stats


def check_dependencies():
    """Check that all dependencies are installed."""
    import subprocess

    print("Checking dependencies...\n")

    # Check ffmpeg
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        if result.returncode == 0:
            version = result.stdout.decode().split('\n')[0]
            print(f"✓ {version}")
        else:
            print("❌ ffmpeg not working")
            return False
    except FileNotFoundError:
        print("❌ ffmpeg not installed")
        return False

    # Check flac
    try:
        result = subprocess.run(['flac', '--version'], capture_output=True, timeout=5)
        if result.returncode == 0:
            version = result.stdout.decode().strip()
            print(f"✓ {version}")
        else:
            print("❌ flac not working")
            return False
    except FileNotFoundError:
        print("❌ flac not installed")
        return False

    # Check Python packages
    try:
        import mutagen
        import discogs_client
        import dotenv
        import requests
        import PIL
        print("✓ All Python packages installed")
    except ImportError as e:
        print(f"❌ Missing Python package: {e.name}")
        return False

    print("\n✓ All dependencies OK\n")
    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Vinyl Digitizer - Automated vinyl record digitization',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Check command
    subparsers.add_parser('check', help='Check dependencies and configuration')

    # Init command
    subparsers.add_parser('init', help='Create default .env configuration file')

    # Process command
    process_parser = subparsers.add_parser('process', help='Process a single WAV file')
    process_parser.add_argument('file', type=Path, help='WAV file to process')
    process_parser.add_argument('-o', '--output-dir', type=Path, help='Output directory')
    process_parser.add_argument('--silence-threshold', type=float, help='Silence threshold (dB)')
    process_parser.add_argument('--min-silence-duration', type=float, help='Min silence duration (seconds)')
    process_parser.add_argument('--dry-run', action='store_true', help='Show what would be done without processing')
    process_parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')

    # Batch command
    batch_parser = subparsers.add_parser('batch', help='Batch process directory of WAV files')
    batch_parser.add_argument('directory', type=Path, help='Directory containing WAV files')
    batch_parser.add_argument('-o', '--output-dir', type=Path, help='Output directory')
    batch_parser.add_argument('--dry-run', action='store_true', help='Show what would be done without processing')
    batch_parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')

    args = parser.parse_args()

    # Handle commands
    if args.command == 'check':
        if check_dependencies():
            print("Checking configuration...")
            config = Config()
            is_valid, error = config.validate()
            if is_valid:
                print("✓ Configuration valid")
                success, msg = config.test_discogs_connection()
                if success:
                    print(f"✓ {msg}")
                    sys.exit(0)
                else:
                    print(f"❌ {msg}")
                    sys.exit(1)
            else:
                print(f"❌ Configuration error: {error}")
                print("\nRun 'vinyl_digitizer.py init' to create a .env file")
                sys.exit(1)
        else:
            sys.exit(1)

    elif args.command == 'init':
        try:
            env_path = create_default_env_file()
            print(f"✓ Created {env_path}")
            print("\nPlease edit this file and add your Discogs token:")
            print("  https://www.discogs.com/settings/developers")
            sys.exit(0)
        except FileExistsError as e:
            print(f"❌ {e}")
            sys.exit(1)

    elif args.command == 'process':
        config = Config()
        is_valid, error = config.validate()
        if not is_valid:
            print(f"❌ Configuration error: {error}")
            print("Run 'vinyl_digitizer.py check' for more info")
            sys.exit(1)

        # Override config with command-line args
        if args.silence_threshold:
            config.default_silence_threshold = args.silence_threshold
        if args.min_silence_duration:
            config.default_min_silence_duration = args.min_silence_duration

        digitizer = VinylDigitizer(config, dry_run=args.dry_run, verbose=args.verbose)
        success = digitizer.process_file(args.file, args.output_dir)
        sys.exit(0 if success else 1)

    elif args.command == 'batch':
        config = Config()
        is_valid, error = config.validate()
        if not is_valid:
            print(f"❌ Configuration error: {error}")
            sys.exit(1)

        digitizer = VinylDigitizer(config, dry_run=args.dry_run, verbose=args.verbose)
        stats = digitizer.batch_process(args.directory, args.output_dir)
        sys.exit(0 if stats['failed'] == 0 else 1)

    else:
        parser.print_help()
        sys.exit(0)


if __name__ == '__main__':
    main()
