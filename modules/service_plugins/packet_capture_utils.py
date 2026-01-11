#!/usr/bin/env python3
"""
Utilities for packet capture service
Includes auth token functionality with on-device signing support
Adapted from meshcore-packet-capture auth_token.py
"""

import os
import time
import hashlib
import base64
import json
import asyncio
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Try PyNaCl first (preferred for JWT tokens)
try:
    import nacl.bindings
    import nacl.signing
    import nacl.exceptions
    PYNACL_AVAILABLE = True
except ImportError:
    PYNACL_AVAILABLE = False
    nacl = None

# Fallback to cryptography library
try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.backends import default_backend
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    ed25519 = None


def hex_to_bytes(hex_str: str) -> bytes:
    """Convert hex string to bytes.
    
    Args:
        hex_str: Hexadecimal string to convert.
        
    Returns:
        bytes: Converted bytes object.
    """
    return bytes.fromhex(hex_str.replace('0x', '').replace(' ', ''))


def bytes_to_hex(data: bytes) -> str:
    """Convert bytes to hex string (lowercase).
    
    Args:
        data: Bytes object to convert.
        
    Returns:
        str: Hexadecimal representation of the bytes (lowercase).
    """
    return data.hex()


def base64url_encode(data: bytes) -> str:
    """Base64url encode (URL-safe base64 without padding).
    
    Args:
        data: Data to encode.
        
    Returns:
        str: URL-safe Base64 encoded string.
    """
    b64 = base64.b64encode(data).decode('ascii')
    return b64.replace('+', '-').replace('/', '_').replace('=', '')


def base64url_decode(data: str) -> bytes:
    """Base64url decode.
    
    Args:
        data: URL-safe Base64 encoded string.
        
    Returns:
        bytes: Decoded bytes.
    """
    b64 = data.replace('-', '+').replace('_', '/')
    padding = 4 - (len(b64) % 4)
    if padding != 4:
        b64 += '=' * padding
    return base64.b64decode(b64)


def int_to_bytes_le(value: int, length: int) -> bytes:
    """Convert integer to little-endian bytes.
    
    Args:
        value: Integer value to convert.
        length: Number of bytes to use.
        
    Returns:
        bytes: Little-endian byte representation.
    """
    return value.to_bytes(length, byteorder='little')


def bytes_to_int_le(data: bytes) -> int:
    """Convert little-endian bytes to integer.
    
    Args:
        data: Bytes object to convert.
        
    Returns:
        int: Integer value.
    """
    return int.from_bytes(data, byteorder='little')


# Ed25519 group order
L = 2**252 + 27742317777372353535851937790883648493


def ed25519_sign_with_expanded_key(message: bytes, scalar: bytes, prefix: bytes, public_key: bytes) -> bytes:
    """Sign a message using Ed25519 with pre-expanded key (orlp format).
    
    This implements RFC 8032 Ed25519 signing with an already-expanded key.
    This matches exactly how orlp/ed25519's ed25519_sign() works.
    
    Args:
        message: Message to sign.
        scalar: First 32 bytes of orlp private key (clamped scalar).
        prefix: Last 32 bytes of orlp private key (prefix for nonce).
        public_key: 32-byte public key.
        
    Returns:
        bytes: 64-byte signature (R || s).
        
    Raises:
        ImportError: If PyNaCl is not available.
    """
    if not PYNACL_AVAILABLE:
        raise ImportError("PyNaCl is required for Ed25519 signing")
    
    # Step 1: Compute nonce r = H(prefix || message) mod L
    h_r = hashlib.sha512(prefix + message).digest()
    r = bytes_to_int_le(h_r) % L
    
    # Step 2: Compute R = r * B (base point multiplication)
    r_bytes = int_to_bytes_le(r, 32)
    R = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(r_bytes)
    
    # Step 3: Compute challenge k = H(R || public_key || message) mod L
    h_k = hashlib.sha512(R + public_key + message).digest()
    k = bytes_to_int_le(h_k) % L
    
    # Step 4: Compute s = (r + k * scalar) mod L
    scalar_int = bytes_to_int_le(scalar)
    s = (r + k * scalar_int) % L
    s_bytes = int_to_bytes_le(s, 32)
    
    # Step 5: Signature is R || s
    return R + s_bytes


