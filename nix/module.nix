# NixOS / home-manager module for the Patch Space daemon + a declarative
# patchspace.
#
# The shape it produces:
#
#   * the daemon runs as a **user** service (it drives the logged-in user's
#     PipeWire session: pw-cli/pw-dump/pw-cat/pw-loopback/wpctl);
#   * the declarative graph is a set of **read-only panel files** generated
#     into the store, handed to the daemon with `--panel-dir ...:ro`, so the
#     daemon can never write them back and `Reset` in the GUI re-applies
#     exactly what Nix said;
#   * the session autosave (the "root panel", where placements and hand-made
#     nodes live) is left where the daemon keeps it, in the user's cache
#     directory - config in the store, state with the daemon;
#   * exported JSON is mixed in per panel (`panels.<name>.imports`) and Nix
#     merges *on top* of it, per node and per edge (see ./lib.nix);
#   * every generated panel is validated at build time with the repo's own
#     `session_repair`, so a bad port, a duplicate edge or an unknown node
#     type fails the build instead of the daemon.
{ self, homeManager ? false }:
{ config, lib, pkgs, osConfig ? null, ... }:

let
  inherit (lib)
    mkEnableOption mkIf mkMerge mkOption types literalExpression optionalString
    concatStringsSep;

  cfg = config.services.patchspace;
  patchspaceLib = import ./lib.nix { inherit lib; };

  system = pkgs.stdenv.hostPlatform.system;
  defaultDaemon = self.packages.${system}.patchspace-daemon;
  defaultClient = self.packages.${system}.patchspace;
  repairTool = self.packages.${system}.patchspace-repair;

  # Node type keys the daemon knows (main.py's NODE_TYPE_REGISTRY).  An enum
  # here turns a typo into an evaluation error; the flake's checks assert
  # this list still matches the packaged daemon, so it can't silently drift.
  nodeTypes = [
    "all_apps" "all_inputs" "all_outputs" "app_input" "app_output"
    "bool_panel_in" "bool_panel_out" "bool_warp_in" "bool_warp_out"
    "boolean_and" "boolean_invert" "boolean_or" "boolean_splitter"
    "boolean_switch" "boolean_xor" "bundle" "bundle_output" "bundle_split"
    "bundle_to_audio" "button" "description_classifier" "description_input"
    "description_output" "device_input" "device_output" "echo_cancel"
    "exclude_filter" "external_only_classifier" "filter" "gate"
    "inverse_switcher" "light_noise_cancel" "media_class_classifier"
    "media_class_input" "media_class_output" "noise_cancel" "normalize"
    "panel_in" "panel_out" "patchspace_device" "patchspace_mic_device"
    "regex_classifier" "regex_input" "regex_output" "reverb"
    "sensitivity_gate" "sound_effect" "splitter" "switcher" "virtual_mic"
    "virtual_speaker" "volume" "warp_in" "warp_out"
  ];

  panelModule = { name, ... }: {
    options = {
      label = mkOption {
        type = types.str;
        default = name;
        description = "Panel title shown above the box.";
      };
      color = mkOption {
        type = types.str;
        default = "#3584e4";
        description = "Panel colour (hex).";
      };
      autoLoad = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Load this panel at start-up.  A panel file that nothing references
          is placed at the root automatically when this is on, so a
          declarative panel comes up on its own - and comes back if the
          placement is deleted in the GUI.
        '';
      };
      placement = {
        # types.number, not float: `w = 520` is what anyone writes, and the
        # daemon reads them as plain JSON numbers.
        x = mkOption { type = types.number; default = 0; };
        y = mkOption { type = types.number; default = 0; };
        w = mkOption { type = types.number; default = 420; };
        h = mkOption { type = types.number; default = 260; };
        anchored = mkOption {
          type = types.bool;
          default = true;
          description = ''
            Pin the panel's *box* so the layout can't shove it around.
            Contents still settle: only nodes the user placed by hand are
            anchored at the node level, so nodes defined here (or imported)
            are laid out by the physics inside the box.
          '';
        };
      };
      imports = mkOption {
        type = types.listOf types.path;
        default = [ ];
        example = literalExpression "[ ./exports/live-session.json ]";
        description = ''
          Exported JSON to merge *under* this panel: anything
          `export_config.py` wrote (or a panel file the GUI exported).  Nix
          wins per node/edge; see the merge rules below.
        '';
      };
      nodes = mkOption {
        type = types.attrsOf (types.submodule {
          options = {
            type = mkOption {
              type = types.nullOr (types.enum nodeTypes);
              default = null;
              description = ''
                Node type.  May be omitted when this entry is *overriding* a
                node an `imports` file already declares - the type then comes
                from there.  Required for a node that exists only here.
              '';
            };
            params = mkOption {
              type = types.attrsOf types.anything;
              default = { };
              description = ''
                The node's serialized parameters, exactly as an export spells
                them (`pattern`, `path`, `label`, `x`, `y`, `anchored`, ...).
                Nodes given no `x`/`y` are placed by the layout.
              '';
            };
          };
        });
        default = { };
        description = "Nodes owned by this panel, keyed by their local id.";
      };
      edges = mkOption {
        type = types.listOf (types.submodule {
          options = {
            from = mkOption { type = types.str; };
            to = mkOption { type = types.str; };
            from_port = mkOption { type = types.str; default = "out"; };
            to_port = mkOption { type = types.str; default = "in"; };
          };
        });
        default = [ ];
      };
      groups = mkOption {
        type = types.listOf types.attrs;
        default = [ ];
        description = "Canvas groups inside this panel (id, label, color, nodes).";
      };
      children = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = ''
          Stems of sub-panels belonging to this panel (each is its own entry
          in `panels`).  A child's placement lives in the child file.
        '';
      };
    };
  };

  # `panels.main` plus the top-level shorthand for the common case
  # (one exported session, a few Nix nodes on top).
  panelDefaults = {
    label = "Main";
    color = "#3584e4";
    autoLoad = true;
    placement = { x = 0; y = 0; w = 420; h = 260; anchored = true; };
    imports = [ ];
    nodes = { };
    edges = [ ];
    groups = [ ];
    children = [ ];
  };
  # ------------------------------------------------------------------
  # Composition across the two scopes
  #
  # The NixOS and home-manager variants expose exactly the same options, and
  # a home-manager configuration **layers on top of** the NixOS-scope one:
  # the user scope wins per node, per edge, per panel and per scalar, so a
  # graph - or a single override - can be written in either place.  The user
  # scope is also where the daemon unit belongs when it is the one enabled:
  # it is a *user* service, and the same unit defined in both scopes would
  # leave systemd picking the user's copy while the system copy's
  # `default.target.wants` link still pointed at the same name.
  #
  # `osConfig` is the NixOS configuration, available when these modules are
  # used together; it is absent for a standalone home-manager install, where
  # this scope is simply the only one.
  # ------------------------------------------------------------------
  base = if homeManager then (osConfig.services.patchspace or null) else null;
  pick = upper: lower: if upper != null then upper else lower;
  baseOf = name: fallback: if base == null then fallback else (base.${name} or fallback);

  # The `main` panel of one scope: the top-level shorthand merged under an
  # explicit `panels.main` (unchanged from the single-scope version, just
  # parameterised so the base scope is laid out the same way).
  mainOf = scope: panelDefaults // (scope.panels.main or { }) // {
    imports = scope.imports ++ ((scope.panels.main or { }).imports or [ ]);
    nodes = scope.nodes // ((scope.panels.main or { }).nodes or { });
    edges = scope.edges ++ ((scope.panels.main or { }).edges or [ ]);
    groups = scope.groups ++ ((scope.panels.main or { }).groups or [ ]);
  };

  # One panel, lower scope under upper scope.  Lists (imports, children)
  # accumulate, nodes/edges/groups merge the same way they do within a scope.
  layerPanel = lower: upper: {
    label = pick upper.label lower.label;
    color = pick upper.color lower.color;
    autoLoad = upper.autoLoad;
    placement = lower.placement // upper.placement;
    children = lower.children ++ upper.children;
    imports = lower.imports ++ upper.imports;
    nodes = lib.recursiveUpdate lower.nodes upper.nodes;
    edges = patchspaceLib.mergeEdges lower.edges upper.edges;
    groups = patchspaceLib.mergeGroups lower.groups upper.groups;
  };

  ownMain = mainOf cfg;
  baseMain = if base == null then null else mainOf base;
  mainPanel = if baseMain == null then ownMain else layerPanel baseMain ownMain;

  ownPanels = removeAttrs cfg.panels [ "main" ];
  basePanels = if base == null then { } else removeAttrs base.panels [ "main" ];
  panels =
    lib.mapAttrs
      (name: upper: if basePanels ? ${name} then layerPanel basePanels.${name} upper else upper)
      ownPanels
    // lib.filterAttrs (name: _: !(ownPanels ? ${name})) basePanels
    // { main = mainPanel; };

  # Scalars: the upper (user) scope wins when it sets one.
  socket = pick cfg.socket (baseOf "socket" null);
  rootPanel = pick cfg.rootPanel (baseOf "rootPanel" null);
  canvasOpacity = pick cfg.canvasOpacity (baseOf "canvasOpacity" null);
  panelDirs = pick cfg.panelDirs (baseOf "panelDirs" null);
  extraArgs = (baseOf "extraArgs" [ ]) ++ cfg.extraArgs;

  # The daemon is handed *some* `--panel-dir` (the generated declarative one
  # is always last), and handing it any at all replaces its built-in default,
  # so the conventional directory - where the GUI keeps the panels you make by
  # hand - has to be named explicitly unless the configuration says otherwise.
  daemonDirs =
    if panelDirs != null then panelDirs
    else [ "%h/.local/share/patchspace/panels:rw" ];

  panelConfig = panel: patchspaceLib.panelConfig panel;

  # Flat config for the validator (what `session_repair` understands).  Its
  # exit status is the build gate: a bad port, an unknown type, a duplicate
  # edge or an endpoint that doesn't exist fails the build here, not at
  # runtime in the daemon.
  panelCheck = name: panel:
    let json = pkgs.writeText "patchspace-${name}.json" (builtins.toJSON (panelConfig panel));
    in pkgs.runCommand "patchspace-check-${name}" {
      # A package-shaped output (dir + bin/), so it is a valid build input
      # rather than a bare file; the build fails here if the config is bad.
      nativeBuildInputs = [ repairTool ];
    } ''
      mkdir -p $out/bin
      # No pipe: the validator's exit status *is* this build's verdict, and a
      # pipe would report the last command's status instead.
      if ! patchspace-repair --check --strict ${json} > $out/report.txt; then
        echo "--- patchspace config for panel '${name}' is invalid ---" >&2
        cat $out/report.txt >&2
        exit 1
      fi
      cat > $out/bin/patchspace-check-${name} <<EOF
      #!${pkgs.runtimeShell}
      exec patchspace-repair --check ${json}
      EOF
      chmod +x $out/bin/patchspace-check-${name}
    '';

  # The panel file the daemon reads.  `mode = read-only` + the `:ro` panel dir
  # is what makes this genuinely declarative: the daemon never writes it back,
  # and `Reset` in the GUI re-applies exactly this content.
  panelFile = name: panel:
    pkgs.writeText "patchspace-panel-${name}.json" (builtins.toJSON {
      type = "panel";
      mode = "read-only";
      label = panel.label;
      color = panel.color;
      auto_load = panel.autoLoad;
      placement = {
        inherit (panel.placement) x y w h anchored;
      };
      config = (panelConfig panel) // { panels = panel.children; };
    });

  panelsDir = pkgs.runCommand "patchspace-panels" {
    # The validated config *is* the config the daemon loads: an invalid panel
    # fails the build here rather than half-loading at runtime.
    buildInputs = lib.optionals cfg.validate (lib.mapAttrsToList panelCheck panels);
  } ''
    mkdir -p $out
    ${concatStringsSep "\n" (lib.mapAttrsToList (name: panel:
      "ln -s ${panelFile name panel} $out/${name}.json") panels)}
  '';

  socketArgs = lib.optionals (socket != null) [ "--socket" (toString socket) ];

  # The daemon *replaces* its default panel directory as soon as one
  # `--panel-dir` is given, so the conventional (imperative) directory has to
  # be passed explicitly here - otherwise enabling this module would hide the
  # panels the GUI makes by hand.  The generated declarative directory goes
  # last: later dirs shadow earlier ones, so a panel defined in Nix wins over
  # a file with the same stem anywhere else.
  panelDirArgs = lib.concatMap (d: [ "--panel-dir" d ]) daemonDirs
    ++ [ "--panel-dir" "${panelsDir}:ro" ];

  rootPanelArgs = lib.optionals (rootPanel != null) [
    "--root-panel" (toString rootPanel)
  ];

  # `type` may be omitted on an override (it comes from the import), so the
  # merged result is what has to have one for every node.  Reported as a
  # build-time assertion naming the offending panel and ids, rather than
  # letting `null` reach the daemon.
  typelessNodes = lib.concatMapStringsSep ", " (name:
    let
      merged = panelConfig panels.${name};
      missing = builtins.filter
        (nid: (merged.nodes.${nid}.type or null) == null)
        (builtins.attrNames merged.nodes);
    in
    lib.optionalString (missing != [ ])
      "${name}: ${concatStringsSep ", " missing}"
  ) (builtins.attrNames panels);

  # A home-manager user that enables patchspace owns the daemon for that user
  # (see the composition note): this scope then defines nothing at all, not
  # even a unit - otherwise systemd would have two definitions of the same
  # user unit and the system one's activation link would point at the other.
  hmOwns = !homeManager && lib.any
    (u: u.services.patchspace.enable or false)
    (lib.attrValues (config.home-manager.users or { }));

  unit = {
    description = "Patch Space daemon (PipeWire patchspace)";
    after = [ "pipewire.service" "wireplumber.service" ];
    wants = [ "pipewire.service" "wireplumber.service" ];
    # Only needed when a root panel is configured somewhere the daemon
    # wouldn't have created for itself (its own default lives under ~/.cache,
    # which already exists).
    execStartPre = lib.optionals (rootPanel != null) [
      "${pkgs.coreutils}/bin/mkdir -p ${builtins.dirOf (toString rootPanel)}"
    ];
    execStart = concatStringsSep " " ([
      "${cfg.package}/bin/patchspace-daemon"
    ] ++ socketArgs ++ panelDirArgs ++ rootPanelArgs ++ cfg.extraArgs);
    restarts = { Restart = "on-failure"; RestartSec = 2; };
    wantedBy = [ "default.target" ];
    # The GUI's canvas opacity travels with the *daemon*: the daemon reads
    # this variable (see main.CANVAS_OPACITY) and reports it with the graph,
    # so a window started from a launcher gets it even when the session
    # environment predates the rebuild - which is the normal case, since a
    # session variable only reaches a session at login.
    environment = mkIf (canvasOpacity != null) [
      "PATCHSPACE_CANVAS_OPACITY=${toString canvasOpacity}"
    ];
  };

in
{
  # The panels are handed to the daemon by closure; the check above rides
  # along with the unit so `nixos-rebuild` builds (and therefore validates)
  # the config it is about to load, and a rebuild lands a *new* store path in
  # ExecStart - which is what restarts the daemon with the new panels.

  options.services.patchspace = {
    enable = mkEnableOption "the Patch Space daemon (and its declarative panels)";

    package = mkOption {
      type = types.package;
      default = defaultDaemon;
      defaultText = literalExpression "patchspace-daemon from this flake";
      description = "The daemon to run (needs pw-cli/pw-cat/wpctl on PATH).";
    };

    installClient = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Install the GUI client (from `clientPackage`) in this scope's profile.

        A patchspace is a daemon plus a set of panel files, and neither of
        those is something you can *open*: the window is a separate app, with
        the `.desktop` entry and icon travelling in its package.  Installing
        it here means `services.patchspace.enable = true` is the whole
        configuration; set this to false if you install the client yourself.
      '';
    };

    clientPackage = mkOption {
      type = types.package;
      default = defaultClient;
      defaultText = literalExpression "the patchspace GUI from this flake";
      description = "The GUI installed when `installClient` is set.";
    };

    socket = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "%t/patchspace.sock";
      description = ''
        Command-API socket.  `null` keeps the daemon's own default
        (`$XDG_RUNTIME_DIR/patchspace.sock`), which is what the GUI and the
        CLI tools connect to out of the box.  Change it and they all need
        `PATCHSPACE_SOCKET` in the session environment - the GUI is a client
        and has to be pointed at the same path.
      '';
    };

    panelDirs = mkOption {
      type = types.nullOr (types.listOf types.str);
      default = null;
      example = literalExpression ''
        [
          "%h/.local/share/patchspace/panels:rw"
          "/etc/patchspace/panels:ro"
        ]
      '';
      description = ''
        Panel directories to load, in the daemon's own `PATH[:rw|:ro]` form:
        searched in order, later dirs shadow earlier ones.  The generated
        declarative directory is always appended **last and read-only**, so a
        panel written in Nix wins over a file with the same stem anywhere else.

        `null` (the default) is the daemon's conventional directory - where
        the GUI keeps the panels you make by hand - because handing the daemon
        any `--panel-dir` at all replaces its built-in default.  Pass `[]` to
        load nothing but the declarative panels.  A home-manager value
        overrides a NixOS-scope one (see the composition note above).
      '';
    };

    rootPanel = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Root panel file: the session autosave (placements, plus any node you
        added by hand instead of declaring it here).  `null` leaves the daemon's
        own default (`~/.cache/patchspace/last_session.json`) alone, so the
        session you have been building in the GUI stays where it is; set it to
        keep that state somewhere else (e.g. `"%S/patchspace/last_session.json"`).
      '';
    };

    canvasOpacity = mkOption {
      type = types.nullOr (types.numbers.between 0.0 1.0);
      default = config.stylix.opacity.applications or null;
      defaultText = literalExpression "config.stylix.opacity.applications";
      example = 0.9;
      description = ''
        Background opacity of the GUI's canvas, 0..1 (1.0 = opaque; lower
        leaves the desktop showing through the grid, with nodes, panels and
        wires still drawn opaque).  Handed to the GUI as
        `PATCHSPACE_CANVAS_OPACITY`, so it applies whether the window is
        started from the launcher or a shell.

        Defaults to stylix's application opacity when stylix is configured
        for this scope, so Patch Space follows the theme without being told
        twice; `null` (no stylix) leaves the GUI's own default of opaque.
      '';
    };

    imports = mkOption {
      type = types.listOf types.path;
      default = [ ];
      example = literalExpression "[ ./exports/live-session.json ]";
      description = "Exported JSON merged under the `main` panel.";
    };

    nodes = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          type = mkOption {
            type = types.nullOr (types.enum nodeTypes);
            default = null;
            description = "Node type (omit when overriding an imported node).";
          };
          params = mkOption { type = types.attrsOf types.anything; default = { }; };
        };
      });
      default = { };
      description = "Nodes of the `main` panel.";
    };

    edges = mkOption {
      type = types.listOf (types.submodule {
        options = {
          from = mkOption { type = types.str; };
          to = mkOption { type = types.str; };
          from_port = mkOption { type = types.str; default = "out"; };
          to_port = mkOption { type = types.str; default = "in"; };
        };
      });
      default = [ ];
      description = "Edges of the `main` panel.";
    };

    groups = mkOption {
      type = types.listOf types.attrs;
      default = [ ];
      description = "Groups of the `main` panel.";
    };

    panels = mkOption {
      type = types.attrsOf (types.submodule panelModule);
      default = { };
      description = ''
        Declarative panels, keyed by stem (the file name the daemon sees).
        `main` is also settable through the top-level `imports`/`nodes`/
        `edges`/`groups` options, which merge under it.
      '';
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Extra arguments for the daemon (see `--help`).";
    };

    validate = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Run every generated panel through the repo's `session_repair` at
        build time.  Catches unknown node types, ports that don't exist,
        kind mismatches (an impulse wire into an audio input), duplicate
        edges and missing endpoints before the daemon ever sees the config.
      '';
    };

    panelsDir = mkOption {
      type = types.path;
      readOnly = true;
      internal = true;
      description = ''
        The generated read-only panel directory handed to the daemon.  Set by
        this module; exposed so a deployment can inspect or `nix build` it
        (that build is what runs the validation).
      '';
    };

    effectiveConfig = mkOption {
      type = types.attrsOf types.anything;
      readOnly = true;
      internal = true;
      description = ''
        The flattened, merged configuration per panel (`{ <stem> = { nodes,
        edges, groups }; }`) that the generated panel files are built from -
        i.e. the NixOS scope with the home-manager scope layered on top.  Set
        by whichever scope owns the daemon; exposed so a deployment (and the
        flake's own checks) can see exactly what the daemon loads.
      '';
    };
  };

  config = mkIf (cfg.enable && !hmOwns) (mkMerge [
    {
      services.patchspace.panelsDir = panelsDir;
      services.patchspace.effectiveConfig =
        lib.mapAttrs (_: patchspaceLib.panelConfig) panels;
    }

    # The GUI is a client of the daemon, so its window defaults come from the
    # session environment rather than the unit - and `canvasOpacity` already
    # follows stylix, so a themed machine needs no extra setting.  Which
    # option holds the session environment depends on the scope this module
    # was loaded in.
    (if homeManager then {
      home.sessionVariables = mkIf (canvasOpacity != null) {
        PATCHSPACE_CANVAS_OPACITY = toString canvasOpacity;
      };
      home.packages = mkIf cfg.installClient [ cfg.clientPackage ];
    } else {
      environment.sessionVariables = mkIf (canvasOpacity != null) {
        PATCHSPACE_CANVAS_OPACITY = toString canvasOpacity;
      };
      environment.systemPackages = mkIf cfg.installClient [ cfg.clientPackage ];
    })

    {
        systemd.user.services.patchspace =
        if homeManager then {
          # home-manager names the INI sections directly.
          Unit = {
            Description = unit.description;
            After = unit.after;
            Wants = unit.wants;
          };
          Service = {
            ExecStartPre = unit.execStartPre;
            ExecStart = unit.execStart;
            inherit (unit.restarts) Restart RestartSec;
            Environment = unit.environment;
          };
          Install.WantedBy = unit.wantedBy;
        } else {
          # NixOS splits them: unitConfig / serviceConfig / install.
          unitConfig = {
            Description = unit.description;
            After = unit.after;
            Wants = unit.wants;
          };
          serviceConfig = {
            ExecStartPre = unit.execStartPre;
            ExecStart = unit.execStart;
            inherit (unit.restarts) Restart RestartSec;
            Environment = unit.environment;
          };
          # NixOS's user units take the new-style `wantedBy`, not
          # `install.WantedBy` (that one is home-manager's spelling).
          wantedBy = unit.wantedBy;
        };

      # (A rebuild rewrites the panel files into a *new* store path, which
      # changes this unit's ExecStart, so the daemon is restarted with the new
      # config - no in-place reload needed.)
      assertions = [
        {
          assertion = typelessNodes == "";
          message = ''
            services.patchspace declares node(s) with no type, and no import
            provides one: ${typelessNodes}
          '';
        }
        {
          assertion = mainPanel.nodes != { } || mainPanel.imports != [ ]
            || mainPanel.edges != [ ] || mainPanel.groups != [ ]
            || mainPanel.children != [ ];
          message = ''
            services.patchspace is enabled but declares nothing: give it
            `panels.<name>` (or the top-level `imports`/`nodes`/`edges`) to
            configure.
          '';
        }
      ];
    }
  ]);
}
