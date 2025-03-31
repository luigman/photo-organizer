# Photo Organizer

A Docker-based application that automatically organizes your photos and videos by date using EXIF metadata.

## Features

- Watch multiple directories for new photos and videos
- Organize files by date in the format `output_path/YYYY/YYYY-MM-DD/original_filename`
- Extract dates from EXIF metadata for images and videos
- Dry-run mode to preview file organization without making changes
- Skip existing files to prevent duplicates
- Process existing files at startup
- Supports common image formats (JPG, PNG, GIF, BMP, TIFF, HEIC, WEBP)
- Supports common video formats (MP4, MOV, AVI, MKV, WEBM, MPG, M4V)

## Setup Instructions

1. **Clone this repository**

2. **Modify the configuration**

   Edit `docker-compose.yml` to set your watch and output directories:

   ```yaml
   volumes:
     - /path/to/photos1:/data/photos/dir1
     - /path/to/photos2:/data/photos/dir2
     - /path/to/output:/data/output
   ```

   You can add as many watch directories as needed.

3. **Build and run the container**

   ```bash
   docker-compose up -d
   ```

   This will build the Docker image and start the container in the background.

## Advanced Configuration

### Customizing Watch Paths

You can modify the `config.json` file to specify which directories to watch:

```json
{
  "watch_paths": [
    "/data/photos/dir1",
    "/data/photos/dir2"
  ],
  "output_path": "/data/output"
}
```

### Running in Dry-Run Mode

To run in dry-run mode (which logs what would happen without actually copying files), uncomment the command line in `docker-compose.yml`:

```yaml
command: ["python", "photo_organizer.py", "--scan-existing", "--dry-run"]
```

### Command Line Options

The application supports the following command line options:

- `--config PATH`: Path to the configuration file (default: `/app/config.json`)
- `--dry-run`: Run in dry-run mode (no files will be copied)
- `--scan-existing`: Scan and process existing files in watch directories at startup

## Troubleshooting

### Checking Logs

To view the logs of the running container:

```bash
docker logs photo-organizer
```

### No Files Being Organized

- Verify that the watch paths in `config.json` match the volume mounts in `docker-compose.yml`
- Make sure file formats are supported (see Features for supported formats)
- Check logs for any errors or warnings

### Permission Issues

If you see permission errors in the logs, make sure the Docker container has write access to your output directory:

```bash
chmod -R 755 /path/to/output
```

## File Organization Logic

1. When a new file is detected in a watch directory, its date is extracted:
   - For images: EXIF DateTimeOriginal or DateTime tag
   - For videos: Creation time metadata
   - Falls back to file modification time if metadata is unavailable

2. The file is then copied to:
   ```
   /data/output/YYYY/YYYY-MM-DD/original_filename
   ```

3. If a file with the same name already exists at the destination, it is skipped.