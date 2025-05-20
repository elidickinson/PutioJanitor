# Put.io Janitor

A Python script that automatically manages storage on put.io by deleting oldest video files from designated folders when free space falls below a threshold. **Nothing to install - it can run as a Github Action for free.**

## Features

- Simple GitHub Actions deployment
- Automatically maintains free space on your put.io account (default: 10 GB)
- Configurable via environment variables - no code changes needed
- Limited to certain files (default: "chill.institute" and "putfirst"
- Deletes oldest files first, based on creation date
- Treats subfolders containing movie files as complete units (deletes entire folder)
- Optionally also purge files from trash (disabled by default)

## Quick Start: Deploy to GitHub Actions

### Get a Token
1. Log in to [put.io](https://put.io)
2. Go to **Settings** → **API**
3. Click **Create new OAuth app**. The details on this page don't matter but the Name should be unique.
  - Name: Put.io Janitor for <your_name>
  - Description: Put.io Janitor
  - Application website: https://github.com/elidickinson/PutioJanitor
  - Callback URL: `urn:ietf:wg:oauth:2.0:oob`
  - "Don't show in Extensions page" is checked
4. Copy the **OAuth Token** for the next step.

### Deploy on Github
1. **Fork this repository** to your GitHub account
2. Go to your forked repo's **Settings** tab → **Secrets and variables** → **Actions**
3. Add a new repository secret named `PUTIO_TOKEN` with your put.io OAuth Token
4. That's it! The workflow will run daily at 10:00 UTC (5:00 AM Eastern Time). Note it only looks in directories "chill.institute" and "putfirst" by default.

## Configuration

All settings can be configured using environment variables:

| Environment Variable | Description | Default Value |
|---|---|---|
| `PUTIO_TOKEN` | **Required** - Your put.io API token | None |
| `PUTIO_SPACE_THRESHOLD_GB` | Free space threshold in GB | 10 |
| `PUTIO_TRASH_CLEANUP_THRESHOLD_GB` | When to clean trash (GB). Set to 0 to disable trash cleanup completely. Only cleans trash when available space falls below this value. | 0 |
| `PUTIO_TRASH_CLEANUP_TARGET_GB` | How much space to free from trash (GB) | 5 |
| `PUTIO_MIN_TRASH_AGE_DAYS` | Minimum age in days for files in trash before they can be deleted | 2 |
| `PUTIO_DELETABLE_FOLDERS` | Comma-separated list of folders to manage | chill.institute,putfirst |
| `PUTIO_MAX_RETRIES` | Maximum API call retry attempts | 3 |
| `PUTIO_RETRY_DELAY` | Seconds between retry attempts | 5 |
| `PUTIO_DRY_RUN` | Set to "true" to run without deleting files | false |

## Manual Usage

If you prefer to run the script manually:

```bash
# Install requirements
pip install requests tus.py

# Set your API token
export PUTIO_TOKEN=your_api_token_here

# Run with default settings (10 GB threshold)
python putio_janitor.py

# Test without deleting any files (dry run)
python putio_janitor.py --dry-run

# Run with custom threshold
python putio_janitor.py --threshold 15

# Enable debug logging
python putio_janitor.py --debug
```

## GitHub Actions Integration

This repository includes a GitHub Actions workflow that runs the script automatically once per day at 10:00 UTC (5:00 AM Eastern Time). The workflow file is already set up in `.github/workflows/cleanup.yml`.

### Testing the Workflow

To test the workflow immediately after setting up:

1. Go to the **Actions** tab in your GitHub repository
2. Select the **Put.io Storage Manager** workflow on the left
3. Click the **Run workflow** button on the right
4. Select the branch and click **Run workflow**

You can check the workflow logs to see what would be deleted. By default, the first run uses `--dry-run` mode which doesn't actually delete anything.

### Customizing the Workflow

To customize the schedule or settings:

1. Edit the `.github/workflows/cleanup.yml` file in your repository
2. For the schedule, modify the `cron` expression under the `schedule` section
3. To configure the environment variables, uncomment and modify the desired variables
4. Commit your changes

### Environment Variables in GitHub Actions

To set environment variables for your workflow:

1. Go to your repository's **Settings** tab → **Secrets and variables** → **Actions**
2. Switch to the **Variables** tab
3. Click **New repository variable**
4. Add your variables (e.g., `PUTIO_SPACE_THRESHOLD_GB` with value `15`)

You can also manually trigger the workflow from the Actions tab in your repository.

## Using Environment Variables Instead of Command-Line Arguments

The script prioritizes environment variables over command-line arguments when both are provided. This is ideal for GitHub Actions deployment.

For example, instead of running:
```bash
python put_io_manager.py --threshold 15 --dry-run
```

You can now use environment variables:
```bash
export PUTIO_SPACE_THRESHOLD_GB=15
export PUTIO_DELETABLE_FOLDERS=movies,downloads
python put_io_manager.py --dry-run
```

### Trash Management Example

If you want files to remain in trash for at least a week before being deleted, and only clean trash when space gets low:

```bash
# Keep files in trash for at least 7 days
export PUTIO_MIN_TRASH_AGE_DAYS=7

# Enable trash cleanup when available space falls below 5 GB
export PUTIO_TRASH_CLEANUP_THRESHOLD_GB=5

# Target freeing up 10 GB when cleaning trash
export PUTIO_TRASH_CLEANUP_TARGET_GB=10

python putio_janitor.py
```

This makes it easy to configure the script via GitHub Actions environment variables without changing the code.

## License

MIT
