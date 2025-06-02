#!/usr/bin/env python3
"""
YouTube Music Downloader with Metadata Organization
Requires: yt-dlp, mutagen, requests
Install with: pip install yt-dlp mutagen requests
"""
import time
import threading

import re
from pathlib import Path
from typing import Dict, Optional, List
import sys
import requests

try:
    import yt_dlp
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, APIC
except ImportError as e:
    print(f"Missing required packages. Install with: pip install yt-dlp mutagen requests")
    sys.exit(1)

class YouTubeMusicDownloader:
    def __init__(self, base_dir: str = "Downloads/Music"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.track_counters = {}  # Track counter per album

    def sanitize_filename(self, filename: str) -> str:
        """Remove invalid characters from filename"""
        return re.sub(r'[<>:"/\\|?*]', '', filename).strip()

    def extract_metadata_from_title(self, title: str, video_info: Dict = None) -> Dict[str, str]:
        """Extract artist, song, album, and year from title, video_info, and description"""
        metadata = {"artist": "", "song": "", "album": "", "track": "", "year": ""}

        description = video_info.get('description', '') if video_info else ''
        uploader = video_info.get('uploader', '') if video_info else ''

        # --- Prefer album name from tags ---
        album_fields = ['album', 'album_title', 'music_album', 'music_album_title']
        for field in album_fields:
            album_value = video_info.get(field)
            if album_value and isinstance(album_value, str) and album_value.strip():
                metadata["album"] = album_value.strip()
                break

        # --- Prefer original year of release from tags ---
        year_fields = [
            'release_year', 'release_date', 'original_year', 'year',
            'music_release_year', 'music_release_date'
        ]
        release_year = None
        for field in year_fields:
            value = video_info.get(field)
            if value:
                match = re.search(r'\b(\d{4})\b', str(value))
                if match:
                    release_year = match.group(1)
                    break
        if release_year:
            metadata["year"] = release_year

        # --- Album/year from description ---
        if not metadata["album"] or not metadata["year"]:
            album_year_match = re.search(r'Album\s*•\s*(.+?)\s*•\s*(\d{4})', description)
            if album_year_match:
                if not metadata["album"]:
                    metadata["album"] = album_year_match.group(1).strip()
                if not metadata["year"]:
                    metadata["year"] = album_year_match.group(2)
            else:
                album_short_match = re.search(r'Album\s*•\s*(\d{4})', description)
                if album_short_match and not metadata["year"]:
                    metadata["year"] = album_short_match.group(1)
                album_patterns = [
                    r'(?:album|from|off)\s*[:\-]\s*(.+?)(?:\n|$)',
                    r'(?:álbum|album):\s*(.+?)(?:\n|$)',
                    r'from\s+the\s+album\s+["\'](.+?)["\']',
                    r'off\s+the\s+album\s+["\'](.+?)["\']',
                ]
                for pattern in album_patterns:
                    match = re.search(pattern, description, re.IGNORECASE | re.MULTILINE)
                    if match:
                        potential_album = match.group(1).strip()
                        potential_album = re.sub(r'^\W+|\W+$', '', potential_album)
                        if 2 < len(potential_album) < 100:
                            if not metadata["album"]:
                                metadata["album"] = potential_album
                            break

        # --- Fallback to upload year ---
        if not metadata["year"] and video_info:
            upload_date = video_info.get('upload_date', '')
            if upload_date and len(upload_date) >= 4:
                metadata["year"] = upload_date[:4]

        # --- Extract artist and song from title ---
        patterns = [
            r'^(.+?)\s*-\s*(.+)$',  # Artist - Song
            r'^(.+?)\s*:\s*(.+)$',  # Artist: Song
            r'^(.+?)\s+by\s+(.+)$', # Song by Artist
            r'^(.+?)\s*\|\s*(.+)$', # Artist | Song
        ]
        for pattern in patterns:
            match = re.match(pattern, title, re.IGNORECASE)
            if match:
                if "by" in pattern:
                    metadata["song"] = match.group(1).strip()
                    metadata["artist"] = match.group(2).strip()
                else:
                    metadata["artist"] = match.group(1).strip()
                    metadata["song"] = match.group(2).strip()
                break

        if not metadata["song"]:
            metadata["song"] = title
        if not metadata["artist"]:
            metadata["artist"] = uploader or "Unknown Artist"

        return metadata

    def get_user_metadata(self, video_info: Dict) -> Dict[str, str]:
        """Get metadata from user input with auto-suggestions"""
        title = video_info.get('title', '')
        uploader = video_info.get('uploader', '')
        description = video_info.get('description', '')

        print(f"\nVideo: {title}")
        print(f"Channel: {uploader}")

        # Auto-extract suggestions
        auto_meta = self.extract_metadata_from_title(title, video_info)

        metadata = {}

        # Artist
        suggestion = auto_meta.get('artist', uploader)
        metadata['artist'] = input(f"Artist [{suggestion}]: ").strip() or suggestion

        # Song
        suggestion = auto_meta.get('song', title)
        metadata['song'] = input(f"Song [{suggestion}]: ").strip() or suggestion

        # Album
        album_suggestion = auto_meta.get('album', '')
        if not album_suggestion:
            album_suggestion = "Single"
        metadata['album'] = input(f"Album [{album_suggestion}]: ").strip() or album_suggestion

        # Auto-increment track number per album
        album_key = f"{metadata['artist']}_{metadata['album']}"
        if album_key not in self.track_counters:
            self.track_counters[album_key] = 1
        else:
            self.track_counters[album_key] += 1

        suggested_track = str(self.track_counters[album_key])
        metadata['track'] = input(f"Track number [{suggested_track}]: ").strip() or suggested_track

        # Year: prefer suggestion from tags/description, fallback to upload date
        year_suggestion = auto_meta.get('year')
        if not year_suggestion:
            upload_date = video_info.get('upload_date', '')
            year_suggestion = upload_date[:4] if upload_date else ''
        metadata['year'] = input(f"Year [{year_suggestion}]: ").strip() or year_suggestion

        return metadata

    def download_cover_art(self, video_info: Dict) -> Optional[bytes]:
        """Download cover art from video thumbnail"""
        try:
            thumbnails = video_info.get('thumbnails', [])
            if not thumbnails:
                return None

            # Find the highest quality thumbnail
            best_thumbnail = None
            max_resolution = 0

            for thumb in thumbnails:
                width = thumb.get('width', 0)
                height = thumb.get('height', 0)
                resolution = width * height

                if resolution > max_resolution:
                    max_resolution = resolution
                    best_thumbnail = thumb

            if not best_thumbnail or not best_thumbnail.get('url'):
                return None

            # Download the thumbnail
            youtube_rate_limiter.acquire()
            response = requests.get(best_thumbnail['url'], timeout=10)
            if response.status_code == 200:
                return response.content

        except Exception as e:
            print(f"Warning: Could not download cover art: {e}")

        return None

    def create_folder_structure(self, metadata: Dict[str, str]) -> Path:
        """Create folder structure: Artist/Album/"""
        artist_folder = self.base_dir / self.sanitize_filename(metadata['artist'])
        album_folder = artist_folder / self.sanitize_filename(metadata['album'])
        album_folder.mkdir(parents=True, exist_ok=True)
        return album_folder

    def add_metadata_to_file(self, file_path: Path, metadata: Dict[str, str], cover_art: Optional[bytes] = None):
        """Add ID3 tags to MP3 file including cover art"""
        try:
            audio_file = MP3(file_path, ID3=ID3)

            try:
                audio_file.add_tags()
            except:
                pass

            audio_file.tags.add(TIT2(encoding=3, text=metadata['song']))
            audio_file.tags.add(TPE1(encoding=3, text=metadata['artist']))
            audio_file.tags.add(TALB(encoding=3, text=metadata['album']))

            if metadata.get('track'):
                audio_file.tags.add(TRCK(encoding=3, text=metadata['track']))

            if metadata.get('year'):
                audio_file.tags.add(TDRC(encoding=3, text=metadata['year']))

            if cover_art:
                audio_file.tags.add(
                    APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=cover_art
                    )
                )
                print(f"✓ Added cover art to {file_path.name}")

            audio_file.save()
            print(f"✓ Added metadata to {file_path.name}")

        except Exception as e:
            print(f"✗ Failed to add metadata: {e}")

    def download_video(self, url: str, interactive: bool = True) -> bool:
        """Download video and organize with metadata"""
        try:
            ydl_opts_info = {
                'quiet': True,
                'no_warnings': True,
            }
            youtube_rate_limiter.acquire()
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                video_info = ydl.extract_info(url, download=False)

            if interactive:
                metadata = self.get_user_metadata(video_info)
            else:
                metadata = self.extract_metadata_from_title(video_info.get('title', ''), video_info)
                if not metadata.get('album'):
                    metadata['album'] = 'Single'
                album_key = f"{metadata.get('artist', 'Unknown')}_{metadata['album']}"
                if album_key not in self.track_counters:
                    self.track_counters[album_key] = 1
                else:
                    self.track_counters[album_key] += 1
                metadata['track'] = str(self.track_counters[album_key])
                if not metadata.get('year'):
                    metadata['year'] = video_info.get('upload_date', '')[:4] if video_info.get('upload_date') else ''

            cover_art = self.download_cover_art(video_info)
            download_folder = self.create_folder_structure(metadata)
            track_num = metadata.get('track', '1').zfill(2)
            filename = f"{track_num}. {self.sanitize_filename(metadata['song'])}"

            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': str(download_folder / f"{filename}.%(ext)s"),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': False,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                print(f"\nDownloading: {metadata['artist']} - {metadata['song']}")
                ydl.download([url])

            mp3_file = download_folder / f"{filename}.mp3"
            if mp3_file.exists():
                self.add_metadata_to_file(mp3_file, metadata, cover_art)
                print(f"✓ Downloaded and organized: {mp3_file}")
                return True
            else:
                print("✗ MP3 file not found after download")
                return False

        except Exception as e:
            print(f"✗ Error downloading {url}: {e}")
            return False

    def download_playlist(self, playlist_url: str, interactive: bool = True):
        """Download entire playlist with organization"""
        try:
            ydl_opts_info = {
                'quiet': True,
                'extract_flat': True,
            }
            youtube_rate_limiter.acquire()
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                playlist_info = ydl.extract_info(playlist_url, download=False)

            entries = playlist_info.get('entries', [])
            playlist_title = playlist_info.get('title', 'Unknown Playlist')

            print(f"\nFound playlist: {playlist_title}")
            print(f"Videos to download: {len(entries)}")

            if interactive:
                proceed = input("Continue? (y/n): ").lower().strip()
                if proceed != 'y':
                    return

            success_count = 0
            for i, entry in enumerate(entries, 1):
                video_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry['id']}"
                print(f"\n[{i}/{len(entries)}] Processing: {entry.get('title', 'Unknown')}")
                if self.download_video(video_url, interactive=False):
                    success_count += 1

            print(f"\n✓ Successfully downloaded {success_count}/{len(entries)} videos")

        except Exception as e:
            print(f"✗ Error processing playlist: {e}")

