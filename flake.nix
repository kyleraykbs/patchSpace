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
              numpy
              pygobject3 # GTK bindings
              pycairo # Cairo drawing
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
          devShells.default = pkgs.mkShell {
            packages = [
              pythonEnv
              pkgs.gobject-introspection
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.cairo # provides Cairo-1.0.typelib
              pkgs.pipewire
              pkgs.wireplumber
              # Noise Cancel node's DSP backends (see
              # patchSpace.NoiseCancelNode.METHODS):
              # RNNoise LADSPA ("rnnoise" method)
              pkgs.rnnoise-plugin
              # Noise Repellent spectral-subtraction LV2 ("noise_repellent")
              pkgs.noise-repellent
              # Steve Harris LADSPA suite, provides gate_1921 ("gate")
              pkgs.ladspaPlugins
            ];

            shellHook = ''
              echo "Python dev shell ready ($(python --version))"
              echo "GTK4, Adwaita, PipeWire, WirePlumber available"
              echo "Noise plugins: RNNoise / Noise Repellent / SWH gate"
            '';
          };
        };
    };
}
