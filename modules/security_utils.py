#!/usr/bin/env python3
"""
Security Utilities for MeshCore Bot
Provides centralized security validation functions to prevent common attacks
"""

import re
import ipaddress
import socket
import platform
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import logging

logger = logging.getLogger('MeshCoreBot.Security')


def _is_nix_environment() -> bool:
    """
    Detect if running in a Nix environment
    
    Returns:
        True if running in Nix build or NixOS
    """
    # Check for Nix store path (most reliable indicator)
    if 'NIX_STORE' in os.environ:
        return True
    
    # Check if we're in a Nix store path
    try:
        current_path = Path.cwd().resolve()
        # Nix store paths typically look like /nix/store/<hash>-<name>
        if '/nix/store/' in str(current_path):
            return True
    except Exception:
        pass
    
    # Check for Nix-related environment variables
    nix_env_vars = ['NIX_PATH', 'NIX_REMOTE', 'IN_NIX_SHELL']
    if any(var in os.environ for var in nix_env_vars):
        return True
    
    return False


def validate_external_url(url: str, allow_localhost: bool = False, timeout: float = 2.0) -> bool:
    """
    Validate that URL points to safe external resource (SSRF protection)
    
    Args:
        url: URL to validate
        allow_localhost: Whether to allow localhost/private IPs (default: False)
        timeout: DNS resolution timeout in seconds (default: 2.0)
    
    Returns:
        True if URL is safe, False otherwise
    
    Raises:
        ValueError: If URL is invalid or unsafe
    """
    try:
        parsed = urlparse(url)
        
        # Only allow HTTP/HTTPS
        if parsed.scheme not in ['http', 'https']:
            logger.warning(f"URL scheme not allowed: {parsed.scheme}")
            return False
        
        # Reject file:// and other dangerous schemes
        if not parsed.netloc:
            logger.warning(f"URL missing network location: {url}")
            return False
        
        # Resolve and check if IP is internal/private (with timeout)
        try:
            # Set socket timeout for DNS resolution
            # Note: getdefaulttimeout() can return None (no timeout), which is valid
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            try:
                ip = socket.gethostbyname(parsed.hostname)
            finally:
                # Restore original timeout (None means no timeout, which is correct)
                socket.setdefaulttimeout(old_timeout)
            
            ip_obj = ipaddress.ip_address(ip)
            
            # If localhost is not allowed, reject private/internal IPs
            if not allow_localhost:
                # Reject private/internal IPs
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                    logger.warning(f"URL resolves to private/internal IP: {ip}")
                    return False
                
                # Reject reserved ranges
                if ip_obj.is_reserved or ip_obj.is_multicast:
                    logger.warning(f"URL resolves to reserved/multicast IP: {ip}")
                    return False
        
        except socket.gaierror as e:
            logger.warning(f"Failed to resolve hostname {parsed.hostname}: {e}")
            return False
        except socket.timeout:
            logger.warning(f"DNS resolution timeout for {parsed.hostname}")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"URL validation failed: {e}")
        return False


def validate_safe_path(file_path: str, base_dir: str = '.', allow_absolute: bool = False) -> Path:
    """
    Validate that path is safe with configurable restrictions
    
    When allow_absolute=False (default):
    - Paths must be within base_dir (prevents path traversal)
    - Blocks dangerous system directories
    
    When allow_absolute=True:
    - Allows paths outside base_dir (for user-configured database/log locations)
    - Still blocks dangerous system directories
    - In Nix environments, dangerous directory checks are relaxed (Nix provides isolation)
    
    Args:
        file_path: Path to validate
        base_dir: Base directory that path must be within (when allow_absolute=False)
        allow_absolute: Whether to allow absolute paths outside base_dir
    
    Returns:
        Resolved Path object if safe
    
    Raises:
        ValueError: If path is unsafe or attempts traversal
    """
    try:
        # Resolve base directory to absolute path
        base = Path(base_dir).resolve()
        
        # Resolve target path relative to base_dir (not current working directory)
        # If file_path is absolute, use it directly; otherwise join with base_dir
        if Path(file_path).is_absolute():
            target = Path(file_path).resolve()
        else:
            # Join with base_dir first, then resolve to handle relative paths correctly
            target = (base / file_path).resolve()
        
        # If absolute paths are not allowed, ensure target is within base
        if not allow_absolute:
            # Check if target is within base directory
            try:
                target.relative_to(base)
            except ValueError:
                raise ValueError(
                    f"Path traversal detected: {file_path} is not within {base_dir}"
                )
        
        # Check for dangerous system paths (OS-specific)
        # In Nix environments, skip this check as Nix provides strong isolation
        is_nix = _is_nix_environment()
        
        if not is_nix:
            system = platform.system()
            if system == 'Windows':
                dangerous_prefixes = [
                    'C:\\Windows\\System32',
                    'C:\\Windows\\SysWOW64',
                    'C:\\Program Files',
                    'C:\\ProgramData',
                    'C:\\Windows\\System',
                ]
                # Check against both forward and backslash paths
                target_str = str(target).lower()
                dangerous = any(target_str.startswith(prefix.lower()) for prefix in dangerous_prefixes)
            elif system == 'Darwin':  # macOS
                dangerous_prefixes = [
                    '/System',
                    '/Library',
                    '/private',
                    '/usr/bin',
                    '/usr/sbin',
                    '/sbin',
                    '/bin',
                ]
                target_str = str(target)
                dangerous = any(target_str.startswith(prefix) for prefix in dangerous_prefixes)
            else:  # Linux and other Unix-like systems
                dangerous_prefixes = ['/etc', '/sys', '/proc', '/dev', '/bin', '/sbin', '/boot']
                target_str = str(target)
                dangerous = any(target_str.startswith(prefix) for prefix in dangerous_prefixes)
            
            if dangerous:
                raise ValueError(f"Access to system directory denied: {file_path}")
        
        return target
        
    except ValueError:
        # Re-raise ValueError as-is (these are our validation errors)
        raise
    except Exception as e:
        raise ValueError(f"Invalid or unsafe file path: {file_path} - {e}")


