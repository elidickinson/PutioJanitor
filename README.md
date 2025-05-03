# Put.io Storage Manager

A Python script that automatically manages storage on put.io by deleting oldest video files from designated folders when space falls below a threshold.

## Features

- Automatically maintains at least 10 GB of free space on your put.io account
- Only deletes files from specified folders ("chill.institute" and "putfirst")
- Deletes oldest files first, based on creation date
- Treats subfolders containing movie files as complete units (deletes entire folder)
- Can be run manually or automatically via GitHub Actions

## Requirements

- Python 3.6+
- put.io API token with read/write access
- Required Python packages: `requests`, `tus.py`

## Installation

1. Clone this repository
2. Install required packages:
   ```
   pip install requests tus.py
   ```
3. Set your put.io API token as an environment variable:
   ```
   export PUTIO_TOKEN=your_api_token_here
   ```

## Usage

Run the script with default settings (10 GB threshold):

```
python put_io_manager.py
```

Run with custom threshold:

```
python put_io_manager.py --threshold 15
```

Test without deleting any files (dry run):

```
python put_io_manager.py --dry-run
```

Enable debug logging:

```
python put_io_manager.py --debug
```

## GitHub Actions Integration

This repository includes a GitHub Actions workflow that runs the script automatically once per day at 2:00 AM UTC. To use it:

1. Fork this repository
2. Go to your repository's Settings > Secrets and add your put.io API token as `PUTIO_TOKEN`
3. That's it! The workflow will run automatically.

You can also manually trigger the workflow from the Actions tab in your repository.

## Customization

To change which folders are monitored, edit the `DELETABLE_FOLDERS` variable in `put_io_manager.py`.

## License

MIT