# Patch Space on NixOS

`services.patchspace` runs the daemon as a **user** service and hands it a generated,
**read-only** space directory - so the graph is config in the store, and the state you make in
the GUI stays state. Enabling it is the whole configuration: daemon, spaces, and the client you
actually open.

```nix
{
  inputs.patchspace.url = "github:kyleraykbs/patchSpace";

  # in your flake's nixosConfigurations module:
  imports = [ inputs.patchspace.nixosModules.patchspace ];
  services.patchspace = {
    enable = true;
    nodes.boom = { type = "sound_effect"; params.path = "~/sounds/boom.wav"; };
    nodes.kick = { type = "button"; params.label = "Kick"; };
    edges = [ { from = "kick"; to = "boom"; } ];
  };
}
```

Then rebuild. The daemon starts as `patchspace.service` (a *user* unit) and the client lands in
`environment.systemPackages`, so `patchspace` and its desktop entry appear for every user.

**Declaring a graph is optional.** `enable = true` on its own is a complete configuration: the
daemon runs and the spaces you make in the GUI - the daemon's own writable space directory - are
the graph. With nothing declared here, no space files are generated at all (an *empty* generated
space would still opt in with `auto_load`, so the daemon would adopt it and an empty "Main"
space would appear on your canvas).

## What the module does

* **Daemon**: `systemd.user.services.patchspace`, ordered `After=`/`Wants=`
  `pipewire.service` and `wireplumber.service`, `Restart=on-failure`, started at
  `default.target`. It drives the logged-in user's PipeWire session, which is why it is a user
  service and not a system one.
* **Graph**: every declared space is written as a `type = "live"`,
  `mode = "read-only"`, `auto_load = true` space file in a store directory passed to the daemon
  **last and read-only**. The daemon never writes it back, and `Reset` in the GUI re-applies
  exactly what Nix said.  (`type` names the space's kind since S1: a generated space is live;
  the pre-S1 `"space"`/`"panel"` markers still load, and `lib.patchspace.normalise` unwraps all
  of them when a `spaces.<name>.imports` entry is a GUI-exported space file rather than a bare
  config.)
* **Client**: the GUI is its own repo/flake (`patchSpaceGui`), so this module no longer
  supplies one.  `installClient` (default `true`) installs `clientPackage` — which a
  configuration must name — into `environment.systemPackages` (or `home.packages` in the
  home-manager scope):

  ```nix
  services.patchspace.clientPackage =
    inputs.patchSpaceGui.packages.${pkgs.system}.patchspace;
  ```

  Leaving `clientPackage` unset with `installClient` on is an evaluation error (the
  `module-scopes` check asserts exactly that), and `installClient = false` means you
  install the window yourself.  The window, its `.desktop` entry and its icon all travel
  in that package.
* **Session environment**: `canvasOpacity` is exported as `PATCHSPACE_CANVAS_OPACITY` for the
  GUI, and put on the daemon unit as well, so a window started by a launcher (whose
  environment predates the rebuild) still gets it.
* **Modules**: the daemon runs the built-in modules and serves what they declare.  A module is
  a separate program that attaches to the daemon's socket, declares its node types and
  announces the nodes it runs; the GUI draws whatever it declares, so a module needs no GUI
  work of its own.  Today the daemon runs the **primitives** module (its value sources — a
  0.0–1.0 slider, a number and a text literal, an on/off — plus arithmetic, comparison,
  boolean logic and a display) as a child process; its log is
  `~/.cache/patchspace/modules/primitives.log`.
  Those children live in the unit's cgroup, so `systemctl --user stop patchspace` reaps them
  even though they are started in their own session (and the daemon stops them itself when it
  exits on purpose).  The PipeWire provider still runs *inside* the daemon (it is the next one
  to move out; see `agents/PLAN.md` phase 3), which is why nothing about the PipeWire side of
  the unit changes here.