def main():
    downloader = YouTubeMusicDownloader()
    print("YouTube Music Downloader")
    print("=" * 40)
    while True:
        print("\nOptions:")
        print("1. Download single video")
        print("2. Download playlist")
        print("3. Batch download from file")
        print("4. Change download directory")
        print("5. Exit")
        choice = input("\nSelect option (1-5): ").strip()
        if choice == '1':
            url = input("Enter YouTube URL: ").strip()
            if url:
                downloader.download_video(url)
        elif choice == '2':
            url = input("Enter YouTube playlist URL: ").strip()
            if url:
                downloader.download_playlist(url)
        elif choice == '3':
            file_path = input("Enter path to text file with URLs (one per line): ").strip()
            try:
                with open(file_path, 'r') as f:
                    urls = [line.strip() for line in f if line.strip()]
                print(f"Found {len(urls)} URLs")
                for i, url in enumerate(urls, 1):
                    print(f"\n[{i}/{len(urls)}] Processing: {url}")
                    downloader.download_video(url, interactive=False)
            except FileNotFoundError:
                print("File not found!")
            except Exception as e:
                print(f"Error reading file: {e}")
        elif choice == '4':
            new_dir = input(f"Current directory: {downloader.base_dir}\nEnter new directory: ").strip()
            if new_dir:
                downloader.base_dir = Path(new_dir)
                downloader.base_dir.mkdir(parents=True, exist_ok=True)
                print(f"Changed directory to: {downloader.base_dir}")
        elif choice == '5':
            print("Goodbye!")
            break
        else:
            print("Invalid option!")

#uses import time
#uses import threading
class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
        self.calls = []

    def acquire(self):
        with self.lock:
            now = time.time()
            # Remove outdated calls
            self.calls = [t for t in self.calls if t > now - self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self.calls.append(time.time())

# Singleton instance for all YouTube requests
youtube_rate_limiter = RateLimiter(max_calls=10, period=1)  # 10 requests per second

if __name__ == "__main__":
    main()