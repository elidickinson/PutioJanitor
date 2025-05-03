#!/usr/bin/env python3
"""
put.io Storage Manager

This script manages storage on put.io by automatically deleting oldest
video files from designated folders when available space falls below a threshold.

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

# Constants
DEFAULT_SPACE_THRESHOLD_GB = 10
DELETABLE_FOLDERS = ["chill.institute", "putfirst"]
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


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
                
                logger.info(f"Total disk space: {self._format_size(disk_size)}")
                logger.info(f"Used space: {self._format_size(disk_used)}")
                logger.info(f"Available space: {self._format_size(disk_avail)}")
                return account
            except Exception as e:
                logger.error(f"Error getting account info (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    raise

    def needs_cleanup(self, account_info) -> bool:
        """
        Check if cleanup is needed based on available space
        
        Args:
            account_info: Account information from get_account_info()
            
        Returns:
            True if cleanup is needed
        """
        return account_info['disk']['avail'] < self.threshold_bytes
    
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
    
    def run(self) -> None:
        """Run the storage manager to check and clean up space if needed"""
        try:
            # Get account info
            account_info = self.get_account_info()
            
            # Check if cleanup is needed
            if not self.needs_cleanup(account_info):
                logger.info(f"Free space ({self._format_size(account_info['disk']['avail'])}) is above threshold ({self._format_size(self.threshold_bytes)}), no cleanup needed.")
                return
            
            # Clean up space
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
    def _format_size(bytes_size: int) -> str:
        """Format byte size to human-readable string"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_size < 1024 or unit == 'TB':
                return f"{bytes_size:.2f} {unit}"
            bytes_size /= 1024


def main():
    """Main function to parse arguments and run the storage manager"""
    parser = argparse.ArgumentParser(description="Manage put.io storage by deleting oldest files when space is low")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually delete files, just log what would happen")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SPACE_THRESHOLD_GB, 
                      help=f"Free space threshold in GB (default: {DEFAULT_SPACE_THRESHOLD_GB})")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    
    # Set logging level
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    # Get API token from environment variable
    token = os.environ.get("PUTIO_TOKEN")
    if not token:
        logger.error("PUTIO_TOKEN environment variable is not set")
        sys.exit(1)
    
    # Log startup information
    logger.info(f"Starting put.io storage manager")
    logger.info(f"Threshold: {args.threshold} GB")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info(f"Deletable folders: {', '.join(DELETABLE_FOLDERS)}")
    
    # Create and run the storage manager
    manager = PutioStorageManager(token, args.threshold, args.dry_run)
    manager.run()


if __name__ == "__main__":
    main()
