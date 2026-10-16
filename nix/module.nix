# NixOS / home-manager module for the Patch Space daemon + a declarative
# patchspace.
#
# The shape it produces:
#
#   * the daemon runs as a **user** service (it drives the logged-in user's
#     PipeWire session: pw-cli/pw-dump/pw-cat/pw-loopback/wpctl);
#   * the declarative graph is a set of **read-only space files** generated
#     into the store, handed to the daemon with `--space-dir ...:ro`, so the
#     daemon can never write them back and `Reset` in the GUI re-applies
#     exactly what Nix said;
#   * the session autosave (the "root space", where placements and hand-made
#     nodes live) is left where the daemon keeps it, in the user's cache
#     directory - config in the store, state with the daemon;
#   * exported JSON is mixed in per space (`spaces.<name>.imports`) and Nix
#     merges *on top* of it, per node and per edge (see ./lib.nix);
#   * every generated space is validated at build time with the repo's own
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
  # The client is its own repo/flake (see agents/PLAN.md phase 7), so this
  # module does not supply one: a configuration that installs it says where it
  # comes from (`clientPackage`).  Until that split lands, this flake's own GUI
  # package is the obvious value to pass.
  defaultClient = null;
  repairTool = self.packages.${system}.patchspace-repair;

  # Node type keys the daemon knows (central/main.py's NODE_TYPE_REGISTRY).  An enum
  # here turns a typo into an evaluation error; the flake's checks assert
  # this list still matches the packaged daemon, so it can't silently drift.
  #
  # The four `panel_*`/`bool_panel_*` ids are the pre-rename spelling of the
  # space I/O ports (S0: panel -> space).  Deliberate legacy entries: the
  # daemon still resolves them (its registry aliases them and the module
  # announces them as `legacy_aliases`), so a graph written before the
  # rename keeps evaluating and loading.  They go away, with the other
  # aliases, in S1.
  nodeTypes = [
    "all_apps" "all_inputs" "all_outputs" "app_classifier" "app_input"
    "app_name_classifier" "app_output"
    "bool_space_in" "bool_space_out" "bool_warp_in" "bool_warp_out"
    "boolean_and" "boolean_invert" "boolean_or" "boolean_splitter"
    "boolean_switch" "boolean_xor" "bundle" "bundle_output" "bundle_split"
    "bundle_to_audio" "button" "clip" "description_classifier" "description_input"
    "description_output" "device_input" "device_output" "echo_cancel"
    "exclude_filter" "external_only_classifier" "filter" "gate"
    "inverse_switcher" "light_noise_cancel" "media_class_classifier"
    "media_class_input" "media_class_output" "noise_cancel" "normalize"
    "space_in" "space_out" "patchspace_device" "patchspace_mic_device"
    "regex_classifier" "regex_input" "regex_output" "reverb"
    "recorder" "sensitivity_gate" "sound" "sound_player" "splitter"
    "switcher"
    "title_classifier" "virtual_mic"
    "virtual_speaker" "volume" "warp_in" "warp_out"
    # legacy (S0)
    "bool_panel_in" "bool_panel_out" "panel_in" "panel_out"
  ];

  spaceModule = { name, ... }: {
    options = {
      label = mkOption {
        type = types.str;
        default = name;
        description = "Space title shown above the box.";
      };
      color = mkOption {
        type = types.str;
        default = "@blue";
        description = ''
          Space color: a hex string, or one of the theme's slots as
          `@blue`/`@green`/`@yellow`/`@red`/`@purple`/`@teal`.  A slot is
          resolved against the *current* theme every time it is drawn, so a
          space colored `@blue` follows the desktop (stylix recolors those
          slots) instead of freezing one hex value; the default is `@blue`.
        '';
      };
      autoLoad = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Load this space at start-up.  A space file that nothing references
          is placed at the root automatically when this is on, so a
          declarative space comes up on its own - and comes back if the
          placement is deleted in the GUI.
        '';
      };
      placement = {
        # types.number, not float: `w = 520` is what anyone writes, and the
        # daemon reads them as plain JSON numbers.
        x = mkOption {
          type = types.nullOr types.number;
          default = null;
          description = ''
            Left/top of the space on the canvas.  Left **unset** the space has
            no position of its own: the daemon places it beside the spaces
            that do have one (to their right, top-aligned) instead of dropping
            it at the origin, and remembers that in the root space - so a
            declarative space shows up next to the graph you have arranged.
            Set it to be explicit about where it goes.
          '';
        };
        y = mkOption {
          type = types.nullOr types.number;
          default = null;
          description = "Top of the space on the canvas - see `x`.";
        };
        w = mkOption { type = types.number; default = 420; };
        h = mkOption { type = types.number; default = 260; };
        anchored = mkOption {
          type = types.bool;
          default = true;
          description = ''
            Pin the space's *box* so the layout can't shove it around.
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
          Exported JSON to merge *under* this space: anything
          `export_config.py` wrote (or a space file the GUI exported).  Nix
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
        description = "Nodes owned by this space, keyed by their local id.";
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
        description = "Canvas groups inside this space (id, label, color, nodes).";
      };
      children = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = ''
          Stems of sub-spaces belonging to this space (each is its own entry
          in `spaces`).  A child's placement lives in the child file.
        '';
      };
    };
  };

  # `spaces.main` plus the top-level shorthand for the common case
  # (one exported session, a few Nix nodes on top).
  spaceDefaults = {
    label = "Main";
    color = "#3584e4";
    autoLoad = true;
    # `x`/`y` stay null here too: the space's placement defaults carry the
    # "no coordinates set" state, not 0/0 (see the generated space file).
    placement = { x = null; y = null; w = 420; h = 260; anchored = true; };
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
  # the user scope wins per node, per edge, per space and per scalar, so a
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

  # The `main` space of one scope: the top-level shorthand merged under an
  # explicit `spaces.main` (unchanged from the single-scope version, just
  # parameterised so the base scope is laid out the same way).
  mainOf = scope: spaceDefaults // (scope.spaces.main or { }) // {
    imports = scope.imports ++ ((scope.spaces.main or { }).imports or [ ]);
    nodes = scope.nodes // ((scope.spaces.main or { }).nodes or { });
    edges = scope.edges ++ ((scope.spaces.main or { }).edges or [ ]);
    groups = scope.groups ++ ((scope.spaces.main or { }).groups or [ ]);
  };

  # One space, lower scope under upper scope.  Lists (imports, children)
  # accumulate, nodes/edges/groups merge the same way they do within a scope.
  layerSpace = lower: upper: {
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
  mainSpace = if baseMain == null then ownMain else layerSpace baseMain ownMain;

  ownSpaces = removeAttrs cfg.spaces [ "main" ];
  baseSpaces = if base == null then { } else removeAttrs base.spaces [ "main" ];
  declaredSpaces =
    lib.mapAttrs
      (name: upper: if baseSpaces ? ${name} then layerSpace baseSpaces.${name} upper else upper)
      ownSpaces
    // lib.filterAttrs (name: _: !(ownSpaces ? ${name})) baseSpaces;
  # Is there anything declarative at all?  Enabling the service without
  # declaring a graph is a complete configuration - the spaces you make in the
  # GUI (the daemon's own, writable space directory) are then the graph - and
  # generating an *empty* `main` space file for it would be worse than
  # useless: it opts in with `auto_load`, so the daemon adopts it and an empty
  # "Main" space appears on the canvas.
  hasDeclarations =
    mainSpace.nodes != { } || mainSpace.imports != [ ]
    || mainSpace.edges != [ ] || mainSpace.groups != [ ]
    || mainSpace.children != [ ] || declaredSpaces != { };
  spaces =
    if hasDeclarations then declaredSpaces // { main = mainSpace; }
    else declaredSpaces;

  # Scalars: the upper (user) scope wins when it sets one.
  socket = pick cfg.socket (baseOf "socket" null);
  rootSpace = pick cfg.rootSpace (baseOf "rootSpace" null);
  canvasOpacity = pick cfg.canvasOpacity (baseOf "canvasOpacity" null);
  spaceDirs = pick cfg.spaceDirs (baseOf "spaceDirs" null);
  extraArgs = (baseOf "extraArgs" [ ]) ++ cfg.extraArgs;

  # The daemon is handed *some* `--space-dir` (the generated declarative one
  # is always last), and handing it any at all replaces its built-in default,
  # so the conventional directory - where the GUI keeps the spaces you make by
  # hand - has to be named explicitly unless the configuration says otherwise.
  daemonDirs =
    if spaceDirs != null then spaceDirs
    else [ "%h/.local/share/patchspace/spaces:rw" ];

  spaceConfig = space: patchspaceLib.spaceConfig space;

  # Flat config for the validator (what `session_repair` understands).  Its
  # exit status is the build gate: a bad port, an unknown type, a duplicate
  # edge or an endpoint that doesn't exist fails the build here, not at
  # runtime in the daemon.
  spaceCheck = name: space:
    let json = pkgs.writeText "patchspace-${name}.json" (builtins.toJSON (spaceConfig space));
    in pkgs.runCommand "patchspace-check-${name}" {
      # A package-shaped output (dir + bin/), so it is a valid build input
      # rather than a bare file; the build fails here if the config is bad.
      nativeBuildInputs = [ repairTool ];
    } ''
      mkdir -p $out/bin
      # No pipe: the validator's exit status *is* this build's verdict, and a
      # pipe would report the last command's status instead.
      if ! patchspace-repair --check --strict ${json} > $out/report.txt; then
        echo "--- patchspace config for space '${name}' is invalid ---" >&2
        cat $out/report.txt >&2
        exit 1
      fi
      cat > $out/bin/patchspace-check-${name} <<EOF
      #!${pkgs.runtimeShell}
      exec patchspace-repair --check ${json}
      EOF
      chmod +x $out/bin/patchspace-check-${name}
    '';

  # The space file the daemon reads.  `mode = read-only` + the `:ro` space dir
  # is what makes this genuinely declarative: the daemon never writes it back,
  # and `Reset` in the GUI re-applies exactly this content.
  spaceFile = name: space:
    pkgs.writeText "patchspace-space-${name}.json" (builtins.toJSON {
      # The space's kind (S1).  A generated space is live: `space`/`panel`
      # were the pre-S1 spellings of exactly that, and the daemon still reads
      # them, but new files say what they are.
      type = "live";
      mode = "read-only";
      label = space.label;
      color = space.color;
      auto_load = space.autoLoad;
      placement =
        # Only the coordinates someone actually set: an absent one is how the
        # daemon knows the space has no position of its own yet.
        lib.filterAttrs (_: v: v != null) {
          inherit (space.placement) x y;
        } // {
          inherit (space.placement) w h anchored;
        };
      config = (spaceConfig space) // { spaces = space.children; };
    });

  spacesDir = pkgs.runCommand "patchspace-spaces" {
    # The validated config *is* the config the daemon loads: an invalid space
    # fails the build here rather than half-loading at runtime.
    buildInputs = lib.optionals cfg.validate (lib.mapAttrsToList spaceCheck spaces);
  } ''
    mkdir -p $out
    ${concatStringsSep "\n" (lib.mapAttrsToList (name: space:
      "ln -s ${spaceFile name space} $out/${name}.json") spaces)}
  '';

  socketArgs = lib.optionals (socket != null) [ "--socket" (toString socket) ];

  # The daemon *replaces* its default space directory as soon as one
  # `--space-dir` is given, so the conventional (imperative) directory has to
  # be passed explicitly here - otherwise enabling this module would hide the
  # spaces the GUI makes by hand.  The generated declarative directory goes
  # last: later dirs shadow earlier ones, so a space defined in Nix wins over
  # a file with the same stem anywhere else.
  spaceDirArgs = lib.concatMap (d: [ "--space-dir" d ]) daemonDirs
    ++ [ "--space-dir" "${spacesDir}:ro" ];

  rootSpaceArgs = lib.optionals (rootSpace != null) [
    "--root-space" (toString rootSpace)
  ];

  # Which built-in modules run.  A store file (like the spaces) so the daemon
  # can be pointed at it declaratively; the daemon's CLI then reports the set
  # as deployment-managed rather than editing it.
  modulesConfig = pkgs.writeText "patchspace-modules.json" (builtins.toJSON {
    modules = lib.mapAttrs (_name: entry: { enable = entry.enable; }) cfg.modules;
  });
  modulesConfigArgs = lib.optionals (cfg.modules != { }) [
    "--modules-config" (toString modulesConfig)
  ];

  # `type` may be omitted on an override (it comes from the import), so the
  # merged result is what has to have one for every node.  Reported as a
  # build-time assertion naming the offending space and ids, rather than
  # letting `null` reach the daemon.
  typelessNodes = lib.concatMapStringsSep ", " (name:
    let
      merged = spaceConfig spaces.${name};
      missing = builtins.filter
        (nid: (merged.nodes.${nid}.type or null) == null)
        (builtins.attrNames merged.nodes);
    in
    lib.optionalString (missing != [ ])
      "${name}: ${concatStringsSep ", " missing}"
  ) (builtins.attrNames spaces);

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
    # Only needed when a root space is configured somewhere the daemon
    # wouldn't have created for itself (its own default lives under ~/.cache,
    # which already exists).
    execStartPre = lib.optionals (rootSpace != null) [
      "${pkgs.coreutils}/bin/mkdir -p ${builtins.dirOf (toString rootSpace)}"
    ];
    execStart = concatStringsSep " " ([
      "${cfg.package}/bin/patchspace-daemon"
    ] ++ socketArgs ++ spaceDirArgs ++ rootSpaceArgs ++ modulesConfigArgs ++ cfg.extraArgs);
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
  # Pre-rename option names (S0: panel -> space): a configuration written
  # before the rename keeps evaluating, with nixpkgs' usual rename warning.
  # They go away in S1 (agents/SPACES.md §9).  `panelsDir` is internal and
  # read-only, so it is aliased by definition below instead - a renamed
  # option module cannot forward into a read-only option.
  imports = [
    (lib.mkRenamedOptionModule [ "services" "patchspace" "panelDirs" ]
      [ "services" "patchspace" "spaceDirs" ])
    (lib.mkRenamedOptionModule [ "services" "patchspace" "rootPanel" ]
      [ "services" "patchspace" "rootSpace" ])
    (lib.mkRenamedOptionModule [ "services" "patchspace" "panels" ]
      [ "services" "patchspace" "spaces" ])
  ];

  # The spaces are handed to the daemon by closure; the check above rides
  # along with the unit so `nixos-rebuild` builds (and therefore validates)
  # the config it is about to load, and a rebuild lands a *new* store path in
  # ExecStart - which is what restarts the daemon with the new spaces.

  options.services.patchspace = {
    enable = mkEnableOption "the Patch Space daemon (and its declarative spaces)";

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

        A patchspace is a daemon plus a set of space files, and neither of
        those is something you can *open*: the window is a separate app, with
        the `.desktop` entry and icon travelling in its package.  Installing
        it here means `services.patchspace.enable = true` is the whole
        configuration; set this to false if you install the client yourself.

        The client is its own flake, so a configuration that installs it says
        where it comes from:

        ```nix
        services.patchspace.clientPackage =
          inputs.patchSpaceGui.packages.${pkgs.system}.patchspace;
        ```
      '';
    };

    clientPackage = mkOption {
      type = types.nullOr types.package;
      default = defaultClient;
      defaultText = literalExpression "null";
      description = ''
        The GUI installed when `installClient` is set.  `null` (the default)
        means this module cannot know it — the client is its own flake, so a
        configuration that wants it installed points this at that flake's
        `patchspace` package.  `installClient` on with this left null is an
        evaluation error, not a silent no-op.
      '';
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

    spaceDirs = mkOption {
      type = types.nullOr (types.listOf types.str);
      default = null;
      example = literalExpression ''
        [
          "%h/.local/share/patchspace/spaces:rw"
          "/etc/patchspace/spaces:ro"
        ]
      '';
      description = ''
        Space directories to load, in the daemon's own `PATH[:rw|:ro]` form:
        searched in order, later dirs shadow earlier ones.  The generated
        declarative directory is always appended **last and read-only**, so a
        space written in Nix wins over a file with the same stem anywhere else.

        `null` (the default) is the daemon's conventional directory - where
        the GUI keeps the spaces you make by hand - because handing the daemon
        any `--space-dir` at all replaces its built-in default.  Pass `[]` to
        load nothing but the declarative spaces.  A home-manager value
        overrides a NixOS-scope one (see the composition note above).
      '';
    };

    rootSpace = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Root space file: the session autosave (placements, plus any node you
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
        leaves the desktop showing through the grid, with nodes, spaces and
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
      description = "Exported JSON merged under the `main` space.";
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
      description = "Nodes of the `main` space.";
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
      description = "Edges of the `main` space.";
    };

    groups = mkOption {
      type = types.listOf types.attrs;
      default = [ ];
      description = "Groups of the `main` space.";
    };

    spaces = mkOption {
      type = types.attrsOf (types.submodule spaceModule);
      default = { };
      description = ''
        Declarative spaces, keyed by stem (the file name the daemon sees).
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
        Run every generated space through the repo's `session_repair` at
        build time.  Catches unknown node types, ports that don't exist,
        kind mismatches (an impulse wire into an audio input), duplicate
        edges and missing endpoints before the daemon ever sees the config.
      '';
    };

    spacesDir = mkOption {
      type = types.path;
      readOnly = true;
      internal = true;
      description = ''
        The generated read-only space directory handed to the daemon.  Set by
        this module; exposed so a deployment can inspect or `nix build` it
        (that build is what runs the validation).
      '';
    };

    panelsDir = mkOption {
      type = types.path;
      readOnly = true;
      internal = true;
      description = ''
        Pre-rename spelling of `spacesDir` (S0: panel -> space), kept as an
        alias for one release so an existing
        `nix build ...services.patchspace.panelsDir` keeps working.  Goes
        away in S1.
      '';
    };

    modules = mkOption {
      type = types.attrsOf (types.submodule {
        options.enable = mkOption {
          type = types.bool;
          default = true;
          description = "Run this built-in module.";
        };
      });
      default = { };
      example = { primitives.enable = false; };
      description = ''
        Which built-in modules the daemon runs.  Anything not listed keeps its
        own default (the modules the daemon ships are on).  The set is written
        as a store file and handed to the daemon with `--modules-config`, so
        the daemon's CLI reports it as deployment-managed and refuses to edit
        it (change it here instead).
      '';
    };

    modulesConfig = mkOption {
      type = types.path;
      readOnly = true;
      internal = true;
      description = ''
        The generated module-enable file handed to the daemon.  Set by this
        module; exposed so a deployment can inspect it.
      '';
    };

    effectiveConfig = mkOption {
      type = types.attrsOf types.anything;
      readOnly = true;
      internal = true;
      description = ''
        The flattened, merged configuration per space (`{ <stem> = { nodes,
        edges, groups }; }`) that the generated space files are built from -
        i.e. the NixOS scope with the home-manager scope layered on top.  Set
        by whichever scope owns the daemon; exposed so a deployment (and the
        flake's own checks) can see exactly what the daemon loads.
      '';
    };
  };

  config = mkIf (cfg.enable && !hmOwns) (mkMerge [
    {
      services.patchspace.spacesDir = spacesDir;
      # The pre-rename spelling of `spacesDir` (S0) - see the option above.
      services.patchspace.panelsDir = spacesDir;
      services.patchspace.modulesConfig = modulesConfig;
      services.patchspace.effectiveConfig =
        lib.mapAttrs (_: patchspaceLib.spaceConfig) spaces;
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
      home.packages = mkIf (cfg.installClient && cfg.clientPackage != null) [ cfg.clientPackage ];
    } else {
      environment.sessionVariables = mkIf (canvasOpacity != null) {
        PATCHSPACE_CANVAS_OPACITY = toString canvasOpacity;
      };
      environment.systemPackages = mkIf (cfg.installClient && cfg.clientPackage != null) [ cfg.clientPackage ];
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

      # (A rebuild rewrites the space files into a *new* store path, which
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
          assertion = !(cfg.installClient && cfg.clientPackage == null);
          message = ''
            services.patchspace.installClient is on but clientPackage is null.
            The GUI is its own flake, so say where it comes from:

              services.patchspace.clientPackage =
                inputs.patchSpaceGui.packages.''${pkgs.system}.patchspace;

            (or this flake's own `packages.patchspace` until the split lands),
            or set installClient = false to install the client yourself.
          '';
        }
      ];
    }
  ]);
}
