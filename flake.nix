{
  description = "Kyle's Python Template";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      flake-parts,
      ...
    }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      perSystem =
        {
          config,
          self',
          inputs',
          pkgs,
          system,
          ...
        }:
        let
          python = pkgs.python3;

          pythonPackages =
            ps: with ps; [
              # Packages
              numpy
            ];

          pythonDevPackages =
            ps: with ps; [
              pytest
              ipython
              black
            ];

          pythonEnv = python.withPackages (ps: (pythonPackages ps) ++ (pythonDevPackages ps));

        in
        {
          # {{{ Build DevShell
          devShells.default = pkgs.mkShell {
            packages = [
              pythonEnv
            ];

            shellHook = ''
              echo "Python dev shell ready ($(python --version))"
            '';
          };
        };
      # }}}
    };
}
