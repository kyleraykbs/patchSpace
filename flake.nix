{
  description = "Patch Space - a PipeWire patchbay daemon and GTK4 client";

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
          source = ./src;

          # The GUI needs PyGObject + PyCairo; the daemon is pure stdlib
          # Python (it drives pw-cli/pw-dump/pw-link/pw-cat subprocesses).
          guiPythonEnv = python.withPackages (
            ps: with ps; [
              pygobject3
              pycairo
            ]
          );

          # DSP plugins the daemon's pw-cli sessions load.  A filter-chain /
          # echo-cancel module is instantiated inside the pw-cli process the
          # daemon spawns, and that process's plugin host honours the
          # pw-cli client's *inherited environment* (not the PipeWire
          # server's), so the daemon wrapper must export LADSPA_PATH /
          # LV2_PATH (plus LADSPA effects are probed by absolute .so path
          # in pwnodes.py, but exporting is belt-and-suspenders).
          ladspaPluginPackages = [
            pkgs.rnnoise-plugin # librnnoise_ladspa.so (NoiseCancelNode)
            pkgs.ladspaPlugins # swh: gate_1410, sc4, fastLookaheadLimiter
            pkgs.caps # caps.so (LADSPA fallback set)
          ];
          lv2PluginPackages = [
            pkgs.calf # Calf Reverb (ReverbNode)
          ];
          ladspaPath = pkgs.lib.concatStringsSep ":" (
            map (p: "${p}/lib/ladspa") ladspaPluginPackages
          );
          lv2Path = pkgs.lib.concatStringsSep ":" (
            map (p: "${p}/lib/lv2") lv2PluginPackages
          );

          # The headless daemon: socket server + PipeWire graph supervisor.
          #
          # Panels (file-backed node containers) are discovered from:
          #   --panel-dir PATH[:rw|:ro]   repeatable; later dirs shadow
          #                               earlier ones.  Defaults to
          #                               $PATCHBAY_PANEL_DIR or
          #                               ~/.local/share/patchbay/panels.
          #   --root-panel PATH           root panel / session autosave;
          #                               defaults to $PATCHBAY_ROOT_PANEL or
          #                               ~/.cache/patchbay/last_session.json.
          # Both flags can also be supplied through the environment for a
          # packaged service.  The wrapper forwards "$@" so they pass through.
          patchbay-daemon = pkgs.writeShellApplication {
            name = "patchbay-daemon";
            runtimeInputs = [
              python
              pkgs.pipewire
              pkgs.wireplumber
            ];
            text = ''
              export LADSPA_PATH="${ladspaPath}''${LADSPA_PATH:+:$LADSPA_PATH}"
              export LV2_PATH="${lv2Path}''${LV2_PATH:+:$LV2_PATH}"
              exec ${python}/bin/python3 ${source}/main.py "$@"
            '';
          };

          # The GTK4 client.  wrapGAppsHook propagates the GTK/Adwaita
          # typelib + GSettings-schema + XDG data dirs the build inputs'
          # setup hooks collect, so the packaged GUI finds Gtk/Adw without
          # any manual GI_TYPELIB_PATH fiddling.
          patchbay = pkgs.stdenvNoCC.mkDerivation {
            pname = "patchbay";
            version = "0.1.0";
            src = source;
            dontUnpack = true;
            dontBuild = true;
            dontConfigure = true;
            nativeBuildInputs = [
              pkgs.makeWrapper
              pkgs.wrapGAppsHook4
            ];
            buildInputs = [
              guiPythonEnv
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.gobject-introspection
              pkgs.cairo
              pkgs.pipewire
              pkgs.wireplumber
            ];
            installPhase = ''
              runHook preInstall
              mkdir -p $out/bin
              # PATCHBAY_DAEMON tells the GUI exactly which daemon binary
              # to spawn for its "start a background daemon" behaviour.
              makeWrapper ${guiPythonEnv}/bin/python $out/bin/patchbay \
                --add-flags "${source}/gui/patchbay_gui.py" \
                --set PATCHBAY_DAEMON "${patchbay-daemon}/bin/patchbay-daemon" \
                --prefix LADSPA_PATH : "${ladspaPath}" \
                --prefix LV2_PATH : "${lv2Path}"
              runHook postInstall
            '';
            meta.mainProgram = "patchbay";
          };

        in
        {
          packages = {
            default = patchbay;
            inherit patchbay patchbay-daemon;
          };

          apps = {
            default = {
              type = "app";
              program = "${patchbay}/bin/patchbay";
            };
            patchbay = {
              type = "app";
              program = "${patchbay}/bin/patchbay";
            };
            daemon = {
              type = "app";
              program = "${patchbay-daemon}/bin/patchbay-daemon";
            };
          };

          devShells.default = pkgs.mkShell {
            packages = [
              (python.withPackages (
                ps: with ps; [
                  numpy
                  pygobject3
                  pycairo
                  pytest
                  ipython
                  black
                ]
              ))
              pkgs.gobject-introspection
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.cairo
              pkgs.pipewire
              pkgs.wireplumber
            ] ++ ladspaPluginPackages ++ lv2PluginPackages;

            shellHook = ''
              export LADSPA_PATH="${ladspaPath}''${LADSPA_PATH:+:$LADSPA_PATH}"
              export LV2_PATH="${lv2Path}''${LV2_PATH:+:$LV2_PATH}"
              echo "Python dev shell ready ($(python --version))"
              echo "GTK4, Adwaita, PipeWire, WirePlumber available"
              echo "LADSPA_PATH=$LADSPA_PATH"
              echo "LV2_PATH=$LV2_PATH"
            '';
          };
        };
    };
}