def read_private_key_file(key_file_path: str) -> Optional[str]:
    """Read a private key from a file (64-byte hex format for orlp/ed25519).
    
    Args:
        key_file_path: Path to the private key file.
        
    Returns:
        Optional[str]: Private key as hex string (128 hex chars = 64 bytes), or None if invalid.
    """
    if not os.path.exists(key_file_path):
        return None
    
    try:
        with open(key_file_path, 'r') as f:
            key = f.read().strip()
            # Remove whitespace
            key = ''.join(key.split())
            # Remove 0x prefix if present
            key = key.replace('0x', '').replace('0X', '')
            
            # Should be 128 hex chars (64 bytes) for orlp format
            if len(key) == 128:
                # Validate it's valid hex
                int(key, 16)
                return key.lower()
            # Try 64 hex chars (32 bytes) - convert to orlp format if needed
            elif len(key) == 64:
                # This is a seed - we'd need to expand it, but for now just return as-is
                # The caller should handle expansion
                return key.lower()
            else:
                logger.debug(f"Private key file has wrong length: {len(key)} (expected 64 or 128 hex chars)")
                return None
    except Exception as e:
        logger.debug(f"Error reading private key file: {e}")
        return None


async def _create_auth_token_with_device(
    payload_dict: Dict[str, Any],
    public_key_hex: str,
    meshcore_instance: Any,
    chunk_size: int = 120
) -> str:
    """Create auth token using on-device signing via meshcore.commands.sign().
    
    Args:
        payload_dict: Token payload as dictionary.
        public_key_hex: Public key in hex (for verification).
        meshcore_instance: Connected MeshCore instance.
        chunk_size: Maximum chunk size for signing (device may have limits).
        
    Returns:
        str: JWT-style token string (header.payload.signature).
        
    Raises:
        ImportError: If meshcore package is missing.
        Exception: If device is not connected or signing fails.
    """
    try:
        from meshcore import EventType
    except ImportError:
        # Fallback if meshcore not available
        raise ImportError("meshcore package required for on-device signing")
    
    if not meshcore_instance or not meshcore_instance.is_connected:
        raise Exception("MeshCore device not connected")
    
    if not hasattr(meshcore_instance, 'commands') or not hasattr(meshcore_instance.commands, 'sign'):
        raise Exception("Device does not support signing (meshcore.commands.sign not available)")
    
    # IMPORTANT: The device signs with self_id.sign(), which uses the device's LocalIdentity
    # The LocalIdentity has its own pub_key and prv_key that may differ from the exported key.
    # We MUST use the device's actual signing public key (from self_id) in the JWT payload
    # so the signature will verify correctly.
    device_signing_public_key = public_key_hex  # Default to provided key
    if hasattr(meshcore_instance, 'self_info') and meshcore_instance.self_info:
        device_info_public_key = meshcore_instance.self_info.get('public_key', '')
        if device_info_public_key:
            # Normalize to hex string if it's bytes
            if isinstance(device_info_public_key, bytes):
                device_info_public_key = device_info_public_key.hex()
            elif isinstance(device_info_public_key, bytearray):
                device_info_public_key = bytes(device_info_public_key).hex()
            device_signing_public_key = device_info_public_key.upper()
            if device_signing_public_key != public_key_hex.upper():
                logger.debug("⚠️  Device's self_id public key differs from provided key")
                logger.debug("  Using device's self_id public key for JWT payload (required for verification)")
                logger.debug(f"  Device signing key (self_id): {device_signing_public_key[:32]}...")
                logger.debug(f"  Provided key: {public_key_hex[:32]}...")
            else:
                logger.debug("✓ Device's self_id public key matches provided key")
        else:
            logger.debug("⚠️  Could not get device's self_info public_key, using provided key")
    else:
        logger.debug("⚠️  Could not get device's self_info, using provided public key")
        logger.debug("  If signature doesn't verify, device may be using different key")
    
    # Update payload with the device's actual signing public key (from self_id)
    # This is critical: the signature was created with self_id's private key,
    # so it will only verify with self_id's public key
    payload_dict['publicKey'] = device_signing_public_key
    
    # Create JWT header
    header = {
        'alg': 'Ed25519',
        'typ': 'JWT'
    }
    
    # Encode header and payload
    header_json = json.dumps(header, separators=(',', ':'))
    payload_json = json.dumps(payload_dict, separators=(',', ':'))
    
    # Base64url encode
    header_encoded = base64url_encode(header_json.encode('utf-8'))
    payload_encoded = base64url_encode(payload_json.encode('utf-8'))
    
    # Create signing input (what we'll sign)
    signing_input = f"{header_encoded}.{payload_encoded}"
    signing_input_bytes = signing_input.encode('utf-8')
    
    # Check if message needs to be chunked
    if len(signing_input_bytes) > chunk_size:
        # Device may have signing size limits - chunk the message
        # For now, we'll try full message first
        logger.debug(f"Signing input is {len(signing_input_bytes)} bytes (chunk_size: {chunk_size})")
    
    # Request device to sign
    try:
        result = await meshcore_instance.commands.sign(signing_input_bytes)
        
        if result.type == EventType.ERROR:
            error_payload = result.payload if hasattr(result, 'payload') else {}
            error_reason = error_payload.get('reason', 'unknown')
            raise Exception(f"Device signing failed: {error_reason}")
        
        # Check for SIGNATURE event type (or OK with signature in payload)
        signature_bytes = None
        if result.type == EventType.SIGNATURE:
            # Get signature from result
            signature_bytes = result.payload.get('signature')
        elif result.type == EventType.OK:
            # Some devices return OK with signature in payload
            signature_bytes = result.payload.get('signature')
        else:
            # Try to get signature from payload regardless of event type
            signature_bytes = result.payload.get('signature') if hasattr(result, 'payload') else None
            if not signature_bytes:
                raise Exception(f"Unexpected response type from device: {result.type}")
        
        if not signature_bytes:
            raise Exception("Device returned empty signature")
        
        # Convert signature to hex if it's bytes
        if isinstance(signature_bytes, bytes):
            signature_hex = bytes_to_hex(signature_bytes)
        elif isinstance(signature_bytes, bytearray):
            signature_hex = bytes_to_hex(bytes(signature_bytes))
        elif isinstance(signature_bytes, str):
            signature_hex = signature_bytes.replace('0x', '').replace(' ', '').lower()
        else:
            raise Exception(f"Unexpected signature type: {type(signature_bytes)}")
        
        # Return JWT format: header.payload.signature
        token = f"{header_encoded}.{payload_encoded}.{signature_hex}"
        
        # Debug: Log JWT contents
        if logger.isEnabledFor(logging.DEBUG):
            try:
                # Decode header and payload for debugging
                header_decoded = base64url_decode(header_encoded).decode('utf-8')
                payload_decoded = base64url_decode(payload_encoded).decode('utf-8')
                logger.debug(f"JWT Header: {header_decoded}")
                logger.debug(f"JWT Payload: {payload_decoded}")
                logger.debug(f"JWT Signature (hex): {signature_hex[:32]}...{signature_hex[-32:]}")
            except Exception as e:
                logger.debug(f"Could not decode JWT for logging: {e}")
        
        return token
    except Exception as e:
        logger.debug(f"Device signing error: {e}")
        raise


