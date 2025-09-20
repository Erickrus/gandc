#!/usr/bin/env python3

import os
import zipfile
import json
import argparse
from pathlib import Path
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def is_valid_symlink(link_path):
    """Check if a path is a symbolic link."""
    path = Path(link_path)
    is_sym = path.is_symlink()
    logger.debug(f"Checking {link_path}: is_symlink={is_sym}")
    return is_sym

def get_relative_link_target(link_path, source_dir):
    """Get the raw link target, preserving relative paths if possible."""
    link_path = Path(link_path)
    source_dir = Path(source_dir).resolve()
    try:
        target = os.readlink(link_path)
        logger.debug(f"Raw link target for {link_path}: {target}")
        # If the target is already relative, use it as is
        if not os.path.isabs(target):
            return target
        # Otherwise, convert absolute target to relative
        target_path = Path(link_path.parent, target).resolve()
        if not target_path.exists():
            logger.warning(f"Link target {target_path} does not exist (broken link)")
        relative_target = os.path.relpath(target_path, source_dir)
        logger.debug(f"Relative link target for {link_path}: {relative_target}")
        return relative_target
    except OSError as e:
        logger.warning(f"Failed to read link {link_path}: {e}")
        return None

def package_directory(source_dir, target_zip):
    """Package a directory into a zip file, storing symbolic links in links.json."""
    source_dir = Path(source_dir).resolve()
    target_zip = Path(target_zip).resolve()
    
    # Collect symbolic link information
    links = {}
    logger.info(f"Scanning {source_dir} for files and links")
    
    for root, dirs, files in os.walk(source_dir, followlinks=False):
        root_path = Path(root)
        for name in files + dirs:
            item_path = root_path / name
            rel_path = str(item_path.relative_to(source_dir))
            
            if is_valid_symlink(item_path):
                link_target = get_relative_link_target(item_path, source_dir)
                if link_target:
                    links[rel_path] = link_target
                    logger.info(f"Found symlink: {rel_path} -> {link_target}")
            elif item_path.is_file():
                logger.debug(f"Found file: {rel_path}")
            else:
                logger.debug(f"Found directory: {rel_path}")
    
    # Write links to a temporary JSON file
    links_json_path = source_dir / 'links.json'
    with open(links_json_path, 'w') as f:
        json.dump(links, f, indent=2)
    logger.info(f"Saved link information to {links_json_path}")
    
    # Create zip file
    logger.info(f"Packaging {source_dir} to {target_zip}")
    with zipfile.ZipFile(target_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Add links.json
        zf.write(links_json_path, arcname='links.json')
        
        # Add all files (excluding symlinks)
        for root, _, files in os.walk(source_dir, followlinks=False):
            for file in files:
                file_path = Path(root) / file
                if not is_valid_symlink(file_path):
                    rel_path = str(file_path.relative_to(source_dir))  # Changed from source_dir.parent
                    logger.debug(f"Adding file to zip: {rel_path}")
                    zf.write(file_path, arcname=rel_path)
    
    # Clean up temporary links.json
    links_json_path.unlink()
    logger.info("Packaging completed successfully.")

def unpackage_directory(source_zip, target_dir):
    """Unpackage a zip file to a target directory, restoring symbolic links from links.json."""
    source_zip = Path(source_zip).resolve()
    target_dir = Path(target_dir).resolve()
    
    logger.info(f"Unpackaging {source_zip} to {target_dir}")
    
    # Create target directory if it doesn't exist
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Extract all files except links.json
    with zipfile.ZipFile(source_zip, 'r') as zf:
        # Extract links.json first
        links_json_data = zf.read('links.json')
        links = json.loads(links_json_data)
        
        # Extract all other files
        for member in zf.namelist():
            if member != 'links.json':
                logger.debug(f"Extracting: {member}")
                zf.extract(member, target_dir)
    
    # Remove links.json from target directory
    links_json_path = target_dir / 'links.json'
    if links_json_path.exists():
        links_json_path.unlink()
        logger.debug(f"Removed {links_json_path}")
    
    # Restore symbolic links
    logger.info("Restoring symbolic links")
    for link_path, link_target in links.items():
        target_path = target_dir / link_path
        target_link_target = link_target  # Use raw target (relative or absolute)
        
        # Ensure parent directory exists
        target_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Remove existing file or link if it exists
        if target_path.exists() or target_path.is_symlink():
            target_path.unlink()
        
        # Create symbolic link
        try:
            os.symlink(target_link_target, target_path)
            logger.info(f"Restored symlink: {link_path} -> {target_link_target}")
        except OSError as e:
            logger.error(f"Failed to restore symlink {link_path} -> {target_link_target}: {e}")
    
    logger.info("Unpackaging completed successfully.")

def main():
    parser = argparse.ArgumentParser(description="Package and unpackage directories with symbolic link support.")
    parser.add_argument('--source', required=True, help='Source directory (to package) or zip file (to unpackage)')
    parser.add_argument('--target', required=True, help='Target zip file (for packaging) or directory (for unpackaging)')
    
    args = parser.parse_args()
    
    source_path = Path(args.source)
    target_path = Path(args.target)
    
    try:
        if source_path.is_dir() and target_path.suffix == '.zip':
            package_directory(args.source, args.target)
        elif source_path.suffix == '.zip' and (target_path.is_dir() or not target_path.exists()):
            unpackage_directory(args.source, args.target)
        else:
            logger.error("Invalid arguments: --source must be a directory and --target a .zip file for packaging, or --source a .zip file and --target a directory for unpackaging.")
            exit(1)
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        exit(1)

if __name__ == "__main__":
    main()
