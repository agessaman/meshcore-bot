#!/usr/bin/env python3
"""
Web Viewer Integration for MeshCore Bot
Provides integration between the main bot and the web viewer
"""

import threading
import time
import subprocess
import sys
import os
import re
from pathlib import Path

from ..utils import resolve_path

class BotIntegration:
    """Simple bot integration for web viewer compatibility"""
    
    def __init__(self, bot):
        self.bot = bot
        self.circuit_breaker_open = False
        self.circuit_breaker_failures = 0
        self.is_shutting_down = False
        # Initialize HTTP session with connection pooling for efficient reuse
        self._init_http_session()
        # Initialize the packet_stream table
        self._init_packet_stream_table()
    
    def _init_http_session(self):
        """Initialize a requests.Session with connection pooling and keep-alive"""
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            import urllib3
            import logging
            
            # Suppress urllib3 connection pool debug messages
            # "Resetting dropped connection" is expected behavior when connections are idle
            # and the connection pool is working correctly
            urllib3_logger = logging.getLogger('urllib3.connectionpool')
            urllib3_logger.setLevel(logging.INFO)  # Suppress DEBUG messages
            
            # Also disable other urllib3 warnings
            urllib3.disable_warnings(urllib3.exceptions.NotOpenSSLWarning)
            
            self.http_session = requests.Session()
            
            # Configure retry strategy
            retry_strategy = Retry(
                total=2,
                backoff_factor=0.1,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            
            # Mount adapter with connection pooling
            # pool_block=False allows non-blocking behavior if pool is full
            adapter = HTTPAdapter(
                pool_connections=1,  # Single connection pool for web viewer
                pool_maxsize=5,      # Allow up to 5 connections in the pool
                max_retries=retry_strategy,
                pool_block=False     # Don't block if pool is full
            )
            self.http_session.mount("http://", adapter)
            self.http_session.mount("https://", adapter)
            
            # Set default headers for keep-alive (though urllib3 handles this automatically)
            self.http_session.headers.update({
                'Connection': 'keep-alive',
            })
        except ImportError:
            # Fallback if requests is not available
            self.http_session = None
        except Exception as e:
            self.bot.logger.debug(f"Error initializing HTTP session: {e}")
            self.http_session = None
    
    def reset_circuit_breaker(self):
        """Reset the circuit breaker"""
        self.circuit_breaker_open = False
        self.circuit_breaker_failures = 0
    
    def _get_web_viewer_db_path(self):
        """Return resolved database path for web viewer. Uses [Bot] db_path when [Web_Viewer] db_path is unset."""
        base_dir = self.bot.bot_root if hasattr(self.bot, 'bot_root') else '.'
        if self.bot.config.has_section('Web_Viewer') and self.bot.config.has_option('Web_Viewer', 'db_path'):
            raw = self.bot.config.get('Web_Viewer', 'db_path', fallback='').strip()
            if raw:
                return resolve_path(raw, base_dir)
        return str(Path(self.bot.db_manager.db_path).resolve())
    
    def _init_packet_stream_table(self):
        """Initialize the packet_stream table in the web viewer database (same as [Bot] db_path by default)."""
        try:
            import sqlite3
            
            db_path = self._get_web_viewer_db_path()
            
            # Connect to database and create table if it doesn't exist
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            cursor = conn.cursor()
            
            # Create packet_stream table with schema matching the INSERT statements
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS packet_stream (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    data TEXT NOT NULL,
                    type TEXT NOT NULL
                )
            ''')
            
            # Create index on timestamp for faster queries
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_packet_stream_timestamp 
                ON packet_stream(timestamp)
            ''')
            
            # Create index on type for filtering by type
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_packet_stream_type 
                ON packet_stream(type)
            ''')
            
            conn.commit()
            conn.close()
            
            self.bot.logger.info(f"Initialized packet_stream table in {db_path}")
            
        except Exception as e:
            self.bot.logger.error(f"Failed to initialize packet_stream table: {e}")
            # Don't raise - allow bot to continue even if table init fails
            # The error will be caught when trying to insert data
    
    def capture_full_packet_data(self, packet_data):
        """Capture full packet data and store in database for web viewer"""
        try:
            import sqlite3
            import json
            import time
            from datetime import datetime
            
            # Ensure packet_data is a dict (might be passed as dict already)
            if not isinstance(packet_data, dict):
                packet_data = self._make_json_serializable(packet_data)
                if not isinstance(packet_data, dict):
                    # If still not a dict, wrap it
                    packet_data = {'data': packet_data}
            
            # Add hops field from path_len if not already present
            # path_len represents the number of hops (each byte = 1 hop)
            if 'hops' not in packet_data and 'path_len' in packet_data:
                packet_data['hops'] = packet_data.get('path_len', 0)
            elif 'hops' not in packet_data:
                # If no path_len either, default to 0 hops
                packet_data['hops'] = 0
            
            # Add datetime for frontend display
            if 'datetime' not in packet_data:
                packet_data['datetime'] = datetime.now().isoformat()
            
            # Convert non-serializable objects to strings
            serializable_data = self._make_json_serializable(packet_data)
            
            # Store in database for web viewer to read
            db_path = self.bot.config.get('Web_Viewer', 'db_path', fallback='meshcore_bot.db')
            # Resolve database path (relative paths resolved from bot root, absolute paths used as-is)
            base_dir = self.bot.bot_root if hasattr(self.bot, 'bot_root') else '.'
            db_path = resolve_path(db_path, base_dir)
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            cursor = conn.cursor()
            
            # Insert packet data
            cursor.execute('''
                INSERT INTO packet_stream (timestamp, data, type)
                VALUES (?, ?, ?)
            ''', (time.time(), json.dumps(serializable_data), 'packet'))
            
            conn.commit()
            conn.close()
            
            # Note: Cleanup is handled by the web viewer subprocess to avoid
            # database lock contention between bot and web viewer processes
            
        except Exception as e:
            self.bot.logger.debug(f"Error storing packet data: {e}")
    
    def capture_command(self, message, command_name, response, success, command_id=None):
        """Capture command data and store in database for web viewer"""
        try:
            import sqlite3
            import json
            import time
            
            # Extract data from message object
            user = getattr(message, 'sender_id', 'Unknown')
            channel = getattr(message, 'channel', 'Unknown')
            user_input = getattr(message, 'content', f'/{command_name}')
            
            # Get repeat information if transmission tracker is available
            repeat_count = 0
            repeater_prefixes = []
            repeater_counts = {}
            if (hasattr(self.bot, 'transmission_tracker') and 
                self.bot.transmission_tracker and 
                command_id):
                repeat_info = self.bot.transmission_tracker.get_repeat_info(command_id=command_id)
                repeat_count = repeat_info.get('repeat_count', 0)
                repeater_prefixes = repeat_info.get('repeater_prefixes', [])
                repeater_counts = repeat_info.get('repeater_counts', {})
            
            # Construct command data structure
            command_data = {
                'user': user,
                'channel': channel,
                'command': command_name,
                'user_input': user_input,
                'response': response,
                'success': success,
                'timestamp': time.time(),
                'repeat_count': repeat_count,
                'repeater_prefixes': repeater_prefixes,
                'repeater_counts': repeater_counts,  # Count per repeater prefix
                'command_id': command_id  # Store command_id for later updates
            }
            
            # Convert non-serializable objects to strings
            serializable_data = self._make_json_serializable(command_data)
            
            # Store in database for web viewer to read
            db_path = self._get_web_viewer_db_path()
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            cursor = conn.cursor()
            
            # Insert command data
            cursor.execute('''
                INSERT INTO packet_stream (timestamp, data, type)
                VALUES (?, ?, ?)
            ''', (time.time(), json.dumps(serializable_data), 'command'))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            self.bot.logger.debug(f"Error storing command data: {e}")
    
    def capture_packet_routing(self, routing_data):
        """Capture packet routing data and store in database for web viewer"""
        try:
            import sqlite3
            import json
            import time
            
            # Convert non-serializable objects to strings
            serializable_data = self._make_json_serializable(routing_data)
            
            # Store in database for web viewer to read
            db_path = self._get_web_viewer_db_path()
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            cursor = conn.cursor()
            
            # Insert routing data
            cursor.execute('''
                INSERT INTO packet_stream (timestamp, data, type)
                VALUES (?, ?, ?)
            ''', (time.time(), json.dumps(serializable_data), 'routing'))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            self.bot.logger.debug(f"Error storing routing data: {e}")
    
    def cleanup_old_data(self, days_to_keep: int = 7):
        """Clean up old packet stream data to prevent database bloat"""
        try:
            import sqlite3
            import time
            
            cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
            
            db_path = self._get_web_viewer_db_path()
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            cursor = conn.cursor()
            
            # Clean up old packet stream data
            cursor.execute('DELETE FROM packet_stream WHERE timestamp < ?', (cutoff_time,))
            deleted_count = cursor.rowcount
            
            conn.commit()
            conn.close()
            
            if deleted_count > 0:
                self.bot.logger.info(f"Cleaned up {deleted_count} old packet stream entries (older than {days_to_keep} days)")
            
        except Exception as e:
            self.bot.logger.error(f"Error cleaning up old packet stream data: {e}")
    
    def _make_json_serializable(self, obj, depth=0, max_depth=3):
        """Convert non-JSON-serializable objects to strings with depth limiting"""
        if depth > max_depth:
            return str(obj)
        
        # Handle basic types first
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        elif isinstance(obj, (list, tuple)):
            return [self._make_json_serializable(item, depth + 1) for item in obj]
        elif isinstance(obj, dict):
            return {k: self._make_json_serializable(v, depth + 1) for k, v in obj.items()}
        elif hasattr(obj, 'name'):  # Enum-like objects
            return obj.name
        elif hasattr(obj, 'value'):  # Enum values
            return obj.value
        elif hasattr(obj, '__dict__'):
            # Convert objects to dict, but limit depth
            try:
                return {k: self._make_json_serializable(v, depth + 1) for k, v in obj.__dict__.items()}
            except (RecursionError, RuntimeError):
                return str(obj)
        else:
            return str(obj)
    
    def send_mesh_edge_update(self, edge_data):
        """Send mesh edge update to web viewer via HTTP API"""
        try:
            # Get web viewer URL from config
            host = self.bot.config.get('Web_Viewer', 'host', fallback='127.0.0.1')
            port = self.bot.config.getint('Web_Viewer', 'port', fallback=8080)
            url = f"http://{host}:{port}/api/stream_data"
            
            payload = {
                'type': 'mesh_edge',
                'data': edge_data
            }
            
            # Use session with connection pooling if available, otherwise fallback to requests.post
            if self.http_session:
                try:
                    # Use a slightly longer timeout to allow connection reuse
                    self.http_session.post(url, json=payload, timeout=1.0)
                except Exception:
                    # Silently fail - web viewer might not be running
                    pass
            else:
                # Fallback if session not initialized
                import requests
                try:
                    requests.post(url, json=payload, timeout=1.0)
                except Exception:
                    pass
        except Exception as e:
            self.bot.logger.debug(f"Error sending mesh edge update to web viewer: {e}")
    
    def send_mesh_node_update(self, node_data):
        """Send mesh node update to web viewer via HTTP API"""
        try:
            import requests
            import json
            
            # Get web viewer URL from config
            host = self.bot.config.get('Web_Viewer', 'host', fallback='127.0.0.1')
            port = self.bot.config.getint('Web_Viewer', 'port', fallback=8080)
            url = f"http://{host}:{port}/api/stream_data"
            
            payload = {
                'type': 'mesh_node',
                'data': node_data
            }
            
            # Send asynchronously (don't block)
            try:
                requests.post(url, json=payload, timeout=0.5)
            except Exception:
                # Silently fail - web viewer might not be running
                pass
        except Exception as e:
            self.bot.logger.debug(f"Error sending mesh node update to web viewer: {e}")
    
    def shutdown(self):
        """Mark as shutting down and close HTTP session"""
        self.is_shutting_down = True
        # Close HTTP session to clean up connections
        if hasattr(self, 'http_session') and self.http_session:
            try:
                self.http_session.close()
            except Exception:
                pass

class WebViewerIntegration:
    """Integration class for starting/stopping the web viewer with the bot"""
    
    # Whitelist of allowed host bindings for security
    ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '0.0.0.0']
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.viewer_process = None
        self.viewer_thread = None
        self.running = False
        
        # File handles for subprocess stdout/stderr (for proper cleanup)
        self._viewer_stdout_file = None
        self._viewer_stderr_file = None
        
        # Get web viewer settings from config
        self.enabled = bot.config.getboolean('Web_Viewer', 'enabled', fallback=False)
        self.host = bot.config.get('Web_Viewer', 'host', fallback='127.0.0.1')
        self.port = bot.config.getint('Web_Viewer', 'port', fallback=8080)  # Web viewer uses 8080
        self.debug = bot.config.getboolean('Web_Viewer', 'debug', fallback=False)
        self.auto_start = bot.config.getboolean('Web_Viewer', 'auto_start', fallback=False)
        
        # Validate configuration for security
        self._validate_config()
        
        # Process monitoring
        self.restart_count = 0
        self.max_restarts = 5
        self.last_restart = 0
        
        # Initialize bot integration for compatibility
        self.bot_integration = BotIntegration(bot)
        
        if self.enabled and self.auto_start:
            self.start_viewer()
    
    def _validate_config(self):
        """Validate web viewer configuration for security"""
        # Validate host against whitelist
        if self.host not in self.ALLOWED_HOSTS:
            raise ValueError(
                f"Invalid host configuration: {self.host}. "
                f"Allowed hosts: {', '.join(self.ALLOWED_HOSTS)}"
            )
        
        # Validate port range (avoid privileged ports)
        if not isinstance(self.port, int) or not (1024 <= self.port <= 65535):
            raise ValueError(
                f"Port must be between 1024-65535 (non-privileged), got: {self.port}"
            )
        
        # Security warning for network exposure
        if self.host == '0.0.0.0':
            self.logger.warning(
                "\n" + "="*70 + "\n"
                "⚠️  SECURITY WARNING: Web viewer binding to all interfaces (0.0.0.0)\n"
                "This exposes bot data (messages, contacts, routing) to your network\n"
                "WITHOUT AUTHENTICATION. Ensure you have firewall protection!\n"
                "For local-only access, use host=127.0.0.1 in config.\n"
                + "="*70
            )
    
    def start_viewer(self):
        """Start the web viewer in a separate thread"""
        if self.running:
            self.logger.warning("Web viewer is already running")
            return
        
        try:
            # Start the web viewer
            self.viewer_thread = threading.Thread(target=self._run_viewer, daemon=True)
            self.viewer_thread.start()
            self.running = True
            self.logger.info(f"Web viewer started on http://{self.host}:{self.port}")
            
        except Exception as e:
            self.logger.error(f"Failed to start web viewer: {e}")
    
    def stop_viewer(self):
        """Stop the web viewer"""
        if not self.running and not self.viewer_process:
            return
        
        try:
            self.running = False
            
            if self.viewer_process and self.viewer_process.poll() is None:
                self.logger.info("Stopping web viewer...")
                try:
                    # First try graceful termination
                    self.viewer_process.terminate()
                    self.viewer_process.wait(timeout=5)
                    self.logger.info("Web viewer stopped gracefully")
                except subprocess.TimeoutExpired:
                    self.logger.warning("Web viewer did not stop gracefully, forcing termination")
                    try:
                        self.viewer_process.kill()
                        self.viewer_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.logger.error("Failed to kill web viewer process")
                    except Exception as e:
                        self.logger.warning(f"Error during forced termination: {e}")
                except Exception as e:
                    self.logger.warning(f"Error during web viewer shutdown: {e}")
                finally:
                    self.viewer_process = None
            
            # Close log file handles
            if self._viewer_stdout_file:
                try:
                    self._viewer_stdout_file.close()
                except Exception as e:
                    self.logger.debug(f"Error closing stdout file: {e}")
                finally:
                    self._viewer_stdout_file = None
            
            if self._viewer_stderr_file:
                try:
                    self._viewer_stderr_file.close()
                except Exception as e:
                    self.logger.debug(f"Error closing stderr file: {e}")
                finally:
                    self._viewer_stderr_file = None
            
            if not self.viewer_process:
                self.logger.info("Web viewer already stopped")
            
            # Additional cleanup: kill any remaining processes on the port
            try:
                import subprocess
                result = subprocess.run(['lsof', '-ti', f':{self.port}'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    pids = result.stdout.strip().split('\n')
                    for pid in pids:
                        pid = pid.strip()
                        if not pid:
                            continue
                        
                        # Validate PID is numeric only (prevent injection)
                        if not re.match(r'^\d+$', pid):
                            self.logger.warning(f"Invalid PID format: {pid}, skipping")
                            continue
                        
                        try:
                            pid_int = int(pid)
                            # Safety check: never kill system PIDs
                            if pid_int < 2:
                                self.logger.warning(f"Refusing to kill system PID: {pid}")
                                continue
                            
                            subprocess.run(['kill', '-9', str(pid_int)], timeout=2)
                            self.logger.info(f"Killed remaining process {pid} on port {self.port}")
                        except (ValueError, subprocess.TimeoutExpired) as e:
                            self.logger.warning(f"Failed to kill process {pid}: {e}")
            except Exception as e:
                self.logger.debug(f"Port cleanup check failed: {e}")
            
        except Exception as e:
            self.logger.error(f"Error stopping web viewer: {e}")
    
    def _run_viewer(self):
        """Run the web viewer in a separate process"""
        stdout_file = None
        stderr_file = None
        
        try:
            # Get the path to the web viewer script
            viewer_script = Path(__file__).parent / "app.py"
            # Use same config as bot so viewer finds db_path, Greeter_Command, etc.
            config_path = getattr(self.bot, 'config_file', 'config.ini')
            config_path = str(Path(config_path).resolve()) if config_path else 'config.ini'
            
            # Build command
            cmd = [
                sys.executable,
                str(viewer_script),
                "--config", config_path,
                "--host", self.host,
                "--port", str(self.port)
            ]
            
            if self.debug:
                cmd.append("--debug")
            
            # Ensure logs directory exists
            os.makedirs('logs', exist_ok=True)
            
            # Open log files in write mode to prevent buffer blocking
            # This fixes the issue where subprocess.PIPE buffers (~64KB) fill up
            # after ~5 minutes and cause the subprocess to hang.
            # Using 'w' mode (overwrite) instead of 'a' (append) since:
            # - The web viewer already has proper logging to web_viewer_modern.log
            # - stdout/stderr are mainly for immediate debugging
            # - Prevents unbounded log file growth
            stdout_file = open('logs/web_viewer_stdout.log', 'w')
            stderr_file = open('logs/web_viewer_stderr.log', 'w')
            
            # Store file handles for proper cleanup
            self._viewer_stdout_file = stdout_file
            self._viewer_stderr_file = stderr_file
            
            # Start the viewer process with log file redirection
            self.viewer_process = subprocess.Popen(
                cmd,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True
            )
            
            # Give it a moment to start up
            time.sleep(2)
            
            # Check if it started successfully
            if self.viewer_process and self.viewer_process.poll() is not None:
                # Process failed immediately - read from log files for error reporting
                stdout_file.flush()
                stderr_file.flush()
                
                # Read last few lines from stderr for error reporting
                try:
                    stderr_file.close()
                    with open('logs/web_viewer_stderr.log', 'r') as f:
                        stderr_lines = f.readlines()[-20:]  # Last 20 lines
                        stderr = ''.join(stderr_lines)
                except Exception:
                    stderr = "Could not read stderr log"
                
                # Read last few lines from stdout for error reporting
                try:
                    stdout_file.close()
                    with open('logs/web_viewer_stdout.log', 'r') as f:
                        stdout_lines = f.readlines()[-20:]  # Last 20 lines
                        stdout = ''.join(stdout_lines)
                except Exception:
                    stdout = "Could not read stdout log"
                
                self.logger.error(f"Web viewer failed to start. Return code: {self.viewer_process.returncode}")
                if stderr and stderr.strip():
                    self.logger.error(f"Web viewer startup error: {stderr}")
                if stdout and stdout.strip():
                    self.logger.error(f"Web viewer startup output: {stdout}")
                
                self.viewer_process = None
                self._viewer_stdout_file = None
                self._viewer_stderr_file = None
                return
            
            # Web viewer is ready
            self.logger.info("Web viewer integration ready for data streaming")
            
            # Monitor the process
            while self.running and self.viewer_process and self.viewer_process.poll() is None:
                time.sleep(1)
            
            # Process exited - read from log files for error reporting if needed
            if self.viewer_process and self.viewer_process.returncode != 0:
                stdout_file.flush()
                stderr_file.flush()
                
                # Read last few lines from stderr for error reporting
                try:
                    stderr_file.close()
                    with open('logs/web_viewer_stderr.log', 'r') as f:
                        stderr_lines = f.readlines()[-20:]  # Last 20 lines
                        stderr = ''.join(stderr_lines)
                except Exception:
                    stderr = "Could not read stderr log"
                
                # Close stdout file as well
                try:
                    stdout_file.close()
                except Exception:
                    pass
                
                self.logger.error(f"Web viewer process exited with code {self.viewer_process.returncode}")
                if stderr and stderr.strip():
                    self.logger.error(f"Web viewer stderr: {stderr}")
                
                self._viewer_stdout_file = None
                self._viewer_stderr_file = None
            elif self.viewer_process and self.viewer_process.returncode == 0:
                self.logger.info("Web viewer process exited normally")
                    
        except Exception as e:
            self.logger.error(f"Error running web viewer: {e}")
            # Close file handles on error
            if stdout_file:
                try:
                    stdout_file.close()
                except Exception:
                    pass
            if stderr_file:
                try:
                    stderr_file.close()
                except Exception:
                    pass
            self._viewer_stdout_file = None
            self._viewer_stderr_file = None
        finally:
            self.running = False
    
    def get_status(self):
        """Get the current status of the web viewer"""
        return {
            'enabled': self.enabled,
            'running': self.running,
            'host': self.host,
            'port': self.port,
            'debug': self.debug,
            'auto_start': self.auto_start,
            'url': f"http://{self.host}:{self.port}" if self.running else None
        }
    
    def restart_viewer(self):
        """Restart the web viewer with rate limiting"""
        current_time = time.time()
        
        # Rate limit restarts to prevent restart loops
        if current_time - self.last_restart < 30:  # 30 seconds between restarts
            self.logger.warning("Restart rate limited - too soon since last restart")
            return
        
        if self.restart_count >= self.max_restarts:
            self.logger.error(f"Maximum restart limit reached ({self.max_restarts}). Web viewer disabled.")
            self.enabled = False
            return
        
        self.restart_count += 1
        self.last_restart = current_time
        
        self.logger.info(f"Restarting web viewer (attempt {self.restart_count}/{self.max_restarts})...")
        self.stop_viewer()
        time.sleep(3)  # Give it more time to stop
        
        self.start_viewer()
    
    def is_viewer_healthy(self):
        """Check if the web viewer process is healthy"""
        if not self.viewer_process:
            return False
        
        # Check if process is still running
        if self.viewer_process.poll() is not None:
            return False
        
        return True
