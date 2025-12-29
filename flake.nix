{
  description = "A Python bot that connects to MeshCore mesh networks via serial port, BLE, or TCP/IP";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    treefmt-nix.url = "github:numtide/treefmt-nix";
    meshcore-cli = {
      url = "github:meshcore-dev/meshcore-cli";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = inputs @ {
    flake-parts,
    self,
    ...
  }:
    flake-parts.lib.mkFlake {inherit inputs;} {
      imports = [
        #        inputs.treefmt-nix.flakeModule
        ./nix/packages.nix
        ./nix/shell.nix
        ./nix/nixos-test.nix
        ./nix/nixos-module.nix
      ];
      systems = ["x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin"];
    };
}
