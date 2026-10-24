import os
import shutil
import hashlib
import threading
import time
import json
import logging
import argparse
import signal
import sqlite3
from datetime import date, datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PIL import Image
from PIL.ExifTags import TAGS
import piexif
import ffmpeg

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('photo-organizer')

class SkipList:
    """
    Persistent record of source files that have already been processed, stored
    as an indexed SQLite table in the output directory. A file is identified by
    path + size + mtime, so a file that is modified or replaced is processed again.
    Lookups are single primary-key reads, so this stays fast with 100k+ entries.
    Writes are batched; losing a batch in a crash only means those files are
    re-evaluated (the duplicate check makes that harmless).
    """
    FLUSH_EVERY = 100        # commit after this many pending writes...
    FLUSH_INTERVAL = 5.0     # ...or this many seconds since the last commit

    def __init__(self, db_path, read_only=False):
        self.db_path = db_path
        self.read_only = read_only
        self._lock = threading.Lock()
        self._pending = 0
        self._last_flush = time.monotonic()
        self._conn = None
        try:
            if read_only:
                # Dry run: read an existing list, never create or modify it
                if os.path.exists(db_path):
                    uri = f"file:{db_path}?mode=ro"
                    self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                self._conn = sqlite3.connect(db_path, check_same_thread=False)
                # Default rollback journal: WAL needs shared memory, which
                # network and FUSE filesystems (NFS, unRAID shares) often lack.
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS processed ("
                    "path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, "
                    "result TEXT NOT NULL, dest TEXT, processed_at TEXT NOT NULL) WITHOUT ROWID"
                )
                self._conn.commit()
            if self._conn is not None:
                count = self._conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
                logger.info(f"Skip list loaded from {db_path}: {count} entries")
        except sqlite3.Error as e:
            logger.warning(f"Skip list unavailable ({e}); continuing without it")
            self._conn = None

    def contains(self, path, size, mtime_ns):
        if self._conn is None:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed WHERE path = ? AND size = ? AND mtime_ns = ?",
                (path, size, mtime_ns)
            ).fetchone()
        return row is not None

    def add(self, path, size, mtime_ns, result, dest=None):
        if self._conn is None or self.read_only:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO processed VALUES (?, ?, ?, ?, ?, ?)",
                    (path, size, mtime_ns, result, dest, datetime.now().isoformat(timespec='seconds'))
                )
                self._pending += 1
                if (self._pending >= self.FLUSH_EVERY
                        or time.monotonic() - self._last_flush >= self.FLUSH_INTERVAL):
                    self._flush_locked()
            except sqlite3.Error as e:
                logger.warning(f"Could not update skip list: {e}")

    def _flush_locked(self):
        self._conn.commit()
        self._pending = 0
        self._last_flush = time.monotonic()

    def flush_if_due(self):
        if self._pending and time.monotonic() - self._last_flush >= self.FLUSH_INTERVAL:
            self.flush()

    def flush(self):
        if self._conn is None or self.read_only:
            return
        with self._lock:
            try:
                self._flush_locked()
            except sqlite3.Error as e:
                logger.warning(f"Could not flush skip list: {e}")

    def close(self):
        if self._conn is None:
            return
        self.flush()
        with self._lock:
            self._conn.close()
            self._conn = None


