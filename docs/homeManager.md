# Patch Space with home-manager

The home-manager module exposes **exactly the same options** as the NixOS one
([nixos.md](./nixos.md) documents them) and **layers on top of it**: the user scope wins per
node, per edge, per panel and per scalar, so a graph - or a single override - can be written in
either place.

## Quick start, standalone (no NixOS)

Everything lives in your home configuration; the module owns the daemon, which is a *user*
service because it drives your PipeWire session.

```nix
{
  inputs.patchspace.url = "github:kyleraykbs/patchSpace";

  # in your home-manager configuration:
  imports = [ inputs.patchspace.homeModules.patchspace ];
  services.patchspace = {
    enable = true;                    # daemon unit, panels, and the client
    nodes.vol = { type = "volume"; params.initial_volume = 0.8; };
    nodes.out = { type = "patchspace_device"; params.label = "Speaker Line"; };
    edges = [ { from = "vol"; to = "out"; } ];
  };
}
```

The client is installed with `home.packages` (via `installClient`), the daemon as
`systemd.user.services.patchspace`, and the generated panels are handed to it exactly as in the
NixOS case.

## Quick start, together with NixOS

Declare the daemon and the shared part of the graph at the system level, then add or override
in your home configuration:

```nix
# NixOS side: /etc/nixos or your host module
services.patchspace = {
  enable = true;
  nodes.mic = { type = "patchspace_mic_device"; params.label = "Mic"; };
  panels.kit = { label = "Kitchen"; placement.w = 520; };
};

# home-manager side: layers on top of the above
services.patchspace = {
  enable = true;
  # override a field the system scope set (the rest of the node survives)
  nodes.mic.params.label = "My Mic";
  # add nodes, edges and whole panels of your own
  nodes.post = { type = "normalize"; params = { }; };
  edges = [ { from = "mic"; to = "post"; } ];
  panels.kit.nodes.snare = { type = "button"; params.label = "Snare"; };
  canvasOpacity = 0.75;               # beats the system scope's value
};
```

**Enabling it in the user scope hands the daemon to that user** - it is a user service and
belongs with the user's own merged configuration. The NixOS scope then defines no unit at all
(for that user), so there is never a second definition of the same unit fighting over the
graph. Enable it in *either* place; enabling both is the "system-wide defaults, per-user
overrides" arrangement above.

## What layers, and how

| Setting | Composition |
|---|---|
| `nodes`, `edges`, `groups` | Merged per id / per edge identity; the user scope wins the fields it sets, everything else survives. |
| `panels`, `panels.<name>.*` | Merged per stem, then per field; `children` and `imports` accumulate, `placement` merges per key. |
| `imports` | Appended after the NixOS scope's, so they layer under the same Nix-side nodes. |
| `socket`, `panelDirs`, `rootPanel`, `canvasOpacity` | User scope wins when it sets one (`null` means "unset", so the lower scope's value stays). |
| `extraArgs` | Concatenated (system first). |
| `enable` | The scope that enables it owns the daemon; the other defines nothing. |
| `installClient`, `clientPackage`, `package`, `validate` | Taken from the **owning** scope: the one with `enable = true`. The client lands in that scope's profile (`home.packages` or `environment.systemPackages`). |

Two consequences worth knowing:

* A home-manager user who sets `enable = true` takes the daemon over from the NixOS scope, by
  design - that is what makes "user overrides the system defaults" work. If you want the
  system-wide daemon (every user gets it, no per-user merge), enable it only in the NixOS
  scope.
* The NixOS scope's `rootPanel`/`panelDirs` still apply unless the user scope sets its own -
  the session autosave and the hand-made panel directory are shared state, not per-scope ones.

## Inspecting and validating

Both scopes generate the same artifacts, and the *owning* scope's are the ones in play:

```console
$ nix build .#nixosConfigurations.<host>.config.services.patchspace.panelsDir   # system scope
$ nix build .#homeConfigurations.<user>.config.services.patchspace.panelsDir    # user scope
```

`services.patchspace.effectiveConfig` gives the same content as an attrset - including the
lower scope's contributions - which is handy for tests:

```nix
assert config.services.patchspace.effectiveConfig.main.nodes.mic.params.label == "My Mic";
```

The flake's own `checks.<system>.module-scopes` check asserts this composition by pure
evaluation (NixOS scope first, then home-manager with it as `osConfig`), including that only
one of the two defines the unit.

## Notes and caveats

* The daemon needs a running PipeWire/WirePlumber for *your* user - true on any graphical
  session, and the unit is ordered `After=` both.
* `canvasOpacity` is exported to the session as `PATCHSPACE_CANVAS_OPACITY` *and* carried by the
  daemon, so a window started from a launcher (possibly with a stale session environment) still
  gets it.
* The client is a normal app: it talks to the daemon over the socket and adopts one that is
  already running (it only stops a daemon it started itself). That is what makes the
  "user scope owns the daemon" arrangement safe.
* The same operational notes as [nixos.md](./nixos.md#operational-notes) apply: one daemon per
  session, socket in `$XDG_RUNTIME_DIR`, never run it as root, and re-lock `path:` inputs after
  every edit to the patch tree.
