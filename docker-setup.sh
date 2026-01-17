#!/bin/bash
# Setup script for Docker deployment
# Creates necessary directories and copies example config

set -e

echo "Setting up meshcore-bot Docker environment..."

# Create data directories
echo "Creating data directories..."
mkdir -p data/{config,databases,logs,backups}

# Copy example config if config doesn't exist
if [ ! -f "data/config/config.ini" ]; then
    if [ -f "config.ini.example" ]; then
        echo "Copying config.ini.example to data/config/config.ini..."
        cp config.ini.example data/config/config.ini
        echo ""
        echo "⚠️  IMPORTANT: Please edit data/config/config.ini with your settings!"
        echo "   - Update database paths to use /data/databases/"
        echo "   - Update log file path to use /data/logs/"
        echo "   - Configure your connection settings"
    else
        echo "⚠️  Warning: config.ini.example not found. Please create data/config/config.ini manually."
    fi
else
    echo "✓ Config file already exists at data/config/config.ini"
fi

# Set permissions (container runs as UID 1000)
echo "Setting permissions..."
chmod -R 755 data/
chown -R 1000:1000 data/ 2>/dev/null || echo "Note: Could not set ownership (may need sudo)"

echo ""
echo "✓ Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit data/config/config.ini with your settings"
echo "2. Update database paths in config.ini:"
echo "   [Bot]"
echo "   db_path = /data/databases/meshcore_bot.db"
echo ""
echo "   [Logging]"
echo "   log_file = /data/logs/meshcore_bot.log"
echo ""
echo "3. Start the container:"
echo "   docker-compose up -d"
echo ""
echo "4. View logs:"
echo "   docker-compose logs -f"
