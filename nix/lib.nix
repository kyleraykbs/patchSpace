# Pure helpers for composing Patch Space configuration in Nix.
#
# A "config" is exactly what `export_config.py` writes and `apply_config.py`
# reads back: { nodes = { <id> = { type, params }; }; edges = [ { from, to,
# to_port?, from_port? } ]; groups = [ ... ]; }.  Every field the daemon
# serializes is keyed by node id and every edge has a stable identity, which
# is what makes the merge below a plain attrset union instead of a list
# splice.
{ lib }:

let
  inherit (builtins) foldl';

  # The daemon's own edge identity (PatchSpace._edge_id): a named target port
  # appends ":port", a named source port "@port", so the common in/out edge
  # keeps the bare "a->b" form.
  edgeKey = e:
    "${e.from}->${e.to}"
    + (if (e.to_port or "in") != "in" then ":${e.to_port}" else "")
    + (if (e.from_port or "out") != "out" then "@${e.from_port}" else "");

  # Edges are a *list* in the file format, so a merge has to go through an
  # attrset keyed by identity; attrValues then orders them deterministically
  # (by key), which keeps the generated JSON stable across evaluations.
  edgesToAttrs = edges: lib.listToAttrs (map (e: lib.nameValuePair (edgeKey e) e) edges);

  mergeEdges = a: b: lib.attrValues (edgesToAttrs a // edgesToAttrs b);

  groupsToAttrs = groups: lib.listToAttrs (map (g: lib.nameValuePair g.id g) groups);

  mergeGroups = a: b: lib.attrValues (groupsToAttrs a // groupsToAttrs b);

  empty = { nodes = { }; edges = [ ]; groups = [ ]; };

  # A node entry may omit `type`: that is how an override of a node an
  # *import* already defines is written (`nodes.boom.params.path = ...`), and
  # the type comes from the import.  A null type must therefore never
  # overwrite one, so it is dropped before the merge.  A node that exists
  # nowhere still needs a real type; the module asserts that after merging.
  dropNullType = lib.mapAttrs (_: n:
    lib.filterAttrs (k: v: !(k == "type" && v == null)) n);

  # Later configs win, at *field* granularity: a node present in both keeps
  # the later config's params (and any param it doesn't mention), an edge
  # present in both is one edge, a group likewise.  Any of the three sections
  # may be missing (a hand-written config, a partial import).
  mergeOne = a: b: {
    nodes = lib.recursiveUpdate (dropNullType (a.nodes or { })) (dropNullType (b.nodes or { }));
    edges = mergeEdges (a.edges or [ ]) (b.edges or [ ]);
    groups = mergeGroups (a.groups or [ ]) (b.groups or [ ]);
  };

  # Serializable-only view of a panel: everything the daemon's panel file
  # format knows, with the imports merged *under* the Nix-side definition -
  # the panel's own `nodes`/`edges` come last, so Nix wins per node and per
  # edge (that is the whole point of writing a panel in Nix on top of an
  # export).
  #
  # An import may be a path to JSON (what `export_config.py` wrote, or a panel
  # file the GUI exported) or an inline attrset; the GUI has export paths for
  # both shapes, so accept either.
  normalise = raw:
    if (raw.type or null) == "panel" then (raw.config or { }) else raw;

  readConfig = p:
    normalise (builtins.fromJSON (builtins.readFile p));

  asConfig = p: if builtins.isPath p || builtins.isString p then readConfig p else normalise p;

  panelConfig = panel:
    foldl' mergeOne empty (map asConfig (panel.imports or [ ])
      ++ [ { nodes = panel.nodes; edges = panel.edges; groups = panel.groups; } ]);

in
{
  inherit edgeKey mergeEdges mergeGroups mergeOne panelConfig normalise
    readConfig asConfig empty;
  mergeConfigs = configs: foldl' mergeOne empty (map asConfig configs);
}
