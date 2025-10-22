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
from pathlib import Path

class BotIntegration:
    """Simple bot integration for web viewer compatibility"""
    
    def __init__(self, bot):
        self.bot = bot
        self.circuit_breaker_open = False
        self.circuit_breaker_failures = 0
        self.is_shutting_down = False
    
    def reset_circuit_breaker(self):
        """Reset the circuit breaker"""
        self.circuit_breaker_open = False
        self.circuit_breaker_failures = 0
    
    def capture_full_packet_data(self, packet_data):
        """Capture full packet data and store in database for web viewer"""
        try:
            import sqlite3
            import json
            import time
            
            # Convert non-serializable objects to strings
            serializable_data = self._make_json_serializable(packet_data)
            
            # Store in database for web viewer to read
            db_path = self.bot.config.get('Database', 'path', fallback='bot_data.db')
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Insert packet data
            cursor.execute('''
                INSERT INTO packet_stream (timestamp, data, type)
                VALUES (?, ?, ?)
            ''', (time.time(), json.dumps(serializable_data), 'packet'))
            
            conn.commit()
            conn.close()
            
            # Periodic cleanup (every 100 packets to avoid performance impact)
            if not hasattr(self, '_packet_count'):
                self._packet_count = 0
            self._packet_count += 1
            if self._packet_count % 100 == 0:
                self.cleanup_old_data()
            
        except Exception as e:
            self.bot.logger.debug(f"Error storing packet data: {e}")
    
    def capture_command(self, message, command_name, response, success):
        """Capture command data and store in database for web viewer"""
        try:
            import sqlite3
            import json
            import time
            
            # Extract data from message object
            user = getattr(message, 'sender_id', 'Unknown')
            channel = getattr(message, 'channel', 'Unknown')
            user_input = getattr(message, 'content', f'/{command_name}')
            
            # Construct command data structure
            command_data = {
                'user': user,
                'channel': channel,
                'command': command_name,
                'user_input': user_input,
                'response': response,
                'success': success,
                'timestamp': time.time()
            }
            
            # Convert non-serializable objects to strings
            serializable_data = self._make_json_serializable(command_data)
            
            # Store in database for web viewer to read
            db_path = self.bot.config.get('Database', 'path', fallback='bot_data.db')
            conn = sqlite3.connect(db_path)
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
            db_path = self.bot.config.get('Database', 'path', fallback='bot_data.db')
            conn = sqlite3.connect(db_path)
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
            
            db_path = self.bot.config.get('Database', 'path', fallback='bot_data.db')
            conn = sqlite3.connect(db_path)
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
    
    def shutdown(self):
        """Mark as shutting down"""
        self.is_shutting_down = True

class WebViewerIntegration:
    """Integration class for starting/stopping the web viewer with the bot"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.viewer_process = None
        self.viewer_thread = None
        self.running = False
        
        # Get web viewer settings from config
        self.enabled = bot.config.getboolean('Web_Viewer', 'enabled', fallback=False)
        self.host = bot.config.get('Web_Viewer', 'host', fallback='127.0.0.1')
        self.port = bot.config.getint('Web_Viewer', 'port', fallback=8080)  # Web viewer uses 8080
        self.debug = bot.config.getboolean('Web_Viewer', 'debug', fallback=False)
        self.auto_start = bot.config.getboolean('Web_Viewer', 'auto_start', fallback=False)
        
        # Process monitoring
        self.restart_count = 0
        self.max_restarts = 5
        self.last_restart = 0
        
        # Initialize bot integration for compatibility
        self.bot_integration = BotIntegration(bot)
        
        if self.enabled and self.auto_start:
            self.start_viewer()
    
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
            else:
                self.logger.info("Web viewer already stopped")
            
            # Additional cleanup: kill any remaining processes on the port
            try:
                import subprocess
                result = subprocess.run(['lsof', '-ti', f':{self.port}'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    pids = result.stdout.strip().split('\n')
                    for pid in pids:
                        if pid.strip():
                            try:
                                subprocess.run(['kill', '-9', pid.strip()], timeout=2)
                                self.logger.info(f"Killed remaining process {pid} on port {self.port}")
                            except Exception as e:
                                self.logger.warning(f"Failed to kill process {pid}: {e}")
            except Exception as e:
                self.logger.debug(f"Port cleanup check failed: {e}")
            
        except Exception as e:
            self.logger.error(f"Error stopping web viewer: {e}")
    
    def _run_viewer(self):
        """Run the web viewer in a separate process"""
        try:
            # Get the path to the web viewer script
            viewer_script = Path(__file__).parent / "app.py"
            
            # Build command
            cmd = [
                sys.executable,
                str(viewer_script),
                "--host", self.host,
                "--port", str(self.port)
            ]
            
            if self.debug:
                cmd.append("--debug")
            
            # Start the viewer process
            self.viewer_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Give it a moment to start up
            time.sleep(2)
            
            # Check if it started successfully
            if self.viewer_process and self.viewer_process.poll() is not None:
                stdout, stderr = self.viewer_process.communicate()
                self.logger.error(f"Web viewer failed to start. Return code: {self.viewer_process.returncode}")
                if stderr:
                    self.logger.error(f"Web viewer startup error: {stderr}")
                if stdout:
                    self.logger.error(f"Web viewer startup output: {stdout}")
                self.viewer_process = None
                return
            
            # Web viewer is ready
            self.logger.info("Web viewer integration ready for data streaming")
            
            # Monitor the process
            while self.running and self.viewer_process and self.viewer_process.poll() is None:
                time.sleep(1)
            
            if self.viewer_process and self.viewer_process.returncode != 0:
                stdout, stderr = self.viewer_process.communicate()
                self.logger.error(f"Web viewer process exited with code {self.viewer_process.returncode}")
                if stderr:
                    self.logger.error(f"Web viewer stderr: {stderr}")
                if stdout:
                    self.logger.error(f"Web viewer stdout: {stdout}")
            elif self.viewer_process and self.viewer_process.returncode == 0:
                self.logger.info("Web viewer process exited normally")
                    
        except Exception as e:
            self.logger.error(f"Error running web viewer: {e}")
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
