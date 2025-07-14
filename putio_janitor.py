#!/usr/bin/env python3
"""
put.io Storage Manager

This script manages storage on put.io by automatically deleting oldest
video files from designated folders when available space falls below a threshold.
It can also clean up old files from trash if needed.

Usage:
    python putio_janitor.py [--dry-run]
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Union

import putiopy

# Try to load .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Constants (configurable via environment variables)
CRITICAL_THRESHOLD_GB = float(os.environ.get("PUTIO_CRITICAL_THRESHOLD_GB", "6"))  # Must have this much free (includes trash)
COMFORT_THRESHOLD_GB = float(os.environ.get("PUTIO_COMFORT_THRESHOLD_GB", "10"))  # Target free space (excludes trash)
MIN_TRASH_AGE_DAYS = int(os.environ.get("PUTIO_MIN_TRASH_AGE_DAYS", "2"))  # Minimum age of files in trash to delete (in days)
DELETABLE_FOLDERS = [f.strip() for f in os.environ.get("PUTIO_DELETABLE_FOLDERS", "chill.institute,putfirst").split(",") if f.strip()]
MAX_RETRIES = int(os.environ.get("PUTIO_MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("PUTIO_RETRY_DELAY", "5"))  # seconds


@dataclass
class FileInfo:
    """Information about a file or folder in put.io"""
    id: int
    name: str
    size: int
    created_at: datetime
    is_folder: bool
    parent_id: int
    is_video: bool = False
    folder_has_video: bool = False


class PutioStorageManager:
    """Manages put.io storage by deleting oldest files when space is low"""

    def __init__(self, token: str, dry_run: bool = False):
        """
        Initialize the put.io storage manager.
        
        Args:
            token: put.io API token
            dry_run: If True, don't actually delete files
        """
        self.client = putiopy.Client(token, use_retry=True)
        self.dry_run = dry_run
        self.root_folder_ids = {}  # Mapping of folder names to IDs
        self.deleted_files = []
        self.bytes_freed = 0
        self.gb_to_bytes = lambda gb: gb * (1024 ** 3)
        
    def get_account_info(self) -> Dict:
        """
        Get account information from put.io
        
        Returns:
            Dict containing account information
        """
        logger.info("Getting account information...")
        try:
            account = self.client.Account.info()
            logger.debug(f"Account info raw response: {account}")
            
            # Log disk and trash information
            disk = account['disk']
            trash_size = account.get('trash_size', 0)
            
            logger.info(f"Total disk space: {self._format_size(disk['size'])}")
            logger.info(f"Used space: {self._format_size(disk['used'])}")
            logger.info(f"Available space: {self._format_size(disk['avail'])}")
            logger.info(f"Trash size: {self._format_size(trash_size)}")
            
            return account
        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            raise

    def get_cleanup_status(self, account_info) -> Tuple[bool, bool]:
        """
        Determine if cleanup is needed based on dual thresholds
        
        Args:
            account_info: Account information from get_account_info()
            
        Returns:
            Tuple of (need_trash_cleanup, need_file_cleanup)
        """
        avail = account_info['disk']['avail']
        trash_size = account_info.get('trash_size', 0)
        
        # Critical: Must have 6GB free including trash
        if avail < self.gb_to_bytes(CRITICAL_THRESHOLD_GB):
            return (True, False)  # Clean trash first
        
        # Comfort: Want 10GB free excluding trash
        if avail < self.gb_to_bytes(COMFORT_THRESHOLD_GB):
            return (False, True)  # Clean files first
            
        return (False, False)  # No cleanup needed
    
    def find_deletable_folders(self) -> None:
        """Find and store IDs of the folders that are allowed to be cleaned up"""
        logger.info("Finding deletable folders...")
        try:
            root_files = self.client.File.list()
            
            # Add debug info to see what attributes are available
            if root_files and len(root_files) > 0:
                logger.debug(f"Sample file attributes: {dir(root_files[0])}")
            
            for folder_name in DELETABLE_FOLDERS:
                for file in root_files:
                    is_folder = (hasattr(file, 'file_type') and file.file_type == "FOLDER") or \
                               (hasattr(file, 'content_type') and file.content_type == "application/x-directory")
                    
                    if file.name == folder_name and is_folder:
                        self.root_folder_ids[folder_name] = file.id
                        logger.info(f"Found deletable folder: {folder_name} (ID: {file.id})")
            
            if not self.root_folder_ids:
                logger.warning(f"None of the specified folders {DELETABLE_FOLDERS} were found!")
        except Exception as e:
            logger.error(f"Error finding deletable folders: {e}")
            raise
    
    def get_files_in_folder(self, folder_id: int, parent_path: str = "") -> List[FileInfo]:
        """
        Get all files and folders in a folder, recursively.
        
        Args:
            folder_id: ID of the folder to scan
            parent_path: Path of the parent folder for logging
            
        Returns:
            List of FileInfo objects
        """
        logger.debug(f"Scanning folder ID: {folder_id} ({parent_path})")
        
        try:
            files = self.client.File.list(parent_id=folder_id)
        except Exception as e:
            logger.error(f"Error listing files in folder {folder_id}: {e}")
            return []
        
        result = []
        
        for file in files:
            current_path = f"{parent_path}/{file.name}" if parent_path else file.name
            file_info = FileInfo(
                id=file.id,
                name=file.name,
                size=file.size,
                created_at=file.created_at,
                is_folder=file.file_type == "FOLDER",
                parent_id=folder_id,
                is_video=file.file_type == "VIDEO"
            )
            
            if file_info.is_folder:
                # Recursively scan subfolders
                subfiles = self.get_files_in_folder(file.id, current_path)
                
                # If the folder or any subfolder contains a video, mark it
                file_info.folder_has_video = any(
                    subfile.is_video or subfile.folder_has_video for subfile in subfiles
                )
                
                # Add all subfiles to the result
                result.extend(subfiles)
            
            result.append(file_info)
            
        return result
    
    def collect_deletable_files(self) -> List[Tuple[FileInfo, List[FileInfo]]]:
        """
        Collect all deletable files and folders.
        
        Returns:
            List of tuples containing (folder, contained_files)
            where folder is None for individual files
        """
        logger.info("Collecting deletable files...")
        all_deletable_files = []
        
        # Process each target folder
        for folder_name, folder_id in self.root_folder_ids.items():
            logger.info(f"Scanning folder: {folder_name}")
            
            # Get all files in the folder
            files = self.get_files_in_folder(folder_id, folder_name)
            
            # Find individual video files (not in a subfolder with other videos)
            # Only include files that are direct children of this root folder
            video_files = [f for f in files if f.is_video and f.parent_id == folder_id]
            
            # Find folders that contain videos (to delete as units)
            # Only include folders that are direct children of this root folder
            video_folders = [f for f in files if f.is_folder and f.folder_has_video and f.parent_id == folder_id]
            
            # For individual video files, add them with None as the container
            for video_file in video_files:
                all_deletable_files.append((None, [video_file]))
            
            # For folders with videos, find all files inside them
            for video_folder in video_folders:
                folder_files = [f for f in files if f.parent_id == video_folder.id]
                all_deletable_files.append((video_folder, folder_files))
            
            logger.info(f"Found {len(video_files)} individual video files and {len(video_folders)} video folders in {folder_name}")
        
        # Sort all deletable files by creation date
        all_deletable_files.sort(
            key=lambda x: x[0].created_at if x[0] else x[1][0].created_at
        )
        
        return all_deletable_files

    def delete_file(self, file_id: int, file_name: str, file_size: int) -> bool:
        """
        Delete a file or folder from put.io
        
        Args:
            file_id: ID of the file/folder to delete
            file_name: Name of the file/folder for logging
            file_size: Size of the file for tracking freed space
            
        Returns:
            True if the file was deleted successfully
        """
        # Safety check: Never delete folders with names matching deletable folder names
        base_name = file_name.split(':')[1].strip() if 'Folder:' in file_name else file_name
        if base_name in DELETABLE_FOLDERS:
            logger.error(f"SAFETY: Attempted to delete protected folder {file_name} - BLOCKED")
            return False
            
        if self.dry_run:
            logger.info(f"[DRY RUN] Would delete: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
            self.deleted_files.append(file_name)
            self.bytes_freed += file_size
            return True
        
        logger.info(f"Deleting: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
        try:
            self.client.request("/files/delete", method="POST", data={"file_ids": file_id})
            self.deleted_files.append(file_name)
            self.bytes_freed += file_size
            logger.info(f"Successfully deleted {file_name}, freed {self._format_size(file_size)}")
            return True
        except Exception as e:
            logger.error(f"Error deleting {file_name}: {e}")
            return False

    def clean_up_space(self, account_info) -> int:
        """
        Delete oldest files to reach comfort threshold
        
        Args:
            account_info: Account information from get_account_info()
            
        Returns:
            Bytes freed
        """
        # Calculate how much space needs to be freed to reach comfort threshold
        needed_space = self.gb_to_bytes(COMFORT_THRESHOLD_GB) - account_info['disk']['avail']
        logger.info(f"Need to free {self._format_size(needed_space)} to reach comfort threshold")
        
        # Find deletable folders
        self.find_deletable_folders()
        if not self.root_folder_ids:
            logger.error("No deletable folders found, cannot clean up space.")
            return 0
        
        # Collect files to delete
        deletable_items = self.collect_deletable_files()
        logger.info(f"Found {len(deletable_items)} items that can be deleted")
        
        if not deletable_items:
            logger.warning("No deletable files found in the specified folders.")
            return 0
        
        # Delete files/folders until we've freed enough space
        freed_space = 0
        for container, files in deletable_items:
            if freed_space >= needed_space:
                break
                
            if container:  # This is a folder with video
                total_size = sum(f.size for f in files)
                if self.delete_file(container.id, f"Folder: {container.name}", total_size):
                    freed_space += total_size
            else:  # This is an individual video file
                video_file = files[0]
                if self.delete_file(video_file.id, video_file.name, video_file.size):
                    freed_space += video_file.size
        
        logger.info(f"Freed {self._format_size(freed_space)} from files")
        return freed_space
    
    
    def get_trash_files(self) -> List[Dict]:
        """
        Get list of files in trash
        
        Returns:
            List of file information dictionaries
        """
        logger.info("Getting files in trash...")
        
        try:
            trash_files = self.client.Account.list_trash()
            
            # Debug log the first file to see its structure
            if trash_files and len(trash_files) > 0:
                logger.debug(f"Sample trash file: {trash_files[0]}")
            
            logger.info(f"Found {len(trash_files)} files in trash")
            return trash_files
        except Exception as e:
            logger.error(f"Error getting trash files: {e}")
            return []
    
    def permanently_delete_from_trash(self, file_id: int, file_name: str, file_size: int) -> bool:
        """
        Permanently delete a file from trash
        
        Args:
            file_id: ID of the file to delete
            file_name: Name of the file for logging
            file_size: Size of the file
            
        Returns:
            True if the file was deleted successfully
        """
        # Safety check: Never delete items with names matching deletable folder names
        if file_name in DELETABLE_FOLDERS:
            logger.error(f"SAFETY: Attempted to delete protected folder {file_name} from trash - BLOCKED")
            return False
            
        if self.dry_run:
            logger.info(f"[DRY RUN] Would permanently delete from trash: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
            self.deleted_files.append(f"Trash: {file_name}")
            self.bytes_freed += file_size
            return True
        
        logger.info(f"Permanently deleting from trash: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
        try:
            self.client.Account.delete_from_trash(file_id)
            self.deleted_files.append(f"Trash: {file_name}")
            self.bytes_freed += file_size
            logger.info(f"Successfully deleted {file_name} from trash, freed {self._format_size(file_size)}")
            return True
        except Exception as e:
            logger.error(f"Error deleting {file_name} from trash: {e}")
            return False
    
    def clean_up_trash(self, account_info) -> int:
        """
        Clean trash to reach critical threshold
        
        Args:
            account_info: Account information from get_account_info()
            
        Returns:
            Bytes freed
        """
        avail_space = account_info['disk']['avail']
        trash_size = account_info.get('trash_size', 0)
        
        # Need to reach critical threshold
        needed_space = self.gb_to_bytes(CRITICAL_THRESHOLD_GB) - avail_space
        
        logger.info(f"Current available space: {self._format_size(avail_space)}")
        logger.info(f"Current trash size: {self._format_size(trash_size)}")
        logger.info(f"Need to free {self._format_size(needed_space)} from trash to reach critical threshold")
        
        # Get files in trash
        trash_files = self.get_trash_files()
        
        if not trash_files:
            logger.warning("No files found in trash.")
            return 0
        
        # Calculate the minimum date for files to be eligible for deletion
        min_date = datetime.now() - timedelta(days=MIN_TRASH_AGE_DAYS)
        
        # Filter files that are old enough to delete
        eligible_files = [
            file for file in trash_files 
            if 'created_at' in file and putiopy.strptime(file['created_at']) < min_date
        ]
        
        logger.info(f"Found {len(eligible_files)} files in trash that are at least {MIN_TRASH_AGE_DAYS} day(s) old")
        
        if not eligible_files:
            logger.warning("No eligible files found in trash to delete.")
            return 0
        
        # Sort files by creation date (oldest first)
        eligible_files.sort(key=lambda x: putiopy.strptime(x['created_at']))
        
        # Delete files until we've freed enough space
        freed_space = 0
        for file in eligible_files:
            if freed_space >= needed_space:
                break
            
            if self.permanently_delete_from_trash(file['id'], file['name'], file['size']):
                freed_space += file['size']
        
        logger.info(f"Freed {self._format_size(freed_space)} from trash")
        return freed_space
    
    def run(self) -> None:
        """Run the storage manager to check and clean up space if needed"""
        try:
            # Get account info
            account_info = self.get_account_info()
            
            # Determine what cleanup is needed
            need_trash, need_files = self.get_cleanup_status(account_info)
            
            if not need_trash and not need_files:
                logger.info("No cleanup needed - sufficient free space available")
                return
            
            # Handle critical situation - clean trash first
            if need_trash:
                logger.info(f"Critical: Available space below {CRITICAL_THRESHOLD_GB}GB, cleaning trash first")
                trash_freed = self.clean_up_trash(account_info)
                
                if not self.dry_run and trash_freed > 0:
                    # Refresh account info
                    account_info = self.get_account_info()
                    need_trash, need_files = self.get_cleanup_status(account_info)
            
            # Clean files if still needed
            if need_files:
                logger.info("Below comfort threshold (10GB), cleaning files")
                self.clean_up_space(account_info)
            
            # Print summary
            if self.deleted_files:
                logger.info(f"Deleted {len(self.deleted_files)} files/folders, freed {self._format_size(self.bytes_freed)}")
                for file in self.deleted_files:
                    logger.info(f"  - {file}")
            
            # Get final status if not in dry run
            if not self.dry_run and self.deleted_files:
                final_account = self.get_account_info()
                logger.info(f"Final free space: {self._format_size(final_account['disk']['avail'])}")
        
        except Exception as e:
            logger.error(f"Error running storage manager: {e}", exc_info=True)
            sys.exit(1)
    
    @staticmethod
    def _format_size(bytes_size: Union[int, float]) -> str:
        """Format byte size to human-readable string"""
        size = float(bytes_size)  # Convert to float for division
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024 or unit == 'TB':
                return f"{size:.2f} {unit}"
            size /= 1024
        # This should never happen with the TB fallback, but needed for LSP
        return f"{size:.2f} PB"



def main():
    """Main function to parse arguments and run the storage manager"""
    parser = argparse.ArgumentParser(description="Manage put.io storage by deleting oldest files when space is low")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually delete files, just log what would happen")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    
    # Set logging level
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    # Get API token from environment variable
    token = os.environ.get("PUTIO_TOKEN", "").strip()
    if not token:
        logger.error("ERROR: PUTIO_TOKEN environment variable is not set or is empty")
        logger.error("Please set your put.io API token:")
        logger.error("  export PUTIO_TOKEN=your_api_token_here")
        logger.error("Or create a .env file with:")
        logger.error("  PUTIO_TOKEN=your_api_token_here")
        sys.exit(1)
    
    # Determine if we should use dry run mode (CLI arg overrides env var)
    dry_run = args.dry_run or os.environ.get("PUTIO_DRY_RUN", "").lower() in ("true", "1", "yes")
    
    # Validate configuration (only check logic, not existence since they have defaults)
    if CRITICAL_THRESHOLD_GB <= 0:
        logger.error("ERROR: Critical threshold must be greater than 0")
        logger.error(f"  Current value: {CRITICAL_THRESHOLD_GB} GB")
        sys.exit(1)
    
    if COMFORT_THRESHOLD_GB <= CRITICAL_THRESHOLD_GB:
        logger.error("ERROR: Comfort threshold must be greater than critical threshold")
        logger.error(f"  Critical threshold: {CRITICAL_THRESHOLD_GB} GB")
        logger.error(f"  Comfort threshold: {COMFORT_THRESHOLD_GB} GB")
        sys.exit(1)
    
    if not DELETABLE_FOLDERS:
        logger.warning("WARNING: No deletable folders specified, using defaults may not work")
        logger.warning("Consider setting PUTIO_DELETABLE_FOLDERS to folders that exist in your account")
    
    # Log startup information
    logger.info(f"Starting put.io storage manager")
    logger.info(f"Critical threshold: {CRITICAL_THRESHOLD_GB} GB (must have this much free)")
    logger.info(f"Comfort threshold: {COMFORT_THRESHOLD_GB} GB (target free space)")
    logger.info(f"Dry run: {dry_run}")
    logger.info(f"Deletable folders: {', '.join(DELETABLE_FOLDERS)}")
    
    # Create and run the storage manager
    manager = PutioStorageManager(token, dry_run)
    manager.run()


if __name__ == "__main__":
    main()
