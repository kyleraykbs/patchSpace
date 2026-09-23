# Patch Space on NixOS

`services.patchspace` runs the daemon as a **user** service and hands it a generated,
**read-only** panel directory - so the graph is config in the store, and the state you make in
the GUI stays state. Enabling it is the whole configuration: daemon, panels, and the client you
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
daemon runs and the panels you make in the GUI - the daemon's own writable panel directory - are
the graph. With nothing declared here, no panel files are generated at all (an *empty* generated
panel would still opt in with `auto_load`, so the daemon would adopt it and an empty "Main"
panel would appear on your canvas).

## What the module does

* **Daemon**: `systemd.user.services.patchspace`, ordered `After=`/`Wants=`
  `pipewire.service` and `wireplumber.service`, `Restart=on-failure`, started at
  `default.target`. It drives the logged-in user's PipeWire session, which is why it is a user
  service and not a system one.
* **Graph**: every declared panel is written as a `mode = "read-only"`, `auto_load = true`
  panel file in a store directory passed to the daemon **last and read-only**. The daemon never
  writes it back, and `Reset` in the GUI re-applies exactly what Nix said.
* **Client**: `installClient` (default `true`) puts the GUI - window, `.desktop` entry and icon
  - into `environment.systemPackages`. Set it to `false` if you install it yourself.
* **Session environment**: `canvasOpacity` is exported as `PATCHSPACE_CANVAS_OPACITY` for the
  GUI, and put on the daemon unit as well, so a window started by a launcher (whose
  environment predates the rebuild) still gets it.

## Declaring a graph

The top-level `nodes`/`edges`/`groups`/`imports` describe the **`main`** panel; `panels.<name>`
adds more. Both forms are the same thing - `panels.main` is merged under the top-level
shorthand.

```nix
services.patchspace = {
  # Anything export_config.py wrote, or a panel file the GUI exported. It is
  # merged *under* the Nix-side definition: Nix wins per node, per edge.
  imports = [ ./exports/live-session.json ];

  nodes = {
    mic = { type = "patchspace_mic_device"; params.label = "Mic"; };
    # An override of a node the import already defines does not restate its
    # type; the imported fields it does not mention survive.
    boom.params.path = "~/sounds/boom.wav";
  };
  edges = [ { from = "mic"; to = "boom"; } ];

  panels.kit = {
    label = "Kitchen";
    placement = { w = 520; h = 320; };      # x/y left unset: see below
    nodes.prep = { type = "splitter"; params = { }; };
    children = [ "snare" ];                 # a nested placement of another panel
  };
  panels.snare = {
    label = "Snare";
    nodes.snare = { type = "button"; params.label = "Snare"; };
  };
};
```

Every generated panel is validated at build time with the repo's own `session_repair`
(`validate = true`, the default): a bad port, a kind mismatch (an impulse wire into an audio
input), a duplicate edge, a missing endpoint or an unknown node type **fails the build** rather
than half-loading in the daemon. To see what the daemon will load - and to run that validation
by hand - build the directory:

```console
$ nix build .#nixosConfigurations.<host>.config.services.patchspace.panelsDir
$ cat result/main.json
```

`services.patchspace.effectiveConfig` is the same content as an attrset
(`{ <stem> = { nodes, edges, groups }; }`), useful for assertions and for other tooling.

### Placement

`placement.w`/`h` size a panel's *box*; `anchored` (default `true`) pins the box so the layout
cannot shove it around. **`x`/`y` are unset by default**, which means "this panel has no
position of its own": the daemon places such a panel **beside the panels that do have one** (to
their right, top-aligned) and remembers that in the root panel, instead of leaving it at the
canvas origin. Set `x`/`y` when you want to be explicit; drag it in the GUI otherwise.

Nodes inside a panel keep whatever `x`/`y` the file has; nodes with no coordinates are settled
by the layout (only nodes the *user* placed are anchored at the node level, so imported and
Nix-declared nodes arrange themselves).

## Options

| Option | Type | Default | Meaning |
|---|---|---|---|
| `enable` | bool | `false` | Run the daemon, generate the panels, install the client. |
| `package` | package | daemon from this flake | The daemon binary the unit runs. |
| `installClient` | bool | `true` | Put the GUI (and its desktop entry/icon) in this scope's profile. |
| `clientPackage` | package | GUI from this flake | What `installClient` installs. |
| `socket` | `str?` | `null` | Command-API socket. `null` = the daemon's default, `$XDG_RUNTIME_DIR/patchspace.sock`. |
| `panelDirs` | `[str]?` | `null` | Daemon `PATH[:rw\|:ro]` entries, searched in order. `null` = the conventional `~/.local/share/patchspace/panels:rw` (where the GUI keeps hand-made panels), which is passed explicitly because handing the daemon any `--panel-dir` replaces its own default. `[]` = only the declarative panels. |
| `rootPanel` | `str?` | `null` | The session autosave file. `null` keeps the daemon's own (`~/.cache/patchspace/last_session.json`). |
| `canvasOpacity` | `float 0..1?` | stylix's `opacity.applications` | Canvas background opacity, exported to the GUI. |
| `imports` | `[path]` | `[]` | JSON merged **under** the `main` panel. |
| `nodes` | attrs | `{}` | `{ <id> = { type, params }; }` for the `main` panel. |
| `edges` | list | `[]` | `{ from, to, from_port?, to_port? }`. |
| `groups` | list | `[]` | Groups of the `main` panel. |
| `panels` | attrs | `{}` | Declarative panels, keyed by stem (the file name the daemon sees). |
| `extraArgs` | `[str]` | `[]` | Extra daemon arguments (see `patchspace-daemon --help`). |
| `validate` | bool | `true` | Run every generated panel through `session_repair` at build time. |
| `panelsDir` | path | *internal* | The generated read-only panel directory (the build that validates). |
| `effectiveConfig` | attrs | *internal* | The merged configuration per panel. |

`panels.<name>.color` (default `@blue`) is a hex **or a theme slot** - `@blue`, `@green`,
`@yellow`, `@red`, `@purple`, `@teal`.  A slot is resolved against the *current* theme every
time the panel is drawn, so a panel colored `@blue` follows the desktop (stylix recolors those
slots) rather than freezing one hex value.  The GUI's color pickers store the same slot values.

Per-panel options (`panels.<name>.*`): `label`, `color`, `autoLoad`, `placement`
(`x`, `y`, `w`, `h`, `anchored`), `imports`, `nodes`, `edges`, `groups`, `children`.

## Using it together with home-manager

The home-manager module mirrors these options exactly and **layers on top of this one** - set
the daemon up here, override a node or add a panel in your home configuration, and the user
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
* **A rebuild restarts the daemon** (the generated panel directory is a new store path, which
  changes the unit's `ExecStart`). Bringing the saved session back up takes a few seconds per
  backed node - the GUI shows a loading overlay while it happens.
* **A `path:` flake input must be re-locked** after every edit to the patch tree
  (`nix flake update patchspace`); a stale `flake.lock` silently evaluates the old tree.
* The daemon logs render "why" at `journalctl --user -u patchspace`; the GUI's console page
  shows the same lines.