def sanitize_input(content: str, max_length: Optional[int] = 500, strip_controls: bool = True) -> str:
    """
    Sanitize user input to prevent injection attacks
    
    Args:
        content: Input string to sanitize
        max_length: Maximum allowed length (default: 500 chars, None to disable length check)
        strip_controls: Whether to remove control characters (default: True)
    
    Returns:
        Sanitized string
    
    Raises:
        ValueError: If max_length is negative
    """
    if not isinstance(content, str):
        content = str(content)
    
    # Validate max_length if provided
    if max_length is not None:
        if max_length < 0:
            raise ValueError(f"max_length must be non-negative, got {max_length}")
        # Limit length to prevent DoS
        if len(content) > max_length:
            content = content[:max_length]
            logger.debug(f"Input truncated to {max_length} characters")
    
    # Remove control characters except newline, carriage return, tab
    if strip_controls:
        # Keep only printable characters plus common whitespace
        content = ''.join(
            char for char in content 
            if ord(char) >= 32 or char in '\n\r\t'
        )
    
    # Remove null bytes (can cause issues in C libraries)
    content = content.replace('\x00', '')
    
    return content.strip()


def validate_api_key_format(api_key: str, min_length: int = 16) -> bool:
    """
    Validate API key format
    
    Args:
        api_key: API key to validate
        min_length: Minimum required length (default: 16)
    
    Returns:
        True if format is valid, False otherwise
    """
    if not isinstance(api_key, str):
        return False
    
    # Check minimum length
    if len(api_key) < min_length:
        return False
    
    # Check for obviously invalid patterns
    invalid_patterns = [
        'your_api_key_here',
        'placeholder',
        'example',
        'test_key',
        '12345',
        'aaaa',
    ]
    
    api_key_lower = api_key.lower()
    if any(pattern in api_key_lower for pattern in invalid_patterns):
        return False
    
    # Check that it's not all the same character
    if len(set(api_key)) < 3:
        return False
    
    return True


def validate_pubkey_format(pubkey: str, expected_length: int = 64) -> bool:
    """
    Validate public key format (hex string)
    
    Args:
        pubkey: Public key to validate
        expected_length: Expected length in characters (default: 64 for ed25519)
    
    Returns:
        True if format is valid, False otherwise
    """
    if not isinstance(pubkey, str):
        return False
    
    # Check exact length
    if len(pubkey) != expected_length:
        return False
    
    # Check hex format
    if not re.match(r'^[0-9a-fA-F]+$', pubkey):
        return False
    
    return True


def validate_port_number(port: int, allow_privileged: bool = False) -> bool:
    """
    Validate port number
    
    Args:
        port: Port number to validate
        allow_privileged: Whether to allow privileged ports <1024 (default: False)
    
    Returns:
        True if port is valid, False otherwise
    """
    if not isinstance(port, int):
        return False
    
    min_port = 1 if allow_privileged else 1024
    max_port = 65535
    
    return min_port <= port <= max_port


def validate_integer_range(value: int, min_value: int, max_value: int, name: str = "value") -> bool:
    """
    Validate integer is within range
    
    Args:
        value: Integer to validate
        min_value: Minimum allowed value (inclusive)
        max_value: Maximum allowed value (inclusive)
        name: Name of the value for error messages
    
    Returns:
        True if valid
    
    Raises:
        ValueError: If value is out of range
    """
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__}")
    
    if value < min_value or value > max_value:
        raise ValueError(
            f"{name} must be between {min_value} and {max_value}, got {value}"
        )
    
    return True
