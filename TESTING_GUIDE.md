# Testing Guide for PR #27 Integration

This guide covers testing the integrated PR #27 changes, including both Nix and standard Python installations.

## Quick Test Checklist

### ✅ Standard Python Installation Tests

#### 1. **Dependency Installation Test**
```bash
# Test that all dependencies can be installed
pip install -e .

# Or test with requirements.txt (should match pyproject.toml)
pip install -r requirements.txt

# Verify critical dependencies are available
python3 -c "import paho.mqtt.client; import cryptography; import nacl; print('✓ All dependencies available')"
```

#### 2. **Import Test**
```bash
# Test that the package can be imported
python3 -c "from modules.core import MeshCoreBot; print('✓ Import successful')"

# Test web viewer import
python3 -c "from modules.web_viewer.app import main; print('✓ Web viewer import successful')"
```

#### 3. **Signal Handler Test** (Critical Fix)
```bash
# Start the bot
python3 meshcore_bot.py --config config.ini.example

# In another terminal, test SIGTERM handling:
# Send SIGTERM and verify graceful shutdown
kill -TERM $(pgrep -f "meshcore_bot.py")

# Or test SIGINT (Ctrl+C) - should show "Shutting down..." and exit cleanly
# Press Ctrl+C and verify it doesn't hang or crash
```

**Expected behavior:**
- Bot should print "Shutting down..." when signal received
- Bot should call `bot.stop()` and exit cleanly
- No hanging processes or error messages

#### 4. **Web Viewer Config Argument Test**
```bash
# Test that --config argument works
python3 -m modules.web_viewer.app --config config.ini.example --help

# Should show help including --config option
```

#### 5. **Package Installation Test**
```bash
# Install as package
pip install -e .

# Test entry points
which meshcore-bot
which meshcore-viewer

# Test they work
meshcore-bot --help
meshcore-viewer --help
```

---

### ✅ Nix Installation Tests (Linux)

#### 1. **Flake Evaluation Test**
```bash
# Test that the flake evaluates correctly
# Note: If you get experimental feature errors, enable them:
nix --extra-experimental-features "nix-command flakes" flake check

# Or configure permanently in ~/.config/nix/nix.conf:
# experimental-features = nix-command flakes

# Should complete without errors
```

#### 2. **Package Build Test**
```bash
# Build the package (enable experimental features if needed)
nix --extra-experimental-features "nix-command flakes" build

# Verify the package was built
ls -la result/

# Check that translations are installed
ls -la result/share/meshcore-bot/translations/

# Should show translation JSON files
```

#### 3. **Package Contents Verification**
```bash
# After building, check package contents
nix build
nix-store -q --tree result/ | grep -E "(meshcore-bot|translations)"

# Verify entry points exist
result/bin/meshcore-bot --help
result/bin/meshcore-viewer --help
```

#### 4. **Dependency Verification**
```bash
# Check that all dependencies are included
nix build
nix-store -qR result/ | grep -E "(paho-mqtt|urllib3|cryptography|pynacl)"

# Should show these packages in the dependency tree
```

#### 5. **NixOS Module Test** (Requires NixOS or VM)
```bash
# Run the NixOS tests (this creates VMs, takes time)
nix --extra-experimental-features "nix-command flakes" flake check --no-build

# Or run specific test
nix --extra-experimental-features "nix-command flakes" build .#checks.x86_64-linux.nixos-module-basic

# This will:
# - Create a NixOS VM
# - Install meshcore-bot service
# - Verify service starts
# - Check file permissions
# - Verify translations path
```

**Note:** NixOS tests require significant resources and time. They create full VMs.

#### 6. **Development Shell Test**
```bash
# Enter development shell (enable experimental features if needed)
nix --extra-experimental-features "nix-command flakes" develop

# Verify dependencies are available
python3 -c "import paho.mqtt.client; print('✓ paho-mqtt available')"
python3 -c "import meshcore; print('✓ meshcore available')"

# Test imports
python3 -c "from modules.core import MeshCoreBot; print('✓ Import works')"
```

---

## Manual Verification Checklist

### Signal Handler Fix Verification

