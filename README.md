# Photo Organizer

A Docker-based application that automatically organizes your photos and videos by date using EXIF metadata.

## Features

- Watch multiple directories for new photos and videos
- Organize files by date in the format `output_path/YYYY/YYYY-MM-DD/original_filename`
- Extract dates from EXIF metadata for images and videos
- Dry-run mode to preview file organization without making changes
- Never overwrites: a photo is skipped if the output already has the same picture at equal or better quality (or within 10% resolution and size); otherwise it is copied next to it with a numeric suffix (e.g. `IMG_0001_1.jpg`)
- Skip list: processed files are recorded in `.photo-organizer-skiplist.db` in the output directory (SQLite, keyed by path + size + modified time) so unchanged files are not re-analyzed on later scans. Dry runs read it but never write it. Delete the file to force a full re-evaluation.
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
- `--incorrect-meta-dates 2022-10-22,2022-10-23`: Images whose date falls on one of these days are filed under `1996/1996-01-01/` and the copy's EXIF dates are rewritten to 1996-01-01 (source files are never modified). Omit to use the original metadata for every file
- `--scan-existing`: Scan and process existing files in watch directories at startup

## Building the container

```
echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
docker build -t ghcr.io/YOUR_GITHUB_USERNAME/photo-organizer:latest .
docker push ghcr.io/YOUR_GITHUB_USERNAME/photo-organizer:latest
```

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