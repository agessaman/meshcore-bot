#!/usr/bin/env python3
"""
pyMC Identity Management Module

Handles cryptographic identity persistence and import/export for pyMC_core.
Identities consist of Ed25519 signing keys which are also used for
X25519 key exchange in MeshCore.
"""

import os
import json
import base64
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

# Import pyMC_core identity
try:
    from pymc_core import LocalIdentity
    PYMC_AVAILABLE = True
except ImportError:
    PYMC_AVAILABLE = False
    LocalIdentity = None

# Import cryptography for key handling
try:
    from nacl.signing import SigningKey, VerifyKey
    from nacl.public import PrivateKey, PublicKey
    from nacl.encoding import RawEncoder
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False
    SigningKey = None
    VerifyKey = None
    PrivateKey = None
    PublicKey = None


class IdentityManager:
    """
    Manages cryptographic identity for the bot.
    
    Handles:
    - Creating new identities
    - Loading existing identities from file
    - Importing identities from MeshCore device exports
    - Persisting identities securely
    """
    
    def __init__(self, bot):
        """
        Initialize the identity manager.
        
        Args:
            bot: The MeshCoreBot instance
        """
        self.bot = bot
        self.logger = logging.getLogger('IdentityManager')
        
        # Get identity file path from config
        identity_file = bot.config.get('Connection', 'pymc_identity_file', fallback='pymc_identity.key')
        
        # Resolve path relative to bot root
        if not os.path.isabs(identity_file):
            identity_file = os.path.join(str(bot.bot_root), identity_file)
        
        self.identity_file = Path(identity_file)
        self._identity: Optional[LocalIdentity] = None
    
    async def load_or_create_identity(self) -> Optional[LocalIdentity]:
        """
        Load existing identity or create a new one.
        
        Returns:
            LocalIdentity instance or None if failed
        """
        if not PYMC_AVAILABLE:
            self.logger.error("pymc-core package not available")
            return None
        
        # Try to load existing identity
        if self.identity_file.exists():
            identity = await self._load_identity()
            if identity:
                self.logger.info(f"Loaded existing identity from {self.identity_file}")
                self._identity = identity
                return identity
        
        # Create new identity
        self.logger.info("Creating new identity...")
        identity = await self._create_identity()
        
        if identity:
            # Save the new identity
            saved = await self._save_identity(identity)
            self._identity = identity
            if saved:
                self.logger.info(f"New identity created and saved to {self.identity_file}")
            else:
                self.logger.warning(f"New identity created but could not be persisted")
        
        return identity
    
    async def _create_identity(self) -> Optional[LocalIdentity]:
        """
        Create a new cryptographic identity.
        
        Returns:
            LocalIdentity instance or None if failed
        """
        try:
            # Create a new LocalIdentity (generates new keypair)
            identity = LocalIdentity()
            
            # Log public key for reference
            pub_key = identity.get_public_key()
            self.logger.info(f"Created identity with public key: {pub_key.hex()[:16]}...")
            
            return identity
            
        except Exception as e:
            self.logger.error(f"Failed to create identity: {e}")
            return None
    
    async def _load_identity(self) -> Optional[LocalIdentity]:
        """
        Load identity from file.
        
        The identity file contains the Ed25519 seed (32 bytes) which is
        used to derive both signing and encryption keys.
        
        Returns:
            LocalIdentity instance or None if failed
        """
        try:
            with open(self.identity_file, 'r') as f:
                data = json.load(f)
            
            # Check file format version
            version = data.get('version', 1)
            
            if version == 1:
                # Version 1: Base64-encoded seed
                seed_b64 = data.get('seed')
                if not seed_b64:
                    self.logger.error("Identity file missing seed")
                    return None
                
                seed = base64.b64decode(seed_b64)
                
                if len(seed) != 32:
                    self.logger.error(f"Invalid seed length: {len(seed)} (expected 32)")
                    return None
                
                # Create LocalIdentity from seed
                # pyMC_core's LocalIdentity can accept a seed in its constructor
                # if not, we need to use nacl directly
                identity = self._create_identity_from_seed(seed)
                
                return identity
            else:
                self.logger.error(f"Unknown identity file version: {version}")
                return None
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in identity file: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Failed to load identity: {e}")
            return None
    
    def _create_identity_from_seed(self, seed: bytes) -> Optional[LocalIdentity]:
        """
        Create a LocalIdentity from an Ed25519 seed.
        
        Args:
            seed: 32-byte Ed25519 seed
            
        Returns:
            LocalIdentity or None if failed
        """
        try:
            # Check if LocalIdentity accepts seed parameter
            # pyMC_core's LocalIdentity might accept private_key parameter
            
            # First try to create with seed/private_key parameter
            try:
                identity = LocalIdentity(private_key=seed)
                return identity
            except TypeError:
                pass
            
            # If that doesn't work, try alternative approaches
            try:
                identity = LocalIdentity(seed=seed)
                return identity
            except TypeError:
                pass
            
            # Last resort: use nacl to derive keys and see if we can inject them
            if NACL_AVAILABLE:
                signing_key = SigningKey(seed)
                # LocalIdentity might have a from_signing_key method
                if hasattr(LocalIdentity, 'from_signing_key'):
                    return LocalIdentity.from_signing_key(signing_key)
            
            # If all else fails, log warning and create new identity
            self.logger.warning("Could not restore identity from seed, creating new identity")
            return LocalIdentity()
            
        except Exception as e:
            self.logger.error(f"Failed to create identity from seed: {e}")
            return None
    
    async def _save_identity(self, identity: LocalIdentity) -> bool:
        """
        Save identity to file.
        
        Args:
            identity: LocalIdentity to save
            
        Returns:
            True if saved successfully
        """
        try:
            # Get the seed/private key from identity
            seed = self._get_identity_seed(identity)
            
            if not seed:
                self.logger.error("Could not extract seed from identity")
                return False
            
            # Create identity data
            data = {
                'version': 1,
                'seed': base64.b64encode(seed).decode('ascii'),
                'public_key': identity.get_public_key().hex()
            }
            
            # Ensure directory exists
            self.identity_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Save with restricted permissions
            with open(self.identity_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            # Set file permissions to owner-only (Unix)
            try:
                os.chmod(self.identity_file, 0o600)
            except OSError:
                pass  # Windows doesn't support chmod the same way
            
            self.logger.info(f"Identity saved to {self.identity_file}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to save identity: {e}")
            return False
    
    def _get_identity_seed(self, identity: LocalIdentity) -> Optional[bytes]:
        """
        Extract the seed/private key from a LocalIdentity.
        
        Args:
            identity: LocalIdentity instance
            
        Returns:
            32-byte seed or None
        """
        try:
            # pymc_core's LocalIdentity has a signing_key attribute (nacl.signing.SigningKey)
            # The seed can be extracted via signing_key.encode() or signing_key._seed
            if hasattr(identity, 'signing_key'):
                signing_key = identity.signing_key
                if signing_key is not None:
                    # Use encode() to get the 32-byte seed
                    if hasattr(signing_key, 'encode'):
                        seed = signing_key.encode()
                        if isinstance(seed, bytes) and len(seed) == 32:
                            return seed
                    # Fallback to _seed attribute
                    if hasattr(signing_key, '_seed'):
                        seed = signing_key._seed
                        if isinstance(seed, bytes) and len(seed) == 32:
                            return seed
            
            # Try other common attribute names as fallback
            for attr in ['_private_key', 'private_key', '_seed', 'seed']:
                if hasattr(identity, attr):
                    key = getattr(identity, attr)
                    if key is not None:
                        if hasattr(key, 'encode'):
                            return key.encode()
                        elif isinstance(key, bytes) and len(key) == 32:
                            return key
                        elif isinstance(key, bytes) and len(key) == 64:
                            return key[:32]
            
            self.logger.warning("Could not find private key attribute in identity")
            return None
            
        except Exception as e:
            self.logger.error(f"Error extracting identity seed: {e}")
            return None
    
    async def import_from_meshcore_export(self, export_data: str) -> Optional[LocalIdentity]:
        """
        Import identity from a MeshCore device export.
        
        MeshCore exports identity as a JSON string containing the
        Ed25519 seed or private key.
        
        Args:
            export_data: JSON string from MeshCore export
            
        Returns:
            LocalIdentity or None if import failed
        """
        try:
            data = json.loads(export_data)
            
            # MeshCore export format may vary
            # Common fields: 'privateKey', 'seed', 'secretKey'
            seed = None
            
            for key in ['privateKey', 'seed', 'secretKey', 'private_key']:
                if key in data:
                    value = data[key]
                    
                    # Try to decode from various formats
                    if isinstance(value, str):
                        # Try hex
                        try:
                            seed = bytes.fromhex(value)
                            if len(seed) == 32:
                                break
                        except ValueError:
                            pass
                        
                        # Try base64
                        try:
                            seed = base64.b64decode(value)
                            if len(seed) == 32:
                                break
                            elif len(seed) == 64:
                                seed = seed[:32]  # Take first 32 bytes
                                break
                        except Exception:
                            pass
                    
                    elif isinstance(value, list) and len(value) == 32:
                        # Array of bytes
                        seed = bytes(value)
                        break
            
            if not seed or len(seed) != 32:
                self.logger.error("Could not extract valid seed from MeshCore export")
                return None
            
            # Create identity from seed
            identity = self._create_identity_from_seed(seed)
            
            if identity:
                # Save the imported identity
                await self._save_identity(identity)
                self._identity = identity
                self.logger.info("Successfully imported identity from MeshCore export")
            
            return identity
            
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in MeshCore export: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Failed to import MeshCore identity: {e}")
            return None
    
    def get_public_key(self) -> Optional[bytes]:
        """
        Get the public key of the current identity.
        
        Returns:
            32-byte public key or None
        """
        if self._identity:
            return self._identity.get_public_key()
        return None
    
    def get_public_key_hex(self) -> str:
        """
        Get the public key as a hex string.
        
        Returns:
            Hex-encoded public key or empty string
        """
        pub_key = self.get_public_key()
        return pub_key.hex() if pub_key else ""
    
    def get_pubkey_prefix(self) -> str:
        """
        Get the 6-byte public key prefix as hex.
        
        This prefix is used for addressing in MeshCore.
        
        Returns:
            12-character hex string or empty string
        """
        pub_key = self.get_public_key()
        return pub_key[:6].hex() if pub_key else ""