class PhotoOrganizer:
    def __init__(self, watch_paths, output_path, dry_run=False):
        self.watch_paths = watch_paths
        self.output_path = output_path
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self.validate_paths(watch_paths, output_path)
        self.skip_list = SkipList(os.path.join(output_path, self.SKIP_LIST_NAME), read_only=dry_run)
        self.supported_extensions = {
            'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.heic', '.webp'],
            'videos': ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mpg', '.m4v']
        }
        logger.info(f"Photo Organizer initialized with watch paths: {watch_paths}")
        logger.info(f"Output path: {output_path}")
        logger.info(f"Dry run mode: {dry_run}")

    @staticmethod
    def _is_within(path, parent):
        path = os.path.realpath(path)
        parent = os.path.realpath(parent)
        return os.path.commonpath([path, parent]) == parent

    @classmethod
    def validate_paths(cls, watch_paths, output_path):
        """
        Refuse configurations where the output directory and a watch directory
        overlap, which would make the organizer re-process its own output.
        """
        for watch in watch_paths:
            if cls._is_within(output_path, watch) or cls._is_within(watch, output_path):
                raise ValueError(
                    f"Output path {output_path} and watch path {watch} must not overlap"
                )

    SKIP_LIST_NAME = '.photo-organizer-skiplist.db'

    # Source images dated on these days have bad metadata: file them under
    # CORRECTED_DATE and rewrite the EXIF date in the copy (time of day is kept).
    BAD_METADATA_DATES = {date(2022, 10, 21), date(2022, 10, 22), date(2022, 10, 23)}
    CORRECTED_DATE = date(1996, 1, 1)

    # Content-aware duplicate detection settings
    HASH_DISTANCE_THRESHOLD = 6   # max differing bits (of 64) to call two images the same picture
    VIDEO_DURATION_TOLERANCE = 1.0  # seconds
    QUALITY_TOLERANCE = 0.10      # "about the same" resolution / file size

    _ORIENTATION_OPS = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }

    @staticmethod
    def _file_hash(path):
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _image_info(cls, path):
        """Return ('image', pixels, 64-bit difference hash), honoring EXIF orientation."""
        with Image.open(path) as img:
            width, height = img.size
            orientation = img.getexif().get(274, 1)
            img.draft('L', (128, 128))  # cheap downscaled decode for JPEGs
            gray = img.convert('L')
        if orientation in (5, 6, 7, 8):
            width, height = height, width
        if orientation in cls._ORIENTATION_OPS:
            gray = gray.transpose(cls._ORIENTATION_OPS[orientation])
        small = gray.resize((9, 8), Image.Resampling.LANCZOS)
        px = list(small.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
        return ('image', width * height, bits)

    @staticmethod
    def _video_info(path):
        """Return ('video', pixels, duration_seconds)."""
        probe = ffmpeg.probe(path)
        stream = next(s for s in probe['streams'] if s.get('codec_type') == 'video')
        duration = float(probe['format']['duration'])
        return ('video', int(stream['width']) * int(stream['height']), duration)

    def _media_info(self, path):
        """Content signature for a file, or None if it can't be decoded."""
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in self.supported_extensions['images']:
                return self._image_info(path)
            if ext in self.supported_extensions['videos']:
                return self._video_info(path)
        except Exception as e:
            logger.debug(f"Could not analyze content of {path}: {e}")
        return None

    def _same_content(self, a, b):
        if a[0] != b[0]:
            return False
        if a[0] == 'image':
            return bin(a[2] ^ b[2]).count('1') <= self.HASH_DISTANCE_THRESHOLD
        return abs(a[2] - b[2]) <= self.VIDEO_DURATION_TOLERANCE

    def _within_tolerance(self, incoming, existing):
        return abs(incoming - existing) <= self.QUALITY_TOLERANCE * existing

    def _should_skip(self, src_path, existing_path):
        """
        Decide whether src_path adds nothing over existing_path. True when both
        show the same content and either the existing file is at least as good
        (resolution and file size) or the two are within 10% of each other on
        both. Different content, or a clearly better incoming file, returns False.
        """
        if os.path.islink(existing_path) or not os.path.isfile(existing_path):
            return False
        src_size = os.path.getsize(src_path)
        existing_size = os.path.getsize(existing_path)

        src_info = self._media_info(src_path)
        existing_info = self._media_info(existing_path)
        if src_info is None or existing_info is None:
            # Content can't be decoded: only an exact byte match counts as a duplicate
            return src_size == existing_size and self._file_hash(src_path) == self._file_hash(existing_path)

        if not self._same_content(src_info, existing_info):
            return False

        src_pixels, existing_pixels = src_info[1], existing_info[1]
        existing_better = existing_pixels >= src_pixels and existing_size >= src_size
        about_equal = (self._within_tolerance(src_pixels, existing_pixels)
                       and self._within_tolerance(src_size, existing_size))
        logger.debug(
            f"Same content: {src_path} ({src_pixels}px, {src_size}B) vs {existing_path} "
            f"({existing_pixels}px, {existing_size}B): existing_better={existing_better}, "
            f"about_equal={about_equal}"
        )
        return existing_better or about_equal

    def resolve_destination(self, src_path, dest_dir, filename):
        """
        Pick a destination path that never overwrites existing data.
        Returns None if an existing file (this name or a _N variant) already
        covers the incoming file, otherwise a path that does not exist yet
        (adding _1, _2, ... when the content differs or the incoming file is
        clearly better).
        """
        stem, ext = os.path.splitext(filename)
        candidate = os.path.join(dest_dir, filename)
        counter = 0
        while os.path.lexists(candidate):
            if self._should_skip(src_path, candidate):
                return None
            counter += 1
            candidate = os.path.join(dest_dir, f"{stem}_{counter}{ext}")
        return candidate

    @staticmethod
    def _safe_copy(src_path, dest_path, post_copy=None):
        """
        Copy to a temp file in the destination directory, then publish it under
        its final name atomically and without ever replacing an existing file.
        post_copy, if given, is called with the temp file path before publishing
        (used to fix metadata on the copy; the source is never modified).
        Raises FileExistsError if dest_path appeared in the meantime.
        """
        dest_dir = os.path.dirname(dest_path)
        tmp_path = os.path.join(
            dest_dir,
            f".{os.path.basename(dest_path)}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            shutil.copy2(src_path, tmp_path)
            if post_copy is not None:
                post_copy(tmp_path)
            try:
                # Fails with FileExistsError if dest_path exists (even as a symlink)
                os.link(tmp_path, dest_path)
            except FileExistsError:
                raise
            except OSError:
                # Filesystem without hard link support: reserve the name
                # exclusively, then move the finished file over the reservation.
                fd = os.open(dest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                os.replace(tmp_path, dest_path)
        finally:
            if os.path.lexists(tmp_path):
                os.remove(tmp_path)

    def get_file_date(self, file_path):
        """
        Extract date from file using EXIF data or file metadata.
        Returns a datetime object or None if date cannot be extracted.
        """
        file_ext = os.path.splitext(file_path)[1].lower()
        
        try:
            # Handle image files
            if file_ext in self.supported_extensions['images']:
                try:
                    # Try to get EXIF data
                    with Image.open(file_path) as img:
                        exif_data = img._getexif()
                        if exif_data:
                            # Look for DateTimeOriginal tag (36867) or DateTime tag (306)
                            for tag_id in [36867, 306]:
                                if tag_id in exif_data:
                                    date_str = exif_data[tag_id]
                                    return datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
                except Exception as e:
                    logger.warning(f"Could not extract EXIF data from {file_path}: {e}")
            
            # Handle video files
            elif file_ext in self.supported_extensions['videos']:
                try:
                    # Try to get metadata from ffmpeg
                    probe = ffmpeg.probe(file_path)
                    if 'creation_time' in probe['format']['tags']:
                        date_str = probe['format']['tags']['creation_time']
                        return datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S.%fZ')
                except Exception as e:
                    logger.warning(f"Could not extract video metadata from {file_path}: {e}")
            
        except Exception as e:
            logger.error(f"Error processing file {file_path}: {e}")
        
        # Fallback to file modification time
        file_mtime = os.path.getmtime(file_path)
        return datetime.fromtimestamp(file_mtime)

    def _needs_date_fix(self, file_path, file_date):
        ext = os.path.splitext(file_path)[1].lower()
        return (ext in self.supported_extensions['images']
                and file_date.date() in self.BAD_METADATA_DATES)

    def _fix_copy_date(self, tmp_path, new_date):
        """Rewrite the EXIF dates and mtime of a copied image (never the source)."""
        stamp = new_date.strftime('%Y:%m:%d %H:%M:%S').encode()
        try:
            try:
                exif_dict = piexif.load(tmp_path)
            except Exception:
                exif_dict = {'0th': {}, 'Exif': {}, 'GPS': {}, 'Interop': {}, '1st': {}, 'thumbnail': None}
            exif_dict['0th'][piexif.ImageIFD.DateTime] = stamp
            exif_dict['Exif'][piexif.ExifIFD.DateTimeOriginal] = stamp
            exif_dict['Exif'][piexif.ExifIFD.DateTimeDigitized] = stamp
            piexif.insert(piexif.dump(exif_dict), tmp_path)
        except Exception as e:
            # Formats piexif can't write (PNG, GIF, HEIC, ...) keep the folder and mtime fix only
            logger.warning(f"Could not rewrite EXIF date in copy {tmp_path}: {e!r}")
        ts = new_date.timestamp()
        os.utime(tmp_path, (ts, ts))

    def organize_file(self, file_path):
        """
        Organize a single file according to its date.
        """
        file_ext = os.path.splitext(file_path)[1].lower()
        
        # Check if file is a supported type
        all_extensions = self.supported_extensions['images'] + self.supported_extensions['videos']
        if file_ext not in all_extensions:
            logger.debug(f"Skipping unsupported file: {file_path}")
            return
        
        try:
            # Skip files already processed (unchanged since last time)
            stat = os.stat(file_path)
            size, mtime_ns = stat.st_size, stat.st_mtime_ns
            if self.skip_list.contains(file_path, size, mtime_ns):
                logger.debug(f"Skipping already processed file: {file_path}")
                return

            # Get the file date
            file_date = self.get_file_date(file_path)
            if not file_date:
                logger.warning(f"Could not determine date for {file_path}, skipping")
                return
            
            corrected_date = None
            if self._needs_date_fix(file_path, file_date):
                corrected_date = file_date.replace(
                    year=self.CORRECTED_DATE.year, month=self.CORRECTED_DATE.month,
                    day=self.CORRECTED_DATE.day)
                logger.info(f"{file_path} is dated {file_date.date()} (bad metadata), "
                            f"filing under {corrected_date.date()}")
                file_date = corrected_date

            # Create destination path: output_path/YYYY/YYYY-MM-DD/
            year_dir = str(file_date.year)
            date_dir = file_date.strftime('%Y-%m-%d')
            dest_dir = os.path.join(self.output_path, year_dir, date_dir)
            
            # Get original filename
            filename = os.path.basename(file_path)
            
            with self._lock:
                dest_path = self.resolve_destination(file_path, dest_dir, filename)
                if dest_path is None:
                    logger.info(f"Skipping {file_path}: an equal or better copy already exists in {dest_dir}")
                    self.skip_list.add(file_path, size, mtime_ns, 'duplicate')
                    return
                if os.path.basename(dest_path) != filename:
                    logger.warning(
                        f"Name conflict for {file_path}: a file named {filename} "
                        f"exists that is not an equal or better copy, using {os.path.basename(dest_path)}"
                    )

                if self.dry_run:
                    logger.info(f"DRY RUN: Would copy {file_path} to {dest_path}")
                    return

                os.makedirs(dest_dir, exist_ok=True)
                try:
                    post_copy = (lambda tmp: self._fix_copy_date(tmp, corrected_date)) if corrected_date else None
                    self._safe_copy(file_path, dest_path, post_copy)
                except FileExistsError:
                    logger.warning(f"Destination appeared during copy, not overwriting: {dest_path}")
                    return
                logger.info(f"Copied {file_path} to {dest_path}")
                self.skip_list.add(file_path, size, mtime_ns, 'copied', dest_path)

        except Exception as e:
            logger.error(f"Error organizing file {file_path}: {e}")

    def process_directory(self, directory):
        """
        Process all files in a directory recursively.
        """
        logger.info(f"Processing directory: {directory}")
        for root, _, files in os.walk(directory):
            for filename in files:
                file_path = os.path.join(root, filename)
                self.organize_file(file_path)
        self.skip_list.flush()

    def close(self):
        self.skip_list.close()

class MediaFileHandler(FileSystemEventHandler):
    def __init__(self, organizer):
        self.organizer = organizer
        
    def on_created(self, event):
        if not event.is_directory:
            logger.debug(f"File created: {event.src_path}")
            self.organizer.organize_file(event.src_path)
            
    def on_moved(self, event):
        if not event.is_directory:
            logger.debug(f"File moved: {event.dest_path}")
            self.organizer.organize_file(event.dest_path)

def main():
    parser = argparse.ArgumentParser(description='Photo Organizer')
    parser.add_argument('--config', type=str, default='/app/config.json', 
                        help='Path to configuration file')
    parser.add_argument('--dry-run', action='store_true', 
                        help='Perform a dry run without copying files')
    parser.add_argument('--scan-existing', action='store_true', 
                        help='Scan existing files in watch directories on startup')
    args = parser.parse_args()
    
    # Load configuration
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except Exception as e:
        logger.error(f"Error loading configuration: {e}")
        return
    
    watch_paths = config.get('watch_paths', [])
    output_path = config.get('output_path', '')
    
    if not watch_paths or not output_path:
        logger.error("Watch paths and output path must be specified in the configuration")
        return
    
    # Create organizer
    try:
        organizer = PhotoOrganizer(watch_paths, output_path, args.dry_run)
    except ValueError as e:
        logger.error(str(e))
        return
    
    # Docker stops containers with SIGTERM; handle it like Ctrl+C so the skip list is flushed
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))

    observers = []
    try:
        # Process existing files if requested
        if args.scan_existing:
            logger.info("Scanning existing files in watch directories")
            for directory in watch_paths:
                organizer.process_directory(directory)

        # Set up observers for each watch path
        event_handler = MediaFileHandler(organizer)

        for path in watch_paths:
            observer = Observer()
            observer.schedule(event_handler, path, recursive=True)
            observer.start()
            observers.append(observer)
            logger.info(f"Started watching directory: {path}")

        logger.info("Photo Organizer is running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            organizer.skip_list.flush_if_due()
    except KeyboardInterrupt:
        logger.info("Stopping Photo Organizer")
    finally:
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join()
        organizer.close()

if __name__ == "__main__":
    main()