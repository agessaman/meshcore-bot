{
  self,
  inputs,
  ...
}: {
  perSystem = {
    config,
    lib,
    pkgs,
    system,
    ...
  }: {
    checks = {
      nixos-module-basic = pkgs.testers.nixosTest {
        name = "meshcore-bot-basic";

        nodes.machine = {
          imports = [self.nixosModules.default];

          services.meshcore-bot = {
            enable = true;
            settings = {
              Connection = {
                connection_type = "tcp";
                hostname = "localhost";
                tcp_port = 5000;
                timeout = 30;
              };
              Bot = {
                bot_name = "TestBot";
                enabled = true;
              };
              Channels = {
                monitor_channels = ["general" "test"];
                respond_to_dms = true;
              };
              Logging = {
                log_level = "DEBUG";
              };
            };
          };
          environment.systemPackages = [
            pkgs.sqlite
          ];
        };

        testScript = ''
          machine.wait_for_unit("meshcore-bot.service")

          # Give it a few seconds to create files and attempt connection
          machine.sleep(15)

          # Check service logs first to diagnose any issues
          print("\n=== Service Logs (last 50 lines) ===")
          logs = machine.succeed("journalctl -u meshcore-bot.service --no-pager -n 50")
          print(logs)

          # Verify service is running (or attempting to run)
          # The service will fail to connect but should be in active/running or restart loop
          print("\n=== Service State ===")
          status = machine.succeed("systemctl is-active meshcore-bot.service || echo 'not-active'")
          print(f"Service status: {status}")

          # Check logs show the bot attempted to start
          machine.succeed("journalctl -u meshcore-bot.service | grep -q 'MeshCore Bot' || journalctl -u meshcore-bot.service | grep -q 'meshcore' || journalctl -u meshcore-bot.service | grep -q 'python'")
          print("✓ Bot startup logged in systemd journal")

          # Check if database file is created in the correct location
          # The database is created during bot initialization in DBManager.__init__
          # If the bot started successfully, the database should exist
          print("\n=== File Checks ===")

          # Check if service directory exists and has correct permissions
          machine.succeed("test -d /var/lib/meshcore-bot")
          machine.succeed("stat -c '%U:%G' /var/lib/meshcore-bot | grep -q meshcore-bot:meshcore-bot")
          print("✓ Data directory exists with correct ownership")

          # Check if database file exists (non-fatal check)
          # The database is created during MeshCoreBot.__init__ -> DBManager.__init__
          # If bot fails before initialization, DB won't exist - that's OK for this test
          print("Checking for database file...")
          # Use a command that always succeeds to check file existence
          machine.succeed("test -f /var/lib/meshcore-bot/meshcore-bot.db")
          print("✓ Database file created at /var/lib/meshcore-bot/meshcore-bot.db")
          # Verify it's a valid SQLite database
          machine.succeed("sqlite3 /var/lib/meshcore-bot/meshcore-bot.db 'SELECT 1' > /dev/null ")
          print("✓ Database file is valid SQLite database")

          # Check if log file is created in the correct location (non-fatal)
          # Log file is created by the bot's logging setup, may not exist if bot fails early
          print("Checking for log file...")
          machine.succeed("test -f /var/log/meshcore-bot/meshcore-bot.log")
          print("✓ Log file created at /var/log/meshcore-bot/meshcore-bot.log")

          # Check if config was generated
          machine.succeed("test -f /nix/store/*-meshcore-bot-config.ini")
          print("✓ Config file exists in nix store")

          # Verify user and group exist
          print("\n=== User/Group Checks ===")
          machine.succeed("id meshcore-bot")
          print("✓ User meshcore-bot exists")

          # Verify user is in dialout group for serial access
          machine.succeed("groups meshcore-bot | grep -q dialout")
          print("✓ User meshcore-bot is in dialout group")

          # Verify it tried to connect via TCP (if bot got that far)
          machine.succeed("journalctl -u meshcore-bot.service | grep -qi 'tcp\|localhost\|connection'")
          print("✓ Bot attempted TCP connection")

        '';
      };
      nixos-module-webviewer = pkgs.testers.nixosTest {
        name = "meshcore-bot-webviewer";

        nodes.machine = {
          imports = [self.nixosModules.default];

          services.meshcore-bot = {
            enable = true;
            webviewer.enable = true;
            settings = {
              Connection = {
                connection_type = "tcp";
                hostname = "localhost";
                tcp_port = 5000;
                timeout = 30;
              };
              Bot = {
                bot_name = "TestBotWithViewer";
                enabled = true;
              };
              Web_Viewer = {
                debug = false;
              };
              Logging = {
                log_level = "DEBUG";
              };
            };
          };
        };

        testScript = ''
          machine.wait_for_unit("meshcore-bot-viewer.service")

          # Give it time to start and initialize web viewer
          machine.sleep(5)

          # Wait for web viewer to start listening on port 8080
          # This is the NixOS test framework way to check if a port is open
          print("\n=== Web Viewer Port Check ===")
          machine.wait_for_open_port(8080, timeout=30)
          print("✓ Web viewer is listening on port 8080")

          # Verify we can connect to the web viewer
          machine.succeed("curl -f http://127.0.0.1:8080/")
          print("✓ Web viewer responds to HTTP requests")

        '';
      };
    };
  };
}
