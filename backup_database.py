#!/usr/bin/env python3
"""
Database Backup Script for MeshCore Bot
Creates timestamped backups of all SQLite database files
"""

import sqlite3
import shutil
import os
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path


def backup_database(db_path, backup_dir="backups"):
    """
    Create a timestamped backup of a SQLite database
    
    Args:
        db_path: Path to the database file
        backup_dir: Directory to store backups (default: backups)
    
    Returns:
        Path to the backup file if successful, None otherwise
    """
    db_path = Path(db_path)
    
    if not db_path.exists():
        print(f"Warning: Database file {db_path} does not exist, skipping...")
        return None
    
    # Create backup directory if it doesn't exist
    backup_path = Path(backup_dir)
    backup_path.mkdir(exist_ok=True)
    
    # Generate timestamp string
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Create backup filename with timestamp
    db_name = db_path.stem
    backup_filename = f"{db_name}_{timestamp}.db"
    backup_file = backup_path / backup_filename
    
    try:
        # Use SQLite backup API for proper backup (handles WAL files correctly)
        with closing(sqlite3.connect(str(db_path))) as source_conn, closing(sqlite3.connect(str(backup_file))) as backup_conn:
            # Perform the backup
            source_conn.backup(backup_conn)
        
        # Get file size for reporting
        file_size = backup_file.stat().st_size
        file_size_mb = file_size / (1024 * 1024)
        
        print(f"✓ Backed up {db_path.name} -> {backup_file.name} ({file_size_mb:.2f} MB)")
        return str(backup_file)
        
    except sqlite3.Error as e:
        print(f"✗ Error backing up {db_path.name}: {e}")
        # Clean up failed backup file
        if backup_file.exists():
            backup_file.unlink()
        return None
    except Exception as e:
        print(f"✗ Unexpected error backing up {db_path.name}: {e}")
        if backup_file.exists():
            backup_file.unlink()
        return None


def check_database_status(db_path):
    """
    Check if a database file is actually used (has tables)
    
    Args:
        db_path: Path to the database file
    
    Returns:
        Tuple of (is_used, table_count, file_size_mb)
    """
    db_path = Path(db_path)
    
    if not db_path.exists():
        return (False, 0, 0.0)
    
    file_size_mb = db_path.stat().st_size / (1024 * 1024)
    
    try:
        conn = sqlite3.connect(str(db_path))
        with closing(conn):
            cursor = conn.cursor()
            
            # Get list of tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            table_count = len(tables)
        
        # Consider database used if it has tables or is larger than 1KB
        is_used = table_count > 0 or file_size_mb > 0.001
        
        return (is_used, table_count, file_size_mb)
    except sqlite3.Error:
        return (False, 0, file_size_mb)


def find_database_files(root_dir=".", check_usage=True):
    """
    Find all SQLite database files in the root directory
    
    Args:
        root_dir: Root directory to search (default: current directory)
        check_usage: If True, check which databases are actually used
    
    Returns:
        List of database file paths, optionally sorted by priority (main database first)
    """
    root = Path(root_dir)
    db_files = []
    
    # Find all .db files in root directory (not in subdirectories)
    for db_file in root.glob("*.db"):
        # Skip WAL and SHM files (they're not standalone databases)
        if db_file.suffix == ".db":
            db_files.append(db_file)
    
    # Sort by priority: meshcore_bot.db first (main database), then others
    def sort_key(db_file):
        name = db_file.name.lower()
        if name == "meshcore_bot.db":
            return (0, name)
        elif name == "bot_data.db":
            return (1, name)
        else:
            return (2, name)
    
    return sorted(db_files, key=sort_key)


def main():
    """Main backup function"""
    # Get script directory (project root)
    script_dir = Path(__file__).parent.absolute()
    os.chdir(script_dir)
    
    print("MeshCore Bot Database Backup")
    print("=" * 50)
    print(f"Working directory: {script_dir}")
    print()
    
    # Find all database files
    db_files = find_database_files(script_dir)
    
    if not db_files:
        print("No database files found in the project root.")
        return 1
    
    print(f"Found {len(db_files)} database file(s):")
    
    # Check which databases are actually used
    active_databases = []
    inactive_databases = []
    
    for db_file in db_files:
        is_used, table_count, file_size = check_database_status(db_file)
        status = "✓ ACTIVE" if is_used else "○ EMPTY/UNUSED"
        print(f"  {status}: {db_file.name} ({table_count} tables, {file_size:.2f} MB)")
        
        if is_used:
            active_databases.append(db_file)
        else:
            inactive_databases.append(db_file)
    
    print()
    
    if not active_databases:
        print("No active databases found. Nothing to backup.")
        return 0
    
    if inactive_databases:
        print(f"Note: {len(inactive_databases)} database(s) appear to be empty/unused and will be skipped.")
        print()
    
    # Create backups (only for active databases)
    backup_dir = script_dir / "backups"
    successful_backups = []
    failed_backups = []
    
    for db_file in active_databases:
        result = backup_database(db_file, backup_dir)
        if result:
            successful_backups.append(result)
        else:
            failed_backups.append(db_file.name)
    
    print()
    print("=" * 50)
    print(f"Backup Summary:")
    print(f"  Successful: {len(successful_backups)}")
    print(f"  Failed: {len(failed_backups)}")
    
    if successful_backups:
        print(f"\nBackups saved to: {backup_dir}")
        print("\nRecent backups:")
        for backup in successful_backups:
            print(f"  - {Path(backup).name}")
    
    if failed_backups:
        print(f"\nFailed to backup:")
        for db in failed_backups:
            print(f"  - {db}")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

