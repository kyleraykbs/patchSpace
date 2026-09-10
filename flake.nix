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
          #   * LADSPA effects (NoiseCancelNode's RNNoise) are loaded by
          #     absolute .so path from the daemon-side file probes in
          #     pwnodes.py, so they work regardless; LADSPA_PATH is
          #     still exported for any LADSPA plugin that relies on it.
          #
          # pkgs.ladspaPlugins is the swh-plugins set: it provides
          # gate_1410.so (SensitivityGateNode), sc4_1882.so /
          # fast_lookahead_limiter_1913.so (NormalizeNode) and
          # gverb_1216.so.  caps provides caps.so, kept for the reverb's
          # LADSPA fallback/experiments.
          #
          # ReverbNode itself uses Calf Reverb, an LV2 plugin: LV2 is
          # found by URI via LV2_PATH (there is no absolute path to
          # probe), so calf must be on the path too - see lv2Dirs below.
          #
          # Each entry is a directory that CONTAINS the shared objects,
          # not the .so files themselves.
          ladspaPluginPackages = [
            pkgs.rnnoise-plugin # librnnoise_ladspa.so (NoiseCancelNode)
            pkgs.ladspaPlugins # swh: gate_1410, sc4, fastLookaheadLimiter
            pkgs.caps # caps.so (LADSPA fallback plugin set)
          ];
          ladspaDirs = map (p: "${p}/lib/ladspa") ladspaPluginPackages;
          lv2PluginPackages = [
            pkgs.calf # Calf Reverb (ReverbNode)
          ];
          lv2Dirs = map (p: "${p}/lib/lv2") lv2PluginPackages;
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
            ] ++ ladspaPluginPackages ++ lv2PluginPackages;

            shellHook = ''
              # Prepend the DSP plugin dirs to the daemon's search paths
              # (preserving whatever the user already had, so other hosts
              # in this shell still work).  The patchbay daemon spawns its
              # pw-cli sessions from this environment, and it is THAT
              # process's LADSPA_PATH / LV2_PATH that the filter-chain
              # module's plugin host reads - see the comments above.
              export LADSPA_PATH="${pluginPath ladspaDirs}''${LADSPA_PATH:+:$LADSPA_PATH}"
              export LV2_PATH="${pluginPath lv2Dirs}''${LV2_PATH:+:$LV2_PATH}"
              echo "Python dev shell ready ($(python --version))"
              echo "GTK4, Adwaita, PipeWire, WirePlumber available"
              echo "LADSPA_PATH=$LADSPA_PATH"
              echo "LV2_PATH=$LV2_PATH"
            '';
          };
        };
    };
}
