import os
import shutil
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
        self.supported_extensions = {
            'images': ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.heic', '.webp'],
            'videos': ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mpg', '.m4v']
        }
        logger.info(f"Photo Organizer initialized with watch paths: {watch_paths}")
        logger.info(f"Output path: {output_path}")
        logger.info(f"Dry run mode: {dry_run}")

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
                    logger.debug(f"Could not extract EXIF data from {file_path}: {e}")
            
            # Handle video files
            elif file_ext in self.supported_extensions['videos']:
                try:
                    # Try to get metadata from ffmpeg
                    probe = ffmpeg.probe(file_path)
                    if 'creation_time' in probe['format']['tags']:
                        date_str = probe['format']['tags']['creation_time']
                        return datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S.%fZ')
                except Exception as e:
                    logger.debug(f"Could not extract video metadata from {file_path}: {e}")
            
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
            
            # Create the full destination path
            dest_path = os.path.join(dest_dir, filename)
            
            # Check if destination file already exists
            if os.path.exists(dest_path):
                logger.info(f"Skipping file as it already exists at destination: {dest_path}")
                return
            
            # Create directory if it doesn't exist
            if not os.path.exists(dest_dir) and not self.dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                logger.debug(f"Created directory: {dest_dir}")
            
            # Copy the file
            if self.dry_run:
                logger.info(f"DRY RUN: Would copy {file_path} to {dest_path}")
            else:
                shutil.copy2(file_path, dest_path)
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
    organizer = PhotoOrganizer(watch_paths, output_path, args.dry_run)
    
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