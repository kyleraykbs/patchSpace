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

          # The daemon is pure stdlib Python (it drives pw-cli/pw-dump/pw-link/
          # pw-cat subprocesses).  The GTK client is its own flake.

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
          # Spaces (file-backed node containers) are discovered from:
          #   --space-dir PATH[:rw|:ro]   repeatable; later dirs shadow
          #                               earlier ones.  Defaults to
          #                               $PATCHSPACE_SPACE_DIR or
          #                               ~/.local/share/patchspace/spaces.
          #   --root-space PATH           root space / session autosave;
          #                               defaults to $PATCHSPACE_ROOT_SPACE or
          #                               ~/.cache/patchspace/last_session.json.
          # Both flags can also be supplied through the environment for a
          # packaged service.  The wrapper forwards "$@" so they pass through.
          patchspace-daemon = pkgs.writeShellApplication {
            name = "patchspace-daemon";
            runtimeInputs = [
              python
              pkgs.pipewire
              pkgs.wireplumber
              # Sound nodes: ffprobe reports a file's length, ffmpeg decodes
              # one when only part of it is played (see pwnodes.SoundNode).
              pkgs.ffmpeg-headless
              # Video nodes (VirtualCameraNode): gst-launch runs the
              # camera -> v4l2 pipeline.  pipewiresrc (in the pipewire
              # package) consumes a camera stream; videoconvert and
              # v4l2sink come from base/good.
              pkgs.gst_all_1.gstreamer
              pkgs.gst_all_1.gst-plugins-base
              pkgs.gst_all_1.gst-plugins-good
            ];
            text = ''
              export LADSPA_PATH="${ladspaPath}''${LADSPA_PATH:+:$LADSPA_PATH}"
              export LV2_PATH="${lv2Path}''${LV2_PATH:+:$LV2_PATH}"
              export GST_PLUGIN_SYSTEM_PATH_1_0="${
                pkgs.lib.concatStringsSep ":" [
                  "${pkgs.pipewire}/lib/gstreamer-1.0"
                  "${pkgs.gst_all_1.gst-plugins-base}/lib/gstreamer-1.0"
                  "${pkgs.gst_all_1.gst-plugins-good}/lib/gstreamer-1.0"
                  # The "out" output, explicitly: gstreamer's first
                  # output is "bin", whose lib/gstreamer-1.0 carries no
                  # plugins - core elements (filesink, queue, ...) live
                  # in "out".
                  "${pkgs.gst_all_1.gstreamer.out}/lib/gstreamer-1.0"
                ]
              }''${GST_PLUGIN_SYSTEM_PATH_1_0:+:$GST_PLUGIN_SYSTEM_PATH_1_0}"
              export PYTHONPATH="${source}''${PYTHONPATH:+:$PYTHONPATH}"
              exec ${python}/bin/python3 -m central.main "$@"
            '';
          };

          # Validate/repair a session or space JSON with no daemon and no
          # PipeWire (session_repair is pure stdlib Python - node type/port
          # knowledge comes from gui/node_specs.py, which has no GTK
          # dependency).  What the NixOS/home-manager module runs at build
          # time over every generated space.
          patchspace-repair = pkgs.writeShellApplication {
            name = "patchspace-repair";
            runtimeInputs = [ python ];
            text = ''
              export PYTHONPATH="${source}''${PYTHONPATH:+:$PYTHONPATH}"
              exec ${python}/bin/python3 -m central.session_repair "$@"
            '';
          };

          # The module CLI: which built-in modules are enabled, and turning
          # them on or off live (the daemon owns the config file).
          patchspace-modules = pkgs.writeShellApplication {
            name = "patchspace-modules";
            runtimeInputs = [ python ];
            text = ''
              export PYTHONPATH="${source}''${PYTHONPATH:+:$PYTHONPATH}"
              exec ${python}/bin/python3 -m central.modules_cli "$@"
            '';
          };

        in
        {
          packages = {
            default = patchspace-daemon;
            inherit patchspace-daemon patchspace-repair patchspace-modules;
          };

          apps = {
            default = {
              type = "app";
              program = "${patchspace-daemon}/bin/patchspace-daemon";
            };
            daemon = {
              type = "app";
              program = "${patchspace-daemon}/bin/patchspace-daemon";
            };
          };

          checks =
            let
              patchspaceLibForCheck = import ./nix/lib.nix { inherit (pkgs) lib; };
              # The module's own path: a space's `imports` merged *under* the
              # space's definition, with a flat export, a space-shaped file
              # and Nix on top.  Exercising spaceConfig here (not just
              # mergeConfigs) is the point - the precedence rule that matters
              # is "the space's own Nix wins over everything it imports".
              space = patchspaceLibForCheck.spaceConfig {
                imports = [
                  {
                    nodes.vol = { type = "volume"; params = { initial_volume = 0.5; label = "from json"; }; };
                    nodes.gate = { type = "gate"; params = { enabled = true; }; };
                    edges = [ { from = "vol"; to = "gate"; } ];
                    groups = [ { id = "g"; label = "g"; nodes = [ "vol" ]; } ];
                  }
                  { type = "space"; config = { nodes.only_in_space.type = "button"; edges = [ ]; groups = [ ]; }; }
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
                echo '${builtins.toJSON space}' > merged.json
                # Nix wins for the field it sets ...
                jq -e '.nodes.vol.params.initial_volume == 0.9' merged.json > /dev/null
                # ... and the imported fields it does not mention survive,
                # including the `type` a typeless override leaves alone.
                jq -e '.nodes.vol.type == "volume"' merged.json > /dev/null
                jq -e '.nodes.vol.params.label == "from json"' merged.json > /dev/null
                # Nodes from every layer are present, and the edge declared
                # twice is one edge (same identity).
                jq -e '.nodes.boom.type == "sound_effect"' merged.json > /dev/null
                jq -e '.nodes.only_in_space.type == "button"' merged.json > /dev/null
                jq -e '.edges | length == 2' merged.json > /dev/null
                jq -e '.groups | length == 1' merged.json > /dev/null
                touch $out
              '';

              # Scope composition: the home-manager variant mirrors the NixOS
              # one's options and layers on top of them, and whichever scope is
              # enabled owns the daemon unit (a *user* service) - defined once,
              # not twice.  Asserted purely by evaluation, in the two steps a
              # real host goes through: the NixOS scope first, then the
              # home-manager scope with that as its `osConfig`.
              module-scopes =
                let
                  module = import ./nix/module.nix;
                  patchspace = cfg: cfg.services.patchspace;

                  nixosScope = extra: hmUsers: pkgs.lib.evalModules {
                    specialArgs = { inherit pkgs; lib = pkgs.lib; };
                    modules = [
                      (module { inherit self; homeManager = false; })
                      {
                        options.systemd.user.services = pkgs.lib.mkOption {
                          type = pkgs.lib.types.attrsOf pkgs.lib.types.anything;
                          default = { };
                        };
                        options.assertions = pkgs.lib.mkOption {
                          type = pkgs.lib.types.listOf pkgs.lib.types.anything;
                          default = [ ];
                        };
                        options.environment.sessionVariables = pkgs.lib.mkOption {
                          type = pkgs.lib.types.attrsOf pkgs.lib.types.str;
                          default = { };
                        };
                        options.environment.systemPackages = pkgs.lib.mkOption {
                          type = pkgs.lib.types.listOf pkgs.lib.types.package;
                          default = [ ];
                        };
                        options.home-manager.users = pkgs.lib.mkOption {
                          type = pkgs.lib.types.attrsOf (pkgs.lib.types.submodule {
                            options.services.patchspace.enable = pkgs.lib.mkOption {
                              type = pkgs.lib.types.bool;
                              default = false;
                            };
                          });
                          default = { };
                        };
                        config.services.patchspace = {
                          enable = true;
                          nodes.from_nixos = {
                            type = "gate";
                            params = { label = "nixos"; enabled = true; };
                          };
                        };
                        config.home-manager.users = hmUsers;
                      }
                    ] ++ extra;
                  };

                  # The NixOS scope alone: it owns the unit.
                  alone = nixosScope [ ] { };

                  # The same scope with a client package named: the "the client
                  # is its own flake" assertion must then be satisfied.
                  withClient = nixosScope [
                    { config.services.patchspace.clientPackage = pkgs.hello; }
                  ] { };

                  # Assertions only fire when something forces them; this is
                  # the forcing (a real host reads config.assertions).
                  clientErrors = cfg: pkgs.lib.filter (a: !a.assertion) cfg.config.assertions;

                  # A home-manager user that enables patchspace: the user scope
                  # now owns it, and the NixOS scope must define no unit at all.
                  claimed = nixosScope [ ] {
                    kyle.services.patchspace.enable = true;
                  };

                  # The home-manager scope, layering onto that NixOS scope.
                  homeScope = pkgs.lib.evalModules {
                    # What home-manager passes when it runs under NixOS: the
                    # system configuration, which this scope layers onto.
                    specialArgs = { inherit pkgs; lib = pkgs.lib; osConfig = claimed.config; };
                    modules = [
                      (module { inherit self; homeManager = true; })
                      {
                        options.home.packages = pkgs.lib.mkOption {
                          type = pkgs.lib.types.listOf pkgs.lib.types.package;
                          default = [ ];
                        };
                        options.home.sessionVariables = pkgs.lib.mkOption {
                          type = pkgs.lib.types.attrsOf pkgs.lib.types.str;
                          default = { };
                        };
                        options.systemd.user.services = pkgs.lib.mkOption {
                          type = pkgs.lib.types.attrsOf pkgs.lib.types.anything;
                          default = { };
                        };
                        options.assertions = pkgs.lib.mkOption {
                          type = pkgs.lib.types.listOf pkgs.lib.types.anything;
                          default = [ ];
                        };
                        config.services.patchspace = {
                          enable = true;
                          nodes.from_nixos.params = { label = "from-hm"; };
                          nodes.from_hm = { type = "splitter"; params = { }; };
                          edges = [ { from = "from_nixos"; to = "from_hm"; } ];
                        };
                      }
                    ];
                  };

                  merged = (patchspace homeScope.config).effectiveConfig.main;
                  solo = (patchspace alone.config).effectiveConfig.main;
                  userUnit = homeScope.config.systemd.user.services.patchspace or { };
                  systemUnit = claimed.config.systemd.user.services.patchspace or { };
                  soloUnit = alone.config.systemd.user.services.patchspace or { };

                  problems = pkgs.lib.filter (p: p != null) [
                    (if merged.nodes.from_nixos.params.label == "from-hm" then null
                     else "the home-manager scope did not win the field it set")
                    (if merged.nodes.from_nixos.params.enabled then null
                     else "the NixOS-scope field did not survive the merge")
                    (if merged.nodes.from_hm.type == "splitter" then null
                     else "a home-manager-only node is missing")
                    (if pkgs.lib.length merged.edges == 1 then null
                     else "the edges did not merge")
                    (if userUnit.Service.ExecStart or null != null then null
                     else "the home-manager scope did not take over the daemon")
                    (if systemUnit == { } then null
                     else "the NixOS scope defined a unit while home-manager owns it")
                    (if soloUnit.serviceConfig.ExecStart or null != null then null
                     else "a NixOS-only configuration did not define the daemon")
                    (if solo.nodes.from_nixos.type == "gate" then null
                     else "a NixOS-only configuration did not generate its space")
                    # The client is its own flake: installing it without saying
                    # where it comes from is an evaluation error, and naming it
                    # satisfies that.
                    (if pkgs.lib.length (clientErrors alone) == 1 then null
                     else "installClient with no clientPackage should be an evaluation error")
                    (if pkgs.lib.length (clientErrors withClient) == 0 then null
                     else "naming the client package should satisfy the assertion")
                  ];
                in
                if problems == [ ]
                then pkgs.runCommand "check-module-scopes" { } "touch $out"
                else throw "module scope composition: ${pkgs.lib.concatStringsSep "; " problems}";
            };

          devShells.default = pkgs.mkShell {
            packages = [
              (python.withPackages (
                ps: with ps; [
                  numpy
                  pytest
                  ipython
                  black
                ]
              ))
              pkgs.pipewire
              pkgs.wireplumber
            ] ++ ladspaPluginPackages ++ lv2PluginPackages;

            shellHook = ''
              export LADSPA_PATH="${ladspaPath}''${LADSPA_PATH:+:$LADSPA_PATH}"
              export LV2_PATH="${lv2Path}''${LV2_PATH:+:$LV2_PATH}"
              echo "Python dev shell ready ($(python --version))"
              echo "PipeWire, WirePlumber and the DSP plugins available"
              echo "the GTK client is its own repo (patchSpaceGui)"
              echo "LADSPA_PATH=$LADSPA_PATH"
              echo "LV2_PATH=$LV2_PATH"
            '';
          };
        };
    };
}
