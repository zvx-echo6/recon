"""
RECON Key Manager - Thread-safe API key management with hot-reload.

Provides a singleton KeyManager that workers (enricher, extractor) read from
instead of loading .env directly. Dashboard can update keys at runtime without
restarting the service.

Dependencies: None beyond stdlib + requests (already in requirements.txt)
Config: Reads/writes /opt/recon/.env
"""

import os
import re
import time
import logging
import threading
import requests

logger = logging.getLogger('recon.key_manager')

class KeyManager:
    """Thread-safe API key store with hot-reload and validation."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._keys_lock = threading.RLock()
        self._gemini_keys = []
        self._env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
        self._last_loaded = None
        self._key_stats = {}  # key_index -> {calls, errors, last_used}
        self._load_from_env()
        self._initialized = True
        logger.info(f"KeyManager initialized with {len(self._gemini_keys)} Gemini key(s)")
    
    # ── Read Operations ──
    
    def get_gemini_keys(self):
        """Return a copy of current Gemini keys. Thread-safe."""
        with self._keys_lock:
            return list(self._gemini_keys)
    
    def get_gemini_key(self, index=0):
        """Get a single Gemini key by index. Returns None if out of range."""
        with self._keys_lock:
            if 0 <= index < len(self._gemini_keys):
                return self._gemini_keys[index]
            return None
    
    def get_gemini_key_count(self):
        """Return number of loaded Gemini keys."""
        with self._keys_lock:
            return len(self._gemini_keys)
    
    def get_masked_keys(self):
        """Return keys masked for display: first 8 + ... + last 4 chars."""
        with self._keys_lock:
            result = []
            for i, key in enumerate(self._gemini_keys):
                if len(key) > 16:
                    masked = key[:8] + '...' + key[-4:]
                elif len(key) > 8:
                    masked = key[:4] + '...' + key[-2:]
                else:
                    masked = '****'
                stats = self._key_stats.get(i, {})
                result.append({
                    'index': i,
                    'masked': masked,
                    'length': len(key),
                    'calls': stats.get('calls', 0),
                    'errors': stats.get('errors', 0),
                    'last_used': stats.get('last_used', None),
                    'valid': stats.get('valid', None),
                    'last_validated': stats.get('last_validated', None),
                })
            return result
    
    # ── Write Operations (all persist to .env) ──
    
    def set_gemini_keys(self, keys):
        """Replace all Gemini keys. Persists to .env. Returns success bool."""
        # Filter empty strings
        keys = [k.strip() for k in keys if k.strip()]
        with self._keys_lock:
            self._gemini_keys = keys
            self._key_stats = {}  # Reset stats on full replace
            self._persist_to_env()
            logger.info(f"Gemini keys replaced: {len(keys)} key(s) loaded")
        return True
    
    def add_gemini_key(self, key):
        """Add a single Gemini key. Persists to .env. Returns new index."""
        key = key.strip()
        if not key:
            raise ValueError("Key cannot be empty")
        with self._keys_lock:
            # Check for duplicates
            if key in self._gemini_keys:
                raise ValueError("Key already exists")
            self._gemini_keys.append(key)
            idx = len(self._gemini_keys) - 1
            self._persist_to_env()
            logger.info(f"Gemini key added at index {idx}")
            return idx
    
    def remove_gemini_key(self, index):
        """Remove a Gemini key by index. Persists to .env. Returns removed key (masked)."""
        with self._keys_lock:
            if index < 0 or index >= len(self._gemini_keys):
                raise IndexError(f"Key index {index} out of range (have {len(self._gemini_keys)} keys)")
            if len(self._gemini_keys) <= 1:
                raise ValueError("Cannot remove last key — pipeline needs at least 1 Gemini key")
            key = self._gemini_keys.pop(index)
            # Rebuild stats with shifted indices
            new_stats = {}
            for i, stats in self._key_stats.items():
                if i < index:
                    new_stats[i] = stats
                elif i > index:
                    new_stats[i - 1] = stats
            self._key_stats = new_stats
            self._persist_to_env()
            masked = key[:8] + '...' + key[-4:] if len(key) > 16 else '****'
            logger.info(f"Gemini key removed at index {index}: {masked}")
            return masked
    
    def replace_gemini_key(self, index, new_key):
        """Replace a single Gemini key at index. Persists to .env."""
        new_key = new_key.strip()
        if not new_key:
            raise ValueError("Key cannot be empty")
        with self._keys_lock:
            if index < 0 or index >= len(self._gemini_keys):
                raise IndexError(f"Key index {index} out of range")
            # Check duplicate (but allow replacing with same key)
            if new_key in self._gemini_keys and self._gemini_keys[index] != new_key:
                raise ValueError("Key already exists at another index")
            self._gemini_keys[index] = new_key
            if index in self._key_stats:
                self._key_stats[index] = {}  # Reset stats for replaced key
            self._persist_to_env()
            logger.info(f"Gemini key replaced at index {index}")
    
    # ── Validation ──
    
    def validate_key(self, key):
        """
        Test a Gemini API key by listing models.
        Returns (valid: bool, message: str).
        """
        try:
            resp = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
                timeout=10
            )
            if resp.status_code == 200 and 'models' in resp.text:
                return True, "Valid — API responded"
            elif resp.status_code == 400:
                return False, f"Invalid key (HTTP {resp.status_code})"
            elif resp.status_code == 403:
                return False, "Key disabled or quota exhausted"
            elif resp.status_code == 429:
                return True, "Valid — but currently rate-limited"
            else:
                return False, f"Unexpected response (HTTP {resp.status_code})"
        except requests.Timeout:
            return False, "Timeout — could not reach Gemini API"
        except requests.ConnectionError:
            return False, "Connection error — check network"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def validate_all(self):
        """Validate all loaded Gemini keys. Returns list of results."""
        results = []
        with self._keys_lock:
            keys_copy = list(enumerate(self._gemini_keys))
        
        for i, key in keys_copy:
            valid, message = self.validate_key(key)
            with self._keys_lock:
                if i not in self._key_stats:
                    self._key_stats[i] = {}
                self._key_stats[i]['valid'] = valid
                self._key_stats[i]['last_validated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            results.append({'index': i, 'valid': valid, 'message': message})
            time.sleep(0.2)  # Don't hammer the API
        
        return results
    
    # ── Stats tracking (called by enricher/extractor) ──
    
    def record_usage(self, key_index, success=True):
        """Record a key usage event. Called by workers after each Gemini call."""
        with self._keys_lock:
            if key_index not in self._key_stats:
                self._key_stats[key_index] = {'calls': 0, 'errors': 0}
            self._key_stats[key_index]['calls'] = self._key_stats[key_index].get('calls', 0) + 1
            if not success:
                self._key_stats[key_index]['errors'] = self._key_stats[key_index].get('errors', 0) + 1
            self._key_stats[key_index]['last_used'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    
    # ── Internal ──
    
    def _load_from_env(self):
        """Load Gemini keys from .env file."""
        keys = []
        if os.path.exists(self._env_path):
            with open(self._env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        match = re.match(r'^GEMINI_KEY(?:_\d+)?=(.+)$', line)
                        if match:
                            val = match.group(1).strip().strip('"').strip("'")
                            if val:
                                keys.append(val)
        self._gemini_keys = keys
        self._last_loaded = time.time()
    
    def _persist_to_env(self):
        """Write current keys back to .env file, preserving non-Gemini lines."""
        other_lines = []
        if os.path.exists(self._env_path):
            with open(self._env_path, 'r') as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not re.match(r'^GEMINI_KEY', stripped):
                        other_lines.append(line.rstrip('\n'))
        
        with open(self._env_path, 'w') as f:
            # Write non-Gemini lines first
            for line in other_lines:
                f.write(line + '\n')
            # Write Gemini keys
            for i, key in enumerate(self._gemini_keys, 1):
                f.write(f'GEMINI_KEY_{i}={key}\n')
        
        self._last_loaded = time.time()
        logger.info(f"Persisted {len(self._gemini_keys)} Gemini key(s) to {self._env_path}")
    
    def reload_from_env(self):
        """Force reload from .env (e.g., if edited externally)."""
        with self._keys_lock:
            self._load_from_env()
            logger.info(f"Reloaded {len(self._gemini_keys)} Gemini key(s) from .env")
        return len(self._gemini_keys)


# Module-level convenience — import and use anywhere
_manager = None

def get_key_manager():
    """Get the singleton KeyManager instance."""
    global _manager
    if _manager is None:
        _manager = KeyManager()
    return _manager
