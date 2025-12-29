{self, ...}: {
  imports = [
  ];
  perSystem = {
    pkgs,
    self',
    ...
  }: {
    # Development shell
    devShells.default = pkgs.mkShell {
      packages = [
        self'.packages.default
      ];
    };
  };
}
