# Put.io Janitor

A Python script that automatically manages storage on put.io using a dual-threshold approach. **Nothing to install - it can run as a Github Action for free.**

## Features

- Runs on a schedule on your own Github account for free.

- **Critical Threshold**: Ensures minimum free space by permanently deleting files
- **Comfort Threshold**: Keeps non-trash files below target levels by moving to trash
- Limited to certain files (default: "chill.institute" and "putfirst")
- Deletes oldest files first, based on creation date
- Treats subfolders containing movie files as complete units (deletes entire folder)
- Intelligent trash management with automatic permanent deletion

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

## How It Works

The script uses a two thresholds to manage storage: a critical threshold to maintain enough free space for downloads and a "comfort" threshold to try to move files to the Trash ahead of deleting them.

### Critical Threshold (Default: 6GB)
- **Purpose**: Ensures minimum free space is always available
- **Includes trash** in the free space calculation
- **Action**: Permanently deletes files (not just moving to trash)
- **Priority**: First cleans old files from trash, then from folders if needed

### Comfort Threshold (Default: 10GB)
- **Purpose**: Keeps your active file collection organized
- **Excludes trash** - only counts non-trash files
- **Action**: Moves files to trash (not permanent deletion)
- **Priority**: Runs only after critical threshold is satisfied

### Example
In a 100GB account with 98GB in files, 6GB critical threshold, and 10GB comfort threshold:
1. **Critical cleanup**: Permanently delete ~2GB of files (first from trash, then from folders) to have 6GB free
2. **Comfort cleanup**: Move ~4GB of remaining files to trash to keep non-trash files below 90GB

## Configuration

All settings can be configured using environment variables:

| Environment Variable | Description | Default Value |
|---|---|---|
| `PUTIO_TOKEN` | **Required** - Your put.io API token | None |
| `PUTIO_CRITICAL_THRESHOLD_GB` | Minimum free space required (includes trash) | 6 |
| `PUTIO_COMFORT_THRESHOLD_GB` | Target maximum for non-trash files | 10 |

| `PUTIO_DELETABLE_FOLDERS` | Comma-separated list of folders to manage | chill.institute,putfirst |
| `PUTIO_MAX_RETRIES` | Maximum API call retry attempts | 3 |
| `PUTIO_RETRY_DELAY` | Seconds between retry attempts | 5 |
| `PUTIO_DRY_RUN` | Set to "true" to run without deleting files | false |

## Local Usage

### Using a .env file (recommended for local development)

1. Create a `.env` file in the project directory:
```bash
# .env
PUTIO_TOKEN=your_api_token_here
PUTIO_CRITICAL_THRESHOLD_GB=6
PUTIO_COMFORT_THRESHOLD_GB=10
PUTIO_DELETABLE_FOLDERS=chill.institute,putfirst
PUTIO_DRY_RUN=true  # Remove this when ready to delete files
```

2. Install requirements:
```bash
pip install requests tus.py python-dotenv
```

3. Run the script:
```bash
python putio_janitor.py
```

### Manual Usage (without .env)

If you prefer to run the script manually with environment variables:

```bash
# Install requirements
pip install requests tus.py

# Set your API token
export PUTIO_TOKEN=your_api_token_here

# Run with default settings
python putio_janitor.py

# Test without deleting any files (dry run)
python putio_janitor.py --dry-run

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
python putio_janitor.py --dry-run
```

You can now use environment variables:
```bash
export PUTIO_COMFORT_THRESHOLD_GB=15
export PUTIO_DELETABLE_FOLDERS=movies,downloads
python putio_janitor.py --dry-run
```

### Configuration Examples

**Conservative Setup** (keep more files):
```bash
export PUTIO_CRITICAL_THRESHOLD_GB=8   # Require more free space
export PUTIO_COMFORT_THRESHOLD_GB=15  # Allow more active files
```

**Aggressive Setup** (faster cleanup, less space usage):
```bash
export PUTIO_CRITICAL_THRESHOLD_GB=4   # Less free space required
export PUTIO_COMFORT_THRESHOLD_GB=8   # Fewer active files allowed
```

**Large Account Setup** (for 500GB+ accounts):
```bash
export PUTIO_CRITICAL_THRESHOLD_GB=10  # Higher critical threshold
export PUTIO_COMFORT_THRESHOLD_GB=50  # More space for active files
export PUTIO_DELETABLE_FOLDERS=movies,downloads,4k  # Manage more folders
```

This makes it easy to configure the script via GitHub Actions environment variables without changing the code.

## License

MIT