def _create_auth_token_python(
    payload_dict: Dict[str, Any],
    private_key_hex: str,
    public_key_hex: str
) -> str:
    """Create auth token using Python signing (PyNaCl).
    
    Args:
        payload_dict: Token payload as dictionary.
        private_key_hex: 64-byte private key in hex (orlp format: scalar || prefix).
        public_key_hex: 32-byte public key in hex.
        
    Returns:
        str: JWT-style token string (header.payload.signature).
        
    Raises:
        ImportError: If PyNaCl is required but missing.
        ValueError: If key lengths are invalid.
    """
    if not PYNACL_AVAILABLE:
        raise ImportError("PyNaCl is required for Python signing. Install with: pip install pynacl")
    
    # Create JWT header
    header = {
        'alg': 'Ed25519',
        'typ': 'JWT'
    }
    
    # Encode header and payload
    header_json = json.dumps(header, separators=(',', ':'))
    payload_json = json.dumps(payload_dict, separators=(',', ':'))
    
    # Base64url encode
    header_encoded = base64url_encode(header_json.encode('utf-8'))
    payload_encoded = base64url_encode(payload_json.encode('utf-8'))
    
    # Create signing input
    signing_input = f"{header_encoded}.{payload_encoded}"
    signing_input_bytes = signing_input.encode('utf-8')
    
    # Parse keys
    private_bytes = hex_to_bytes(private_key_hex)
    public_bytes = hex_to_bytes(public_key_hex)
    
    if len(private_bytes) != 64:
        raise ValueError(f"Private key must be 64 bytes (orlp format), got {len(private_bytes)}")
    
    if len(public_bytes) != 32:
        raise ValueError(f"Public key must be 32 bytes, got {len(public_bytes)}")
    
    # Extract scalar and prefix from orlp private key
    scalar = private_bytes[:32]
    prefix = private_bytes[32:64]
    
    # Sign using Ed25519 with expanded key
    signature_bytes = ed25519_sign_with_expanded_key(
        signing_input_bytes,
        scalar,
        prefix,
        public_bytes
    )
    
    # Convert signature to hex
    signature_hex = bytes_to_hex(signature_bytes)
    
    # Return JWT format
    token = f"{header_encoded}.{payload_encoded}.{signature_hex}"
    
    # Debug: Log JWT contents
    if logger.isEnabledFor(logging.DEBUG):
        try:
            # Decode header and payload for debugging
            header_decoded = base64url_decode(header_encoded).decode('utf-8')
            payload_decoded = base64url_decode(payload_encoded).decode('utf-8')
            logger.debug(f"JWT Header: {header_decoded}")
            logger.debug(f"JWT Payload: {payload_decoded}")
            logger.debug(f"JWT Signature (hex): {signature_hex[:32]}...{signature_hex[-32:]}")
        except Exception as e:
            logger.debug(f"Could not decode JWT for logging: {e}")
    
    return token


