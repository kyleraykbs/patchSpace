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
          # swh-plugins provides gate_1410.so (SensitivityGateNode's
          # LADSPA gate); caps provides caps.so (ReverbNode's "Plate").
          # Both are also in pkgs.ladspaPlugins, but listing them
          # explicitly keeps the two plugins the daemon actually needs
          # from silently vanishing if that aggregate's contents change.
          #
          # Each entry is a directory that CONTAINS the shared objects,
          # not the .so files themselves.
          ladspaPluginPackages = [
            pkgs.rnnoise-plugin # librnnoise_ladspa.so (NoiseCancelNode)
            pkgs.swh-plugins # gate_1410.so (SensitivityGateNode)
            pkgs.caps # caps.so (ReverbNode)
            pkgs.ladspaPlugins
          ];
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
            ] ++ ladspaPluginPackages;

            shellHook = ''
              # Prepend the DSP plugin dirs to the daemon's LADSPA search
              # path (preserving whatever the user already had, so other
              # LADSPA hosts in this shell still work).  The patchbay
              # daemon spawns its pw-cli sessions from this environment,
              # and it is THAT process's LADSPA_PATH that the filter-chain
              # module's plugin host reads - see the
              # ladspaPluginPackages comment above.
              export LADSPA_PATH="${pluginPath ladspaDirs}''${LADSPA_PATH:+:$LADSPA_PATH}"
              echo "Python dev shell ready ($(python --version))"
              echo "GTK4, Adwaita, PipeWire, WirePlumber available"
              echo "LADSPA_PATH=$LADSPA_PATH"
            '';
          };
        };
    };
}
