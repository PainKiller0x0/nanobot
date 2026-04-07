#!/bin/bash

# Simple, non-recursive backup script for nanobot.
# Run this via cron, e.g., "0 4 * * * /path/to/scripts/backup_sessions.sh"

backup_dir="$HOME/.nanobot/backups"
mkdir -p "$backup_dir"

timestamp=$(date +%Y%m%d_%H%M%S)
archive_name="nanobot_backup_$timestamp.tar.gz"
archive_path="$backup_dir/$archive_name"

echo "Creating backup..."
# We backup the sessions db/jsonl and config files
tar -czf "$archive_path" -C "$HOME" --exclude="backups" --exclude="ark" \
    .nanobot/sessions \
    .nanobot/config.json \
    .nanobot/runtime_config.yaml 2>/dev/null

if [ $? -eq 0 ]; then
    echo "Backup successful: $archive_path"
    
    # Optional: Keep only the 10 most recent backups
    ls -tp "$backup_dir"/nanobot_backup_*.tar.gz | grep -v '/$' | tail -n +11 | xargs -I {} rm -- {} 2>/dev/null
    
    exit 0
else
    echo "Backup failed!"
    exit 1
fi