1. **Start bot normally:**
   ```bash
   python3 meshcore_bot.py --config config.ini.example
   ```

2. **Test SIGTERM:**
   - In another terminal: `kill -TERM <pid>`
   - Should see "Shutting down..." message
   - Bot should exit cleanly (check with `ps aux | grep meshcore`)

3. **Test SIGINT (Ctrl+C):**
   - Press Ctrl+C
   - Should see "Shutting down..." message
   - Bot should exit cleanly

4. **Verify no hanging:**
   - After shutdown, check for zombie processes
   - `ps aux | grep meshcore` should show nothing

### Dependency Verification

1. **Check pyproject.toml includes all dependencies:**
   ```bash
   grep -E "(paho-mqtt|urllib3|cryptography|pynacl)" pyproject.toml
   ```

2. **Verify Nix packages include dependencies:**
   ```bash
   grep -E "(paho-mqtt|urllib3|pynacl)" nix/packages.nix
   ```

3. **Test imports work:**
   ```bash
   python3 <<EOF
   import paho.mqtt.client
   import urllib3
   import cryptography
   import nacl
   print("✓ All new dependencies importable")
   EOF
   ```

### Translation Path Fix Verification

1. **For Nix build:**
   ```bash
   nix build
   test -d result/share/meshcore-bot/translations
   ls result/share/meshcore-bot/translations/*.json
   # Should show translation files
   ```

2. **For NixOS module:**
   - The NixOS test should verify this automatically
   - Check that `translation_path` in generated config points to correct location

### Code Quality Checks

1. **Syntax check:**
   ```bash
   python3 -m py_compile meshcore_bot.py
   python3 -m py_compile modules/web_viewer/app.py
   ```

2. **Import check:**
   ```bash
   python3 -c "import meshcore_bot; import modules.web_viewer.app"
   ```

---

## Expected Test Results

### ✅ All Tests Should Pass

1. **Standard Python:**
   - ✅ Dependencies install correctly
   - ✅ Imports work
   - ✅ Signal handler shuts down gracefully
   - ✅ Web viewer accepts --config argument
   - ✅ Entry points work

2. **Nix:**
   - ✅ Flake evaluates
   - ✅ Package builds
   - ✅ Translations installed to share/
   - ✅ All dependencies included
   - ✅ Entry points work
   - ✅ NixOS module tests pass (if run)

---

## Troubleshooting

### If signal handler test fails:
- Check that `asyncio.run()` is being used correctly
- Verify no blocking operations in signal handler
- Check for proper event loop handling

### If Nix build fails:
- **Experimental features error**: Enable with `--extra-experimental-features "nix-command flakes"` or configure in `~/.config/nix/nix.conf`:
  ```
  experimental-features = nix-command flakes
  ```
- Run `nix --extra-experimental-features "nix-command flakes" flake update` to update lock file
- Check that `flake-parts` is in inputs
- Verify all dependencies are available in nixpkgs

### If translations not found:
- Check `nix/packages.nix` postInstall hook
- Verify source path is correct
- Check that translations directory exists in source

### If dependencies missing:
- Compare `requirements.txt` with `pyproject.toml`
- Check `nix/packages.nix` propagatedBuildInputs
- Verify package names match (e.g., `paho-mqtt` vs `paho.mqtt`)

---

## Quick Test Script

Save this as `quick_test.sh`:

```bash
#!/bin/bash
set -e

echo "=== Testing Standard Python Installation ==="
python3 -c "import paho.mqtt.client; import cryptography; import nacl; print('✓ Dependencies OK')"
python3 -c "from modules.core import MeshCoreBot; print('✓ Import OK')"
python3 meshcore_bot.py --help > /dev/null && echo "✓ meshcore-bot help works"
python3 -m modules.web_viewer.app --help > /dev/null && echo "✓ web viewer help works"

echo ""
echo "=== Testing Nix (if available) ==="
if command -v nix &> /dev/null; then
    nix flake check --no-build && echo "✓ Flake evaluates"
    echo "Run 'nix build' to test package build"
else
    echo "Nix not available, skipping Nix tests"
fi

echo ""
echo "✓ All quick tests passed!"
```

Make it executable: `chmod +x quick_test.sh`
