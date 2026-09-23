{
  description = "Patch Space - a PipeWire patchspace daemon and GTK4 client";

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

      # The declarative-configuration modules: one body, used from both a
      # NixOS system and a home-manager setup (the daemon is a *user* service
      # either way - it drives the logged-in user's PipeWire session).
      # `flake.lib.patchspace` exposes the JSON-merge helpers on their own, for
      # anyone composing configs outside the module.
      flake = {
        modules.nixos.patchspace = import ./nix/module.nix { inherit self; };
        modules.homeManager.patchspace = import ./nix/module.nix { inherit self; };
        nixosModules.patchspace = import ./nix/module.nix { inherit self; };
        homeModules.patchspace = import ./nix/module.nix { inherit self; };
        lib.patchspace = import ./nix/lib.nix { inherit (inputs.nixpkgs) lib; };
      };

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
          #                               $PATCHSPACE_PANEL_DIR or
          #                               ~/.local/share/patchspace/panels.
          #   --root-panel PATH           root panel / session autosave;
          #                               defaults to $PATCHSPACE_ROOT_PANEL or
          #                               ~/.cache/patchspace/last_session.json.
          # Both flags can also be supplied through the environment for a
          # packaged service.  The wrapper forwards "$@" so they pass through.
          patchspace-daemon = pkgs.writeShellApplication {
            name = "patchspace-daemon";
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

          # Validate/repair a session or panel JSON with no daemon and no
          # PipeWire (session_repair is pure stdlib Python - node type/port
          # knowledge comes from gui/node_specs.py, which has no GTK
          # dependency).  What the NixOS/home-manager module runs at build
          # time over every generated panel.
          patchspace-repair = pkgs.writeShellApplication {
            name = "patchspace-repair";
            runtimeInputs = [ python ];
            text = ''
              exec ${python}/bin/python3 ${source}/session_repair.py "$@"
            '';
          };

          # The GTK4 client.  wrapGAppsHook propagates the GTK/Adwaita
          # typelib + GSettings-schema + XDG data dirs the build inputs'
          # setup hooks collect, so the packaged GUI finds Gtk/Adw without
          # any manual GI_TYPELIB_PATH fiddling.
          patchspace = pkgs.stdenvNoCC.mkDerivation {
            pname = "patchspace";
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
              mkdir -p $out/bin $out/share/applications \
                $out/share/icons/hicolor/scalable/apps
              # PATCHSPACE_DAEMON tells the GUI exactly which daemon binary
              # to spawn for its "start a background daemon" behaviour.
              makeWrapper ${guiPythonEnv}/bin/python $out/bin/patchspace \
                --add-flags "${source}/gui/patchspace_gui.py" \
                --set PATCHSPACE_DAEMON "${patchspace-daemon}/bin/patchspace-daemon" \
                --prefix LADSPA_PATH : "${ladspaPath}" \
                --prefix LV2_PATH : "${lv2Path}"
              # Desktop entry + its icon travel with the package, so a
              # launcher (or `home.packages`) picks the app up without the
              # consumer repeating `xdg.desktopEntries`/`xdg.dataFile`.
              # Copied under the plain names: a store path's basename carries
              # a hash prefix, and a desktop file is found by its name.
              cp ${./packaging/org.patchspace.desktop} \
                $out/share/applications/org.patchspace.desktop
              cp ${./packaging/org.patchspace.svg} \
                $out/share/icons/hicolor/scalable/apps/org.patchspace.svg
              runHook postInstall
            '';
            meta.mainProgram = "patchspace";
          };

        in
        {
          packages = {
            default = patchspace;
            inherit patchspace patchspace-daemon patchspace-repair;
          };

          apps = {
            default = {
              type = "app";
              program = "${patchspace}/bin/patchspace";
            };
            patchspace = {
              type = "app";
              program = "${patchspace}/bin/patchspace";
            };
            daemon = {
              type = "app";
              program = "${patchspace-daemon}/bin/patchspace-daemon";
            };
          };

          checks =
            let
              patchspaceLibForCheck = import ./nix/lib.nix { inherit (pkgs) lib; };
              # The module's own path: a panel's `imports` merged *under* the
              # panel's definition, with a flat export, a panel-shaped file
              # and Nix on top.  Exercising panelConfig here (not just
              # mergeConfigs) is the point - the precedence rule that matters
              # is "the panel's own Nix wins over everything it imports".
              panel = patchspaceLibForCheck.panelConfig {
                imports = [
                  {
                    nodes.vol = { type = "volume"; params = { initial_volume = 0.5; label = "from json"; }; };
                    nodes.gate = { type = "gate"; params = { enabled = true; }; };
                    edges = [ { from = "vol"; to = "gate"; } ];
                    groups = [ { id = "g"; label = "g"; nodes = [ "vol" ]; } ];
                  }
                  { type = "panel"; config = { nodes.only_in_panel.type = "button"; edges = [ ]; groups = [ ]; }; }
                ];
                nodes = {
                  vol.params.initial_volume = 0.9;   # override, `type` not restated
                  boom = { type = "sound_effect"; params = { path = "~/x.wav"; }; };
                };
                edges = [ { from = "vol"; to = "gate"; } { from = "vol"; to = "boom"; } ];
                groups = [ ];
              };
            in
            {
              module-merge = pkgs.runCommand "check-module-merge" { nativeBuildInputs = [ pkgs.jq ]; } ''
                echo '${builtins.toJSON panel}' > merged.json
                # Nix wins for the field it sets ...
                jq -e '.nodes.vol.params.initial_volume == 0.9' merged.json > /dev/null
                # ... and the imported fields it does not mention survive,
                # including the `type` a typeless override leaves alone.
                jq -e '.nodes.vol.type == "volume"' merged.json > /dev/null
                jq -e '.nodes.vol.params.label == "from json"' merged.json > /dev/null
                # Nodes from every layer are present, and the edge declared
                # twice is one edge (same identity).
                jq -e '.nodes.boom.type == "sound_effect"' merged.json > /dev/null
                jq -e '.nodes.only_in_panel.type == "button"' merged.json > /dev/null
                jq -e '.edges | length == 2' merged.json > /dev/null
                jq -e '.groups | length == 1' merged.json > /dev/null
                touch $out
              '';
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
