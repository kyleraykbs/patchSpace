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

          # DSP plugin packages the daemon's pw-cli sessions load. A
          # filter-chain/echo-cancel module is instantiated INSIDE the
          # pw-cli process the daemon spawns (verified empirically - the
          # module's plugin host honors the pw-cli client's inherited
          # environment, not the PipeWire server's), so an effect's
          # plugin must be discoverable from the daemon's own
          # environment:
          #
          #   * LV2 effects (SensitivityGateNode's Calf Gate) resolve by
          #     URI through LV2_PATH - the packages below only get added
          #     to PATH by mkShell, and NixOS never sets LV2_PATH, so
          #     without the shellHook export below the Gate's
          #     "http://calf.sourceforge.net/plugins/Gate" URI can't be
          #     found and the module load fails every retry.
          #   * LADSPA effects (NoiseCancelNode's RNNoise) are loaded by
          #     absolute .so path from the daemon-side file probes in
          #     patchSpace.py, so they work regardless; LADSPA_PATH is
          #     still exported for any LADSPA plugin that relies on it.
          #
          # Each entry is a directory that CONTAINS the bundles (LV2) or
          # the shared objects (LADSPA), not the bundles/.so files
          # themselves.
          lv2PluginPackages = [
            pkgs.calf # Calf Gate etc. (SensitivityGateNode)
            pkgs.noise-repellent
          ];
          ladspaPluginPackages = [
            pkgs.rnnoise-plugin # librnnoise_ladspa.so (NoiseCancelNode)
            pkgs.ladspaPlugins
          ];
          lv2Dirs = map (p: "${p}/lib/lv2") lv2PluginPackages;
          ladspaDirs = map (p: "${p}/lib/ladspa") ladspaPluginPackages;
          pluginPath = dirs: pkgs.lib.concatStringsSep ":" dirs;

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
            ] ++ lv2PluginPackages ++ ladspaPluginPackages;

            shellHook = ''
              # Prepend the DSP plugin dirs to the daemon's LV2/LADSPA
              # search paths (preserving whatever the user already had,
              # so other LV2/LADSPA hosts in this shell still work).
              # The patchbay daemon spawns its pw-cli sessions from this
              # environment, and it is THAT process's LV2_PATH/LADSPA_PATH
              # that the filter-chain module's plugin host reads - see
              # the lv2PluginPackages/ladspaPluginPackages comment above.
              export LV2_PATH="${pluginPath lv2Dirs}''${LV2_PATH:+:$LV2_PATH}"
              export LADSPA_PATH="${pluginPath ladspaDirs}''${LADSPA_PATH:+:$LADSPA_PATH}"
              echo "Python dev shell ready ($(python --version))"
              echo "GTK4, Adwaita, PipeWire, WirePlumber available"
              echo "LV2_PATH=$LV2_PATH"
              echo "LADSPA_PATH=$LADSPA_PATH"
            '';
          };
        };
    };
}
