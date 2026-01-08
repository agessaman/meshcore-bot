{
  self,
  inputs,
  ...
}: {
  imports = [
  ];
  perSystem = {
    pkgs,
    self',
    system,
    ...
  }: let
    # Fix meshcore-cli to use correct package names
    meshcore-cli-fixed = inputs.meshcore-cli.packages.${system}.meshcore-cli.overridePythonAttrs (old: {
      # Override propagatedBuildInputs to use correct nixpkgs names
      propagatedBuildInputs = with pkgs.python3Packages; [
        meshcore
        bleak
        prompt-toolkit # Fixed: was prompt_toolkit
        requests
        pycryptodome
      ];
    });

    # Custom Python packages not in nixpkgs - fetch from PyPI
    maidenhead = pkgs.python3Packages.buildPythonPackage rec {
      pname = "maidenhead";
      version = "1.7.0";
      format = "setuptools";

      src = pkgs.fetchPypi {
        inherit pname version;
        hash = "sha256-NkFdzo9jCSflkjLIjQgTUhjTKBlQs9PrXXyqBKdiP3Q=";
      };

      doCheck = false; # Skip tests for simplicity
    };

    pyephem = pkgs.python3Packages.buildPythonPackage rec {
      pname = "ephem"; # PyPI name is 'ephem', not 'pyephem'
      version = "4.1.6";
      format = "setuptools";

      src = pkgs.fetchPypi {
        inherit pname version;
        hash = "sha256-DtLk6nb52z7t4iBK2rivPxcIIBx8BO6FEecQpUymQl8=";
      };

      doCheck = false;
    };

    openmeteo-sdk = pkgs.python3Packages.buildPythonPackage rec {
      pname = "openmeteo-sdk";
      version = "1.23.0";
      pyproject = true;

      src = pkgs.fetchPypi {
        pname = "openmeteo_sdk"; # PyPI uses underscore
        inherit version;
        hash = "sha256-L4YLRjW+1azCyc7pTCLcVjrZBuPWjycNPnX8A9m0I/E=";
      };
      propagatedBuildInputs = with pkgs.python3Packages; [
        requests
        hatchling
        flatbuffers
      ];

      doCheck = false;
    };
    openmeteo-requests = pkgs.python3Packages.buildPythonPackage rec {
      pname = "openmeteo-requests";
      version = "1.7.4";
      #format = "setuptools";
      pyproject = true;

      src = pkgs.fetchPypi {
        pname = "openmeteo_requests"; # PyPI uses underscore
        inherit version;
        hash = "sha256-lJCqDvdo3cWJfVcXA2pZiXRk/gNhmt09WXyjgApwQkQ=";
      };

      propagatedBuildInputs = with pkgs.python3Packages; [
        requests
        hatchling
        niquests
        openmeteo-sdk
      ];

      doCheck = false;
    };

    retry-requests = pkgs.python3Packages.buildPythonPackage rec {
      pname = "retry-requests";
      version = "2.0.0";
      format = "setuptools";

      src = pkgs.fetchPypi {
        inherit pname version;
        hash = "sha256-PQITXlqv7fCSQEFBgvxzicXStN4CUtq6AFTJ1qJ+djk=";
      };

      # Patch to remove setup_requires if it causes issues
      patches = [
        (pkgs.writeText "remove-setup-requires.patch" ''
          --- retry-requests-2.0.0-old/setup.py   2025-12-23 14:46:45.698182443 +0100
          +++ retry-requests-2.0.0/setup.py       2025-12-23 14:47:00.302294857 +0100
          @@ -23,7 +23,6 @@
               install_requires=["requests", "urllib3>=1.26"],
               extras_require={"test": test_requires},
               tests_require=test_requires,
          -    setup_requires=["pytest-runner"],
               classifiers=[
                   "Development Status :: 5 - Production/Stable",
                   "Intended Audience :: Developers",
        '')
      ];

      propagatedBuildInputs = with pkgs.python3Packages; [
        requests
      ];

      doCheck = false;
    };
  in {
    # Package definitions
    packages.default = pkgs.python3Packages.buildPythonPackage {
      name = "meshcore-bot";
      src = ./..;
      pyproject = true;

      nativeBuildInputs = with pkgs.python3Packages; [setuptools];
      
      # Install translations to share directory for NixOS module compatibility
      # Translations are in the source root, copy them to the standard location
      postInstall = ''
        mkdir -p $out/share/meshcore-bot
        if [ -d "$src/translations" ]; then
          cp -r "$src/translations" $out/share/meshcore-bot/
        fi
      '';
      
      propagatedBuildInputs =
        (with pkgs.python3Packages; [
          # Core Python dependencies from requirements.txt
          aiohttp
          aiohttp-retry
          aiomqtt
          bleak
          colorlog
          feedparser
          flask
          flask-socketio
          geopy
          meshcore
          pyserial
          python-dateutil
          pytz
          requests
          requests-cache
          schedule
          cryptography
          paho-mqtt
          urllib3
          pynacl
        ])
        ++ [
          # Custom packages from PyPI
          maidenhead
          pyephem
          openmeteo-requests
          retry-requests
          # Fixed meshcore-cli
          meshcore-cli-fixed
        ];

      # Allow pip to fetch packages not available in nixpkgs
      dontCheckRuntimeDeps = true;

      meta = {
        description = "A Python bot that connects to MeshCore mesh networks via serial port, BLE, or TCP/IP.";
        mainProgram = "meshcore-bot";
        license = pkgs.lib.licenses.mit;
        homepage = "https://github.com/agessaman/meshcore-bot/";
      };
    };
  };
}