async def _fetch_private_key_from_device(meshcore_instance: Any) -> Optional[str]:
    """Attempt to export private key from device.
    
    Args:
        meshcore_instance: Connected MeshCore instance.
        
    Returns:
        Optional[str]: Private key as hex string (128 hex chars), or None if not available.
    """
    if not meshcore_instance or not meshcore_instance.is_connected:
        return None
    
    try:
        from meshcore import EventType
        
        if hasattr(meshcore_instance, 'commands') and hasattr(meshcore_instance.commands, 'export_private_key'):
            logger.debug("Attempting to export private key from device...")
            result = await meshcore_instance.commands.export_private_key()
            
            if result.type == EventType.PRIVATE_KEY:
                device_private_key = result.payload.get("private_key")
                if device_private_key:
                    # Convert to hex string if it's bytes
                    if isinstance(device_private_key, bytes):
                        device_private_key = bytes_to_hex(device_private_key)
                    elif isinstance(device_private_key, bytearray):
                        device_private_key = bytes_to_hex(bytes(device_private_key))
                    elif isinstance(device_private_key, str):
                        device_private_key = device_private_key.replace('0x', '').replace(' ', '').lower()
                    
                    # Validate length (should be 128 hex chars = 64 bytes)
                    if len(device_private_key) == 128:
                        logger.debug("✓ Successfully exported private key from device")
                        return device_private_key
                    elif len(device_private_key) == 64:
                        # 32-byte seed - would need expansion, but return as-is
                        logger.debug("Device returned 32-byte seed (may need expansion)")
                        return device_private_key
                    else:
                        logger.debug(f"Exported private key has wrong length: {len(device_private_key)}")
            elif result.type == EventType.DISABLED:
                logger.debug("Private key export is disabled on device")
            elif result.type == EventType.ERROR:
                logger.debug(f"Device returned error when exporting private key: {result.payload}")
    except Exception as e:
        logger.debug(f"Failed to export private key from device: {e}")
    
    return None