* **Choosing the modules**: `services.patchspace.modules` decides which built-ins run —
  `{ primitives.enable = false; }` disables one, and anything not mentioned keeps its own
  default (the daemon's own modules are on).  The set is written as a store file and handed to
  the daemon with `--modules-config`, so the daemon's CLI
  (`patchspace-modules`, in `environment.systemPackages` via the client package) reports it as
  deployment-managed and refuses to edit it — change it here instead.  Without this option the
  daemon uses `~/.config/patchspace/modules.json`, which the CLI *does* edit (and which then
  applies live: disabling a module takes its nodes offline without touching the graph).

## Declaring a graph

The top-level `nodes`/`edges`/`groups`/`imports` describe the **`main`** space; `spaces.<name>`
adds more. Both forms are the same thing - `spaces.main` is merged under the top-level
shorthand.

```nix
services.patchspace = {
  # Anything export_config.py wrote, or a space file the GUI exported. It is
  # merged *under* the Nix-side definition: Nix wins per node, per edge.
  imports = [ ./exports/live-session.json ];

  nodes = {
    mic = { type = "patchspace_mic_device"; params.label = "Mic"; };
    # An override of a node the import already defines does not restate its
    # type; the imported fields it does not mention survive.
    boom.params.path = "~/sounds/boom.wav";
  };
  edges = [ { from = "mic"; to = "boom"; } ];

  spaces.kit = {
    label = "Kitchen";
    placement = { w = 520; h = 320; };      # x/y left unset: see below
    nodes.prep = { type = "splitter"; params = { }; };
    children = [ "snare" ];                 # a nested placement of another space
  };
  spaces.snare = {
    label = "Snare";
    nodes.snare = { type = "button"; params.label = "Snare"; };
  };
};
```

Every generated space is validated at build time with the repo's own `session_repair`
(`validate = true`, the default): a bad port, a kind mismatch (an impulse wire into an audio
input), a duplicate edge, a missing endpoint or an unknown node type **fails the build** rather
than half-loading in the daemon. To see what the daemon will load - and to run that validation
by hand - build the directory:

```console
$ nix build .#nixosConfigurations.<host>.config.services.patchspace.spacesDir
$ cat result/main.json
```

`services.patchspace.effectiveConfig` is the same content as an attrset
(`{ <stem> = { nodes, edges, groups }; }`), useful for assertions and for other tooling.

### Placement

`placement.w`/`h` size a space's *box*; `anchored` (default `true`) pins the box so the layout
cannot shove it around. **`x`/`y` are unset by default**, which means "this space has no
position of its own": the daemon places such a space **beside the spaces that do have one** (to
their right, top-aligned) and remembers that in the root space, instead of leaving it at the
canvas origin. Set `x`/`y` when you want to be explicit; drag it in the GUI otherwise.

Nodes inside a space keep whatever `x`/`y` the file has; nodes with no coordinates are settled
by the layout (only nodes the *user* placed are anchored at the node level, so imported and
Nix-declared nodes arrange themselves).

## Options

