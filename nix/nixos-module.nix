{
  self,
  inputs,
  ...
}: {
  flake.nixosModules.default = {
    config,
    lib,
    pkgs,
    ...
  }: let
    cfg = config.services.meshcore-bot;

    # Helper function to convert nested attrset to INI format
    toINI = lib.generators.toINI {
      mkKeyValue = lib.generators.mkKeyValueDefault {
        mkValueString = v:
          if builtins.isBool v
          then
            (
              if v
              then "true"
              else "false"
            )
          else if builtins.isString v
          then v
          else if builtins.isInt v
          then toString v
          else if builtins.isFloat v
          then toString v
          else if builtins.isList v
          then lib.concatStringsSep "," v
          else throw "Unsupported type for INI value: ${builtins.typeOf v}";
      } " = ";
    };

    # Generate config file from settings
    configFile = pkgs.writeText "meshcore-bot-config.ini" (toINI cfg.settings);
  in {
    options.services.meshcore-bot = {
      enable = lib.mkEnableOption "MeshCore Bot service";

      package = lib.mkOption {
        type = lib.types.package;
        default = inputs.self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        defaultText = lib.literalExpression "inputs.self.packages.\${pkgs.stdenv.hostPlatform.system}.meshcore-bot";
        description = "The meshcore-bot package to use.";
      };

      webviewer = {
        enable = lib.mkEnableOption "MeshCore Bot Web Viewer service";

        host = lib.mkOption {
          type = lib.types.str;
          default = "127.0.0.1";
          description = "Host to bind the web viewer to.";
        };

        port = lib.mkOption {
          type = lib.types.int;
          default = 8080;
          description = "Port to bind the web viewer to.";
        };
      };

      settings = lib.mkOption {
        type = lib.types.attrsOf (lib.types.attrsOf lib.types.anything);
        default = {};
        example = lib.literalExpression ''
          {
            Connection = {
              connection_type = "serial";
              serial_port = "/dev/ttyUSB0";
              timeout = 30;
            };
            Bot = {
              bot_name = "MeshCoreBot";
              enabled = true;
            };
          }
        '';
        description = ''
          Configuration for meshcore-bot in INI format.
        '';
        apply = userSettings: let
          # Default settings that will be merged with user settings
          defaults = {
            Bot = {
              db_path = "${cfg.dataDir}/meshcore-bot.db";
            };
            Localization = {
              # Translation path: translations are installed to share directory by Nix package
              translation_path = "${cfg.package}/share/meshcore-bot/translations";
            };
            Logging = {
              log_file = "/var/log/meshcore-bot/meshcore-bot.log";
              colored_output = false;
            };
            Security = {
              allow_absolute_paths = true;
            };
            Web_Viewer = {
              enabled = false;
              db_path = "${cfg.dataDir}/meshcore-bot.db";
              auto_start = false; #We start it in a seperate unit
              host = cfg.webviewer.host;
              port = cfg.webviewer.port;
            };
          };
        in
          # Recursively merge defaults with user settings, user settings take precedence
          lib.recursiveUpdate defaults userSettings;
      };

      dataDir = lib.mkOption {
        type = lib.types.path;
        default = "/var/lib/meshcore-bot";
        description = ''
          Directory where meshcore-bot stores its database and persistent data.
          This directory will be created automatically with appropriate permissions.
        '';
      };
    };

    config = lib.mkIf cfg.enable {
      # Create user and group
      users.users.meshcore-bot = {
        isSystemUser = true;
        group = "meshcore-bot";
        description = "MeshCore Bot service user";
        home = cfg.dataDir;
        # Grant access to serial ports
        extraGroups = ["dialout"];
      };

      users.groups.meshcore-bot = {};

      # Create systemd service
      systemd.services.meshcore-bot = {
        description = "MeshCore Bot - Mesh network bot for MeshCore devices";
        after = ["network.target"];
        wantedBy = ["multi-user.target"];

        # Service configuration
        serviceConfig = {
          Type = "simple";
          User = "meshcore-bot";
          Group = "meshcore-bot";

          # Working directory - systemd will create it automatically
          WorkingDirectory = cfg.dataDir;

          # StateDirectory creates /var/lib/meshcore-bot with correct ownership
          # This is the NixOS/systemd way to manage service state directories
          StateDirectory = "meshcore-bot";
          StateDirectoryMode = "0750";

          # LogsDirectory creates /var/log/meshcore-bot with correct ownership
          LogsDirectory = "meshcore-bot";
          LogsDirectoryMode = "0750";

          # Start command
          ExecStart = "${cfg.package}/bin/meshcore-bot --config ${configFile}";

          # Restart policy
          Restart = "on-failure";
          RestartSec = "10s";

          # Security hardening
          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;

          # Additional hardening
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictAddressFamilies = ["AF_UNIX" "AF_INET" "AF_INET6" "AF_BLUETOOTH"];
          RestrictNamespaces = true;
          LockPersonality = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          RemoveIPC = true;
          PrivateMounts = true;
        };

        # Environment
        environment = {
          PYTHONUNBUFFERED = "1";
        };
      };

      # Install package data (translations, templates, etc.)
      environment.systemPackages = [cfg.package];

      # Web viewer service
      systemd.services.meshcore-bot-viewer = lib.mkIf cfg.webviewer.enable {
        description = "MeshCore Bot Web Viewer - Web interface for MeshCore Bot";
        after = ["network.target" "meshcore-bot.service"];
        wantedBy = ["multi-user.target"];

        # Service configuration
        serviceConfig = {
          Type = "simple";
          User = "meshcore-bot";
          Group = "meshcore-bot";

          # Working directory - systemd will create it automatically
          WorkingDirectory = cfg.dataDir;

          # StateDirectory creates /var/lib/meshcore-bot with correct ownership
          # This is the NixOS/systemd way to manage service state directories
          StateDirectory = "meshcore-bot";
          StateDirectoryMode = "0750";

          # LogsDirectory creates /var/log/meshcore-bot with correct ownership
          LogsDirectory = "meshcore-bot";
          LogsDirectoryMode = "0750";

          # Start command
          ExecStart = "${cfg.package}/bin/meshcore-viewer --host ${cfg.webviewer.host} --port ${toString cfg.webviewer.port} --config ${configFile}";

          # Restart policy
          Restart = "on-failure";
          RestartSec = "10s";

          # Security hardening
          NoNewPrivileges = true;
          PrivateTmp = true;
          ProtectSystem = "strict";
          ProtectHome = true;

          # Additional hardening
          ProtectKernelTunables = true;
          ProtectKernelModules = true;
          ProtectControlGroups = true;
          RestrictAddressFamilies = ["AF_UNIX" "AF_INET" "AF_INET6"];
          RestrictNamespaces = true;
          LockPersonality = true;
          RestrictRealtime = true;
          RestrictSUIDSGID = true;
          RemoveIPC = true;
          PrivateMounts = true;
        };

        # Environment
        environment = {
          PYTHONUNBUFFERED = "1";
        };
      };
    };
  };
}
