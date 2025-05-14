#!/usr/bin/env python3
"""
put.io Storage Manager

This script manages storage on put.io by automatically deleting oldest
video files from designated folders when available space falls below a threshold.
It can also clean up old files from trash if needed.

Usage:
    python put_io_manager.py [--dry-run] [--threshold THRESHOLD_GB]
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple, Union

import putiopy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Constants (configurable via environment variables)
DEFAULT_SPACE_THRESHOLD_GB = float(os.environ.get("PUTIO_SPACE_THRESHOLD_GB", "10"))
TRASH_CLEANUP_THRESHOLD_GB = float(os.environ.get("PUTIO_TRASH_CLEANUP_THRESHOLD_GB", "0"))  # Threshold for when to clean trash (0 means never clean trash)
TRASH_CLEANUP_TARGET_GB = float(os.environ.get("PUTIO_TRASH_CLEANUP_TARGET_GB", "5"))  # How much space to free up from trash
MIN_TRASH_AGE_DAYS = int(os.environ.get("PUTIO_MIN_TRASH_AGE_DAYS", "2"))  # Minimum age of files in trash to delete (in days)
DELETABLE_FOLDERS = os.environ.get("PUTIO_DELETABLE_FOLDERS", "chill.institute,putfirst").split(",")
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

    def __init__(self, token: str, threshold_gb: float = DEFAULT_SPACE_THRESHOLD_GB, dry_run: bool = False):
        """
        Initialize the put.io storage manager.
        
        Args:
            token: put.io API token
            threshold_gb: Free space threshold in GB
            dry_run: If True, don't actually delete files
        """
        self.client = putiopy.Client(token, use_retry=True)
        self.threshold_bytes = threshold_gb * (1024 ** 3)  # Convert GB to bytes
        self.dry_run = dry_run
        self.root_folder_ids = {}  # Mapping of folder names to IDs
        self.deleted_files = []
        self.bytes_freed = 0
        
    def get_account_info(self) -> Dict:
        """
        Get account information from put.io
        
        Returns:
            Dict containing account information
        """
        logger.info("Getting account information...")
        for attempt in range(MAX_RETRIES):
            try:
                account = self.client.Account.info()
                logger.debug(f"Account info raw response: {account}")
                
                # Access the disk info from the dictionary
                disk_size = account['disk']['size']
                disk_used = account['disk']['used']
                disk_avail = account['disk']['avail']
                
                # Get trash size if available
                trash_size = account.get('trash_size', 0)
                
                logger.info(f"Total disk space: {self._format_size(disk_size)}")
                logger.info(f"Used space: {self._format_size(disk_used)}")
                logger.info(f"Available space: {self._format_size(disk_avail)}")
                logger.info(f"Trash size: {self._format_size(trash_size)}")
                return account
            except Exception as e:
                logger.error(f"Error getting account info (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    raise

    def needs_cleanup(self, account_info, trash_size: int = 0) -> bool:
        """
        Check if cleanup is needed based on available space
        
        Args:
            account_info: Account information from get_account_info()
            trash_size: Size of trash in bytes (optional, will be added to available space)
            
        Returns:
            True if cleanup is needed
        """
        # Calculate effective available space by adding trash size
        effective_available = account_info['disk']['avail'] + trash_size
        logger.debug(f"Effective available space: {self._format_size(effective_available)}")
        
        # Only need cleanup if effective available space is below threshold
        return effective_available < self.threshold_bytes
    
    def find_deletable_folders(self) -> None:
        """Find and store IDs of the folders that are allowed to be cleaned up"""
        logger.info("Finding deletable folders...")
        root_files = self.client.File.list()
        
        # Add debug info to see what attributes are available
        if root_files and len(root_files) > 0:
            sample_file = root_files[0]
            logger.debug(f"Sample file attributes: {dir(sample_file)}")
            logger.debug(f"Sample file __dict__: {sample_file.__dict__}")
        
        for folder_name in DELETABLE_FOLDERS:
            for file in root_files:
                # Check if file is a folder (file_type is "FOLDER" or content_type is "application/x-directory")
                is_folder = False
                if hasattr(file, 'file_type'):
                    is_folder = file.file_type == "FOLDER"
                elif hasattr(file, 'content_type'):
                    is_folder = file.content_type == "application/x-directory"
                
                if file.name == folder_name and is_folder:
                    self.root_folder_ids[folder_name] = file.id
                    logger.info(f"Found deletable folder: {folder_name} (ID: {file.id})")
        
        if not self.root_folder_ids:
            logger.warning(f"None of the specified folders {DELETABLE_FOLDERS} were found!")
    
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
                has_video = any(subfile.is_video or subfile.folder_has_video for subfile in subfiles)
                file_info.folder_has_video = has_video
                
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
            video_files = [f for f in files if f.is_video and f.parent_id == folder_id]
            
            # Find folders that contain videos (to delete as units)
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
        if self.dry_run:
            logger.info(f"[DRY RUN] Would delete: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
            self.deleted_files.append(file_name)
            self.bytes_freed += file_size
            return True
        
        logger.info(f"Deleting: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
        
        for attempt in range(MAX_RETRIES):
            try:
                self.client.request(f"/files/delete", method="POST", data={"file_ids": file_id})
                self.deleted_files.append(file_name)
                self.bytes_freed += file_size
                logger.info(f"Successfully deleted {file_name}, freed {self._format_size(file_size)}")
                return True
            except Exception as e:
                logger.error(f"Error deleting {file_name} (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        
        return False

    def clean_up_space(self, account_info) -> bool:
        """
        Delete oldest files until free space is above threshold
        
        Args:
            account_info: Account information from get_account_info()
            
        Returns:
            True if cleanup was successful
        """
        # Calculate how much space needs to be freed
        needed_space = self.threshold_bytes - account_info['disk']['avail']
        logger.info(f"Need to free {self._format_size(needed_space)} to reach threshold")
        
        # Find deletable folders
        self.find_deletable_folders()
        if not self.root_folder_ids:
            logger.error("No deletable folders found, cannot clean up space.")
            return False
        
        # Collect files to delete
        deletable_items = self.collect_deletable_files()
        logger.info(f"Found {len(deletable_items)} items that can be deleted")
        
        if not deletable_items:
            logger.warning("No deletable files found in the specified folders.")
            return False
        
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
        
        logger.info(f"Freed {self._format_size(freed_space)} of space")
        
        # Return success only if we've freed enough space
        return freed_space >= needed_space
    
    def needs_trash_cleanup(self, account_info) -> bool:
        """
        Check if trash cleanup is needed based on available space
        
        Args:
            account_info: Account information from get_account_info()
            
        Returns:
            True if trash cleanup is needed
        """
        # If trash cleanup threshold is 0, never clean trash
        if TRASH_CLEANUP_THRESHOLD_GB <= 0:
            return False
            
        # Get available space and trash size
        avail_space = account_info['disk']['avail']
        trash_size = account_info.get('trash_size', 0)
        
        # Convert thresholds to bytes
        trash_cleanup_threshold = TRASH_CLEANUP_THRESHOLD_GB * (1024 ** 3)
        
        # Check if available space is below threshold and trash has files
        return avail_space < trash_cleanup_threshold and trash_size > 0
    
    def get_trash_files(self) -> List[Dict]:
        """
        Get list of files in trash
        
        Returns:
            List of file information dictionaries
        """
        logger.info("Getting files in trash...")
        
        for attempt in range(MAX_RETRIES):
            try:
                # Use the API endpoint to list trash
                response = self.client.request("/files/list", params={"trash": "true"})
                
                # Extract files from the response
                if isinstance(response, dict) and "files" in response:
                    trash_files = response["files"]
                else:
                    logger.error(f"Unexpected response format: {response}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY)
                        continue
                    return []
                
                # Debug log the first file to see its structure
                if trash_files and len(trash_files) > 0:
                    logger.debug(f"Sample trash file: {trash_files[0]}")
                
                logger.info(f"Found {len(trash_files)} files in trash")
                return trash_files
            except Exception as e:
                logger.error(f"Error getting trash files (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        
        logger.error("Failed to get trash files after multiple attempts")
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
        if self.dry_run:
            logger.info(f"[DRY RUN] Would permanently delete from trash: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
            self.deleted_files.append(f"Trash: {file_name}")
            self.bytes_freed += file_size
            return True
        
        logger.info(f"Permanently deleting from trash: {file_name} (ID: {file_id}, Size: {self._format_size(file_size)})")
        
        for attempt in range(MAX_RETRIES):
            try:
                # Use the API endpoint to permanently delete from trash
                # Based on our tests, this is the working endpoint
                self.client.request("/files/delete", method="POST", 
                                   data={"file_ids": file_id}, 
                                   params={"trash": "true", "permanently": "true"})
                
                self.deleted_files.append(f"Trash: {file_name}")
                self.bytes_freed += file_size
                logger.info(f"Successfully deleted {file_name} from trash, freed {self._format_size(file_size)}")
                return True
            except Exception as e:
                logger.error(f"Error deleting {file_name} from trash (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        
        return False
    
    def clean_up_trash(self, account_info) -> bool:
        """
        Clean up trash by permanently deleting oldest files
        
        Args:
            account_info: Account information from get_account_info()
            
        Returns:
            True if cleanup was successful
        """
        # Get available space and trash size
        avail_space = account_info['disk']['avail']
        trash_size = account_info.get('trash_size', 0)
        
        # Calculate target space to free (to reach TRASH_CLEANUP_TARGET_GB)
        trash_target_bytes = TRASH_CLEANUP_TARGET_GB * (1024 ** 3)
        needed_space = max(0, trash_target_bytes - avail_space)
        
        logger.info(f"Current available space: {self._format_size(avail_space)}")
        logger.info(f"Current trash size: {self._format_size(trash_size)}")
        logger.info(f"Need to free {self._format_size(needed_space)} from trash to reach target")
        
        # Get files in trash
        trash_files = self.get_trash_files()
        
        if not trash_files:
            logger.warning("No files found in trash.")
            return False
        
        # Calculate the minimum date for files to be eligible for deletion
        import datetime as dt
        min_date = dt.datetime.now() - dt.timedelta(days=MIN_TRASH_AGE_DAYS)
        
        # Filter files that are old enough to delete
        eligible_files = []
        for file in trash_files:
            # Parse the creation date from the file info
            if 'created_at' in file:
                created_at = putiopy.strptime(file['created_at'])
                # Check if file is old enough
                if created_at < min_date:
                    eligible_files.append(file)
        
        logger.info(f"Found {len(eligible_files)} files in trash that are at least {MIN_TRASH_AGE_DAYS} day(s) old")
        
        if not eligible_files:
            logger.warning("No eligible files found in trash to delete.")
            return False
        
        # Sort files by creation date (oldest first)
        eligible_files.sort(key=lambda x: putiopy.strptime(x['created_at']))
        
        # Delete files until we've freed enough space
        freed_space = 0
        for file in eligible_files:
            if freed_space >= needed_space:
                break
            
            file_id = file['id']
            file_name = file['name']
            file_size = file['size']
            
            if self.permanently_delete_from_trash(file_id, file_name, file_size):
                freed_space += file_size
        
        logger.info(f"Freed {self._format_size(freed_space)} from trash")
        
        # Return success only if we've freed enough space
        return freed_space >= needed_space
    
    def run(self) -> None:
        """Run the storage manager to check and clean up space if needed"""
        try:
            # Get account info
            account_info = self.get_account_info()
            
            # Get trash size from account info
            trash_size = account_info.get('trash_size', 0)  
            
            # Calculate effective available space (including trash)
            effective_avail = account_info['disk']['avail'] + trash_size
            logger.info(f"Effective available space (including trash): {self._format_size(effective_avail)}")
            
            # Check if trash cleanup is needed first (less than 5GB free and trash has files)
            if self.needs_trash_cleanup(account_info):
                logger.info(f"Available space ({self._format_size(account_info['disk']['avail'])}) is below trash cleanup threshold ({self._format_size(TRASH_CLEANUP_THRESHOLD_GB * (1024 ** 3))}), attempting to clean trash")
                trash_success = self.clean_up_trash(account_info)
                
                if trash_success and not self.dry_run:
                    # Get updated account info after trash cleanup
                    account_info = self.get_account_info()
                    logger.info(f"Updated free space after trash cleanup: {self._format_size(account_info['disk']['avail'])}")
            
            # Check if cleanup is still needed based on effective available space 
            # (we include trash in calculation since it could be freed)
            trash_size = account_info.get('trash_size', 0)
            if not self.needs_cleanup(account_info, trash_size):
                logger.info(f"Effective free space ({self._format_size(account_info['disk']['avail'] + trash_size)}) is above threshold ({self._format_size(self.threshold_bytes)}), no cleanup needed.")
                
                # Print summary of any trash deletions
                if self.deleted_files:
                    logger.info(f"Deleted {len(self.deleted_files)} files/folders, freed {self._format_size(self.bytes_freed)}")
                    for file in self.deleted_files:
                        logger.info(f"  - {file}")
                
                return
            
            # Clean up space from regular files if still needed
            success = self.clean_up_space(account_info)
            
            # Print summary
            if self.deleted_files:
                logger.info(f"Deleted {len(self.deleted_files)} files/folders, freed {self._format_size(self.bytes_freed)}")
                for file in self.deleted_files:
                    logger.info(f"  - {file}")
            
            # Get updated account info if not in dry run
            if not self.dry_run:
                updated_account = self.get_account_info()
                logger.info(f"Updated free space: {self._format_size(updated_account['disk']['avail'])}")
            
            if not success:
                logger.warning("Could not free enough space to reach threshold")
                if not self.dry_run:
                    sys.exit(1)
        
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


def test_trash_api(token):
    """Test trash-related API endpoints"""
    logger.info("*** TESTING TRASH API ENDPOINTS ***")
    
    client = putiopy.Client(token, use_retry=True)
    
    # Test listing trash
    logger.info("Testing listing files in trash...")
    try:
        # Test with trash=true parameter
        response = client.request("/files/list", params={"trash": "true"})
        if isinstance(response, dict):
            logger.info(f"Response keys: {list(response.keys())}")
            files = response.get("files", [])
            logger.info(f"Found {len(files)} files in trash")
        
        # Print sample file info if available
        if files and len(files) > 0:
            sample_file = files[0]
            logger.info(f"Sample trash file: {sample_file}")
            logger.info(f"Sample file keys: {list(sample_file.keys())}")
            
            # Check if file has required attributes for our implementation
            for key in ['id', 'name', 'size', 'created_at']:
                if key in sample_file:
                    logger.info(f"  - {key}: {sample_file[key]}")
                else:
                    logger.warning(f"  - Missing key: {key}")
    except Exception as e:
        logger.error(f"Error listing trash: {e}")
    
    # Test trash endpoints
    logger.info("Testing trash-related endpoints...")
    
    # Test candidate trash deletion endpoints
    possible_endpoints = [
        ("/files/delete/permanent", "POST"),
        ("/files/trash/delete", "POST"),
        ("/files/trash/empty", "POST"),
        ("/trash/delete", "POST"),
        ("/trash/empty", "POST"),
        ("/files/delete", "POST", {"trash": "true", "permanently": "true"}),
    ]
    
    for endpoint_info in possible_endpoints:
        endpoint = endpoint_info[0]
        method = endpoint_info[1]
        params = endpoint_info[2] if len(endpoint_info) > 2 else {}
        
        logger.info(f"Testing endpoint: {method} {endpoint} {params}")
        try:
            # Use a non-existent file ID to avoid actually deleting anything
            data = {"file_ids": "-1"}
            
            # Make the request
            test_response = client.request(endpoint, method=method, data=data, params=params, raw=True)
            logger.info(f"Response status: {test_response.status_code}")
            logger.info(f"Response: {test_response.text}")
        except Exception as e:
            logger.error(f"Error testing endpoint {endpoint}: {e}")
            
    # Test if we can restore from trash (this functionality might be useful in future)
    logger.info("Testing trash restore endpoint...")
    try:
        test_response = client.request("/files/restore", method="POST", data={"file_ids": "-1"}, raw=True)
        logger.info(f"Restore endpoint status: {test_response.status_code}")
        logger.info(f"Response: {test_response.text}")
    except Exception as e:
        logger.error(f"Error testing restore endpoint: {e}")
    
    logger.info("*** TRASH API TEST COMPLETE ***")

def main():
    """Main function to parse arguments and run the storage manager"""
    parser = argparse.ArgumentParser(description="Manage put.io storage by deleting oldest files when space is low")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually delete files, just log what would happen")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SPACE_THRESHOLD_GB, 
                      help=f"Free space threshold in GB (default: {DEFAULT_SPACE_THRESHOLD_GB})")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--test-trash-api", action="store_true", help="Test trash API endpoints")
    args = parser.parse_args()
    
    # Set logging level
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    # Get API token from environment variable
    token = os.environ.get("PUTIO_TOKEN")
    if not token:
        logger.error("PUTIO_TOKEN environment variable is not set")
        sys.exit(1)
    
    # Test trash API if requested
    if args.test_trash_api:
        test_trash_api(token)
        return
    
    # Determine if we should use dry run mode
    # Command line argument overrides environment variable
    dry_run = args.dry_run
    if not dry_run and os.environ.get("PUTIO_DRY_RUN", "").lower() in ("true", "1", "yes"):
        dry_run = True
    
    # Get threshold from command line or environment variable
    # Command line argument overrides environment variable
    threshold = args.threshold
    
    # Log startup information
    logger.info(f"Starting put.io storage manager")
    logger.info(f"Threshold: {threshold} GB")
    logger.info(f"Dry run: {dry_run}")
    logger.info(f"Deletable folders: {', '.join(DELETABLE_FOLDERS)}")
    
    # Create and run the storage manager
    manager = PutioStorageManager(token, threshold, dry_run)
    manager.run()


if __name__ == "__main__":
    main()