| Option | Type | Default | Meaning |
|---|---|---|---|
| `enable` | bool | `false` | Run the daemon, generate the spaces, install the client. |
| `package` | package | daemon from this flake | The daemon binary the unit runs. |
| `installClient` | bool | `true` | Put the GUI (and its desktop entry/icon) in this scope's profile. |
| `clientPackage` | package? | `null` | What `installClient` installs. The GUI is its own flake, so a configuration that installs it names it here (`inputs.patchSpaceGui.packages.<system>.patchspace`); `null` with `installClient` on is an evaluation error. |
| `socket` | `str?` | `null` | Command-API socket. `null` = the daemon's default, `$XDG_RUNTIME_DIR/patchspace.sock`. |
| `spaceDirs` | `[str]?` | `null` | Daemon `PATH[:rw\|:ro]` entries, searched in order. `null` = the conventional `~/.local/share/patchspace/spaces:rw` (where the GUI keeps hand-made spaces), which is passed explicitly because handing the daemon any `--space-dir` replaces its own default. `[]` = only the declarative spaces. |
| `rootSpace` | `str?` | `null` | The session autosave file. `null` keeps the daemon's own (`~/.cache/patchspace/last_session.json`). |
| `canvasOpacity` | `float 0..1?` | stylix's `opacity.applications` | Canvas background opacity, exported to the GUI. |
| `imports` | `[path]` | `[]` | JSON merged **under** the `main` space. |
| `nodes` | attrs | `{}` | `{ <id> = { type, params }; }` for the `main` space. |
| `edges` | list | `[]` | `{ from, to, from_port?, to_port? }`. |
| `groups` | list | `[]` | Groups of the `main` space. |
| `spaces` | attrs | `{}` | Declarative spaces, keyed by stem (the file name the daemon sees). |
| `modules` | attrs | `{}` | Which built-in modules the daemon runs (`{ primitives.enable = false; }`). Unlisted modules keep their own default. Written as a store file handed to the daemon with `--modules-config`. |
| `extraArgs` | `[str]` | `[]` | Extra daemon arguments (see `patchspace-daemon --help`). |
| `validate` | bool | `true` | Run every generated space through `session_repair` at build time. |
| `spacesDir` | path | *internal* | The generated read-only space directory (the build that validates). |
| `effectiveConfig` | attrs | *internal* | The merged configuration per space. |

`spaces.<name>.color` (default `@blue`) is a hex **or a theme slot** - `@blue`, `@green`,
`@yellow`, `@red`, `@purple`, `@teal`.  A slot is resolved against the *current* theme every
time the space is drawn, so a space colored `@blue` follows the desktop (stylix recolors those
slots) rather than freezing one hex value.  The GUI's color pickers store the same slot values.

Per-space options (`spaces.<name>.*`): `label`, `color`, `autoLoad`, `placement`
(`x`, `y`, `w`, `h`, `anchored`), `imports`, `nodes`, `edges`, `groups`, `children`.

**Pre-rename option names** (the container was called a *panel* until S0,
`agents/SPACES.md`): `panelDirs`, `rootPanel` and `panels` still evaluate (they
are aliased onto `spaceDirs`, `rootSpace` and `spaces`, with nixpkgs' rename
warning), `panelsDir` is still readable as an alias of `spacesDir`, and the node
types `panel_in`/`panel_out`/`bool_panel_in`/`bool_panel_out` still type-check.
The daemon accepts `--panel-dir`/`--root-panel` and reads the pre-rename
environment names for the same one-release window; they all go away in S1 (the
list is in `agents/NOTES.md`).

## Using it together with home-manager

The home-manager module mirrors these options exactly and **layers on top of this one** - set
the daemon up here, override a node or add a space in your home configuration, and the user
scope wins. Whichever scope is *enabled* owns the daemon unit, and never both. See
[homeManager.md](./homeManager.md) for the rules and examples.

## Operational notes

* **One daemon per session.** A second instance refuses to start rather than reaping the first
  one's graph - including a `patchspace` GUI-spawned daemon, which adopts a running one
  instead. If a unit ends up `failed` because something held the graph, `systemctl --user
  reset-failed patchspace && systemctl --user start patchspace`.
* **The socket** lives in `$XDG_RUNTIME_DIR` (a path the user owns), with
  `/tmp/patchspace.sock` only as the fallback for a session with no runtime dir. Set `socket`
  if you need it elsewhere - the GUI/CLI then need `PATCHSPACE_SOCKET` in the session too.
* **Never run the daemon as root.** It manages *your* PipeWire session; a root-owned socket in
  `/tmp` is also what made an earlier daemon fail to bind while still running.
* **A rebuild restarts the daemon** (the generated space directory is a new store path, which
  changes the unit's `ExecStart`). Bringing the saved session back up takes a few seconds per
  backed node - the GUI shows a loading overlay while it happens.
* **A `path:` flake input must be re-locked** after every edit to the patch tree
  (`nix flake update patchspace`); a stale `flake.lock` silently evaluates the old tree.
* The daemon logs render "why" at `journalctl --user -u patchspace`; the GUI's console page
  shows the same lines.