async def create_auth_token_async(
    meshcore_instance: Optional[Any] = None,
    public_key_hex: Optional[str] = None,
    private_key_hex: Optional[str] = None,
    iata: str = "LOC",
    timestamp: Optional[int] = None,
    audience: Optional[str] = None,
    exp: Optional[int] = None,
    owner_public_key: Optional[str] = None,
    owner_email: Optional[str] = None,
    use_device: bool = True
) -> str:
    """Create a JWT-style authentication token for MQTT authentication.
    
    Supports on-device signing (preferred) with fallback to Python signing.
    
    Args:
        meshcore_instance: Optional connected MeshCore instance for on-device signing.
        public_key_hex: Public key in hex (required).
        private_key_hex: Private key in hex (64 bytes = 128 hex chars, orlp format).
                         Required if meshcore_instance not available or device signing fails.
        iata: IATA code (default: "LOC").
        timestamp: Unix timestamp for 'iat' claim (default: current time).
        audience: Optional audience for token (e.g., MQTT broker hostname).
        exp: Optional expiration time (Unix timestamp).
        owner_public_key: Optional owner public key.
        owner_email: Optional owner email.
        use_device: If True, try on-device signing first (default: True).
        
    Returns:
        str: JWT-style token string (header.payload.signature).
        
    Raises:
        ValueError: If public_key_hex is missing or private key is missing for Python signing.
    """
    if timestamp is None:
        timestamp = int(time.time())
    
    # Get public key from device if not provided
    if not public_key_hex and meshcore_instance and hasattr(meshcore_instance, 'self_info'):
        try:
            self_info = meshcore_instance.self_info
            if isinstance(self_info, dict):
                public_key_hex = self_info.get('public_key', '')
            elif hasattr(self_info, 'public_key'):
                public_key_hex = self_info.public_key
            
            # Convert to hex if needed
            if isinstance(public_key_hex, bytes):
                public_key_hex = bytes_to_hex(public_key_hex)
            elif isinstance(public_key_hex, bytearray):
                public_key_hex = bytes_to_hex(bytes(public_key_hex))
        except Exception as e:
            logger.debug(f"Could not get public key from device: {e}")
    
    if not public_key_hex:
        raise ValueError("public_key_hex is required")
    
    # Normalize public key (remove 0x, whitespace, uppercase)
    public_key_hex = public_key_hex.replace('0x', '').replace(' ', '').upper()
    
    # Create JWT payload
    # Default expiration: 24 hours from now (86400 seconds)
    if exp is None:
        exp = timestamp + 86400
    
    payload_dict = {
        'publicKey': public_key_hex,
        'iat': timestamp,
        'exp': exp
    }
    
    if audience:
        payload_dict['aud'] = audience
    
    # Add owner information if provided (using 'owner' and 'email' field names to match original)
    if owner_public_key:
        # Normalize owner public key (remove 0x, whitespace, uppercase)
        owner_pubkey_clean = owner_public_key.replace('0x', '').replace(' ', '').upper()
        payload_dict['owner'] = owner_pubkey_clean
    
    if owner_email:
        # Normalize email to lowercase (matches original script)
        payload_dict['email'] = owner_email.lower()
    
    # Try on-device signing first if available
    if use_device and meshcore_instance and meshcore_instance.is_connected:
        try:
            if hasattr(meshcore_instance, 'commands') and hasattr(meshcore_instance.commands, 'sign'):
                logger.debug("Using on-device signing for auth token")
                return await _create_auth_token_with_device(
                    payload_dict,
                    public_key_hex,
                    meshcore_instance
                )
        except Exception as device_error:
            logger.debug(f"On-device signing failed: {device_error}, falling back to Python signing")
            # Fall through to Python signing
    
    # Fallback to Python signing
    if not private_key_hex:
        # Try to fetch from device
        if meshcore_instance:
            private_key_hex = await _fetch_private_key_from_device(meshcore_instance)
        
        # Try to read from file if still not available
        if not private_key_hex:
            private_key_file = os.getenv('PACKETCAPTURE_PRIVATE_KEY_FILE') or os.getenv('PRIVATE_KEY_FILE')
            if private_key_file:
                private_key_hex = read_private_key_file(private_key_file)
    
    if not private_key_hex:
        raise ValueError(
            "private_key_hex is required for Python signing. "
            "Either provide private_key_hex, set PACKETCAPTURE_PRIVATE_KEY_FILE, "
            "or ensure device supports on-device signing."
        )
    
    # Normalize private key
    private_key_hex = private_key_hex.replace('0x', '').replace(' ', '').lower()
    
    # If 64 hex chars (32 bytes), we need the full 64-byte orlp format
    # For now, assume it's already in orlp format (128 hex chars)
    if len(private_key_hex) == 64:
        logger.warning("Private key is 32 bytes - may need expansion to orlp format (64 bytes)")
        # Could expand here if needed, but for now assume caller provides full key
    
    logger.debug("Using Python signing for auth token")
    return _create_auth_token_python(payload_dict, private_key_hex, public_key_hex)


# Legacy synchronous function (for backward compatibility)
def create_auth_token(
    private_key_hex: str,
    public_key_hex: str,
    iata: str = "LOC",
    timestamp: Optional[int] = None,
    audience: Optional[str] = None
) -> str:
    """Synchronous version of create_auth_token (Python signing only).
    
    Args:
        private_key_hex: Private key in hex (64 bytes = 128 hex chars, orlp format).
        public_key_hex: Public key in hex (32 bytes = 64 hex chars).
        iata: IATA code (default: "LOC").
        timestamp: Unix timestamp (default: current time).
        audience: Optional audience for token.
        
    Returns:
        str: JWT-style token string (header.payload.signature).
    """
    if timestamp is None:
        timestamp = int(time.time())
    
    payload_dict = {
        'publicKey': public_key_hex.replace('0x', '').replace(' ', '').upper(),
        'iat': timestamp
    }
    
    if audience:
        payload_dict['aud'] = audience
    
    return _create_auth_token_python(
        payload_dict,
        private_key_hex.replace('0x', '').replace(' ', '').lower(),
        public_key_hex.replace('0x', '').replace(' ', '').upper()
    )

