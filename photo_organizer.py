import os
import shutil
import hashlib
import threading
import time
import json
import logging
import argparse
from datetime import datetime
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

class PhotoOrganizer:
    def __init__(self, watch_paths, output_path, dry_run=False):
        self.watch_paths = watch_paths
        self.output_path = output_path
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self.validate_paths(watch_paths, output_path)
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

    @staticmethod
    def _file_hash(path):
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _files_identical(cls, a, b):
        """True if both are regular files with identical content."""
        if os.path.islink(a) or os.path.islink(b):
            return False
        if not (os.path.isfile(a) and os.path.isfile(b)):
            return False
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return cls._file_hash(a) == cls._file_hash(b)

    def resolve_destination(self, src_path, dest_dir, filename):
        """
        Pick a destination path that never overwrites existing data.
        Returns None if an identical file is already present, otherwise a path
        that does not exist yet (adding _1, _2, ... on a name collision).
        """
        stem, ext = os.path.splitext(filename)
        candidate = os.path.join(dest_dir, filename)
        counter = 0
        while os.path.lexists(candidate):
            if self._files_identical(src_path, candidate):
                return None
            counter += 1
            candidate = os.path.join(dest_dir, f"{stem}_{counter}{ext}")
        return candidate

    @staticmethod
    def _safe_copy(src_path, dest_path):
        """
        Copy to a temp file in the destination directory, then publish it under
        its final name atomically and without ever replacing an existing file.
        Raises FileExistsError if dest_path appeared in the meantime.
        """
        dest_dir = os.path.dirname(dest_path)
        tmp_path = os.path.join(
            dest_dir,
            f".{os.path.basename(dest_path)}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            shutil.copy2(src_path, tmp_path)
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
            # Get the file date
            file_date = self.get_file_date(file_path)
            if not file_date:
                logger.warning(f"Could not determine date for {file_path}, skipping")
                return
            
            # Create destination path: output_path/YYYY/YYYY-MM-DD/
            year_dir = str(file_date.year)
            date_dir = file_date.strftime('%Y-%m-%d')
            dest_dir = os.path.join(self.output_path, year_dir, date_dir)
            
            # Get original filename
            filename = os.path.basename(file_path)
            
            with self._lock:
                dest_path = self.resolve_destination(file_path, dest_dir, filename)
                if dest_path is None:
                    logger.info(f"Skipping {file_path}: identical file already exists in {dest_dir}")
                    return
                if os.path.basename(dest_path) != filename:
                    logger.warning(
                        f"Name conflict for {file_path}: a different file named {filename} "
                        f"exists, using {os.path.basename(dest_path)}"
                    )

                if self.dry_run:
                    logger.info(f"DRY RUN: Would copy {file_path} to {dest_path}")
                    return

                os.makedirs(dest_dir, exist_ok=True)
                try:
                    self._safe_copy(file_path, dest_path)
                except FileExistsError:
                    logger.warning(f"Destination appeared during copy, not overwriting: {dest_path}")
                    return
                logger.info(f"Copied {file_path} to {dest_path}")

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
    
    # Process existing files if requested
    if args.scan_existing:
        logger.info("Scanning existing files in watch directories")
        for directory in watch_paths:
            organizer.process_directory(directory)
    
    # Set up observers for each watch path
    observers = []
    event_handler = MediaFileHandler(organizer)
    
    for path in watch_paths:
        observer = Observer()
        observer.schedule(event_handler, path, recursive=True)
        observer.start()
        observers.append(observer)
        logger.info(f"Started watching directory: {path}")
    
    try:
        logger.info("Photo Organizer is running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping Photo Organizer")
        for observer in observers:
            observer.stop()
    
    for observer in observers:
        observer.join()

if __name__ == "__main__":
    main()