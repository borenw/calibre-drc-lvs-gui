# Calibre DRC / LVS GUI

A dependency-free, browser-based GUI for launching **Siemens Calibre** DRC and LVS
runs and comparing results — driven by a tiny local web server so that Calibre and
Cadence tools run **in the same shell environment you launched from** (licenses,
`PATH`, module setup all inherited).

No Flask, no pip installs — Python 3.6+ standard library only. The whole app is a
**single file**, so installing = downloading that one file.

## Install & run — one command

Copy this (use the 📋 button on the box), paste in your terminal, press Enter:

```bash
curl -fsSL https://raw.githubusercontent.com/borenw/calibre-drc-lvs-gui/main/calibre_drc_lvs_gui.py -o calibre_drc_lvs_gui.py && python3 calibre_drc_lvs_gui.py --open
```

That's it — it downloads the script and opens the GUI in your browser. Run it from a
shell where your Calibre/Cadence modules are set up, **or** just click **Load modules**
in the app once it opens.

<sub>Prefer a helper script? `curl -fsSL https://raw.githubusercontent.com/borenw/calibre-drc-lvs-gui/main/install.sh | bash`</sub>

## Why a local server (not just an `.html` file)

A static HTML page can't run shell commands. To launch Calibre in *your* environment,
this script runs a small HTTP server **in your current shell**; the browser talks to
`localhost`, and every `strmout` / `calibre` call is a subprocess that inherits
`os.environ` verbatim. So there's no new terminal and nothing to re-source.

## Updating

> **Note:** `raw.githubusercontent.com` is CDN-cached for ~5 minutes, so right after
> a push it may serve the previous file. To fetch the **latest** immediately, use the
> GitHub API endpoint (bypasses the cache):

```bash
curl -fsSL -H "Accept: application/vnd.github.raw" \
  "https://api.github.com/repos/borenw/calibre-drc-lvs-gui/contents/calibre_drc_lvs_gui.py?ref=main" \
  -o calibre_drc_lvs_gui.py
```

The current build number is shown **top-right in the GUI** (e.g. `rev 24`) — check it
matches the latest commit after updating.

## Later runs & configuration

Once downloaded, just:

```bash
python3 calibre_drc_lvs_gui.py --open   # then open the printed http://127.0.0.1:8899/
```

First run creates `calibre_gui_config.json` next to the script. Point it at your
PDK/site values (see `config.example.json`) via the **Config** tab in the browser,
or with environment variables (`CDS_LIB`, `LAYERMAP`, `DRC_DECK`, `DRC_DECK_GLOB`,
`LVS_DECK`, `EDA_MODULES`, …).

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--port N` | `8899` | port to serve on |
| `--host H` | `127.0.0.1` | bind address |
| `--base DIR` | `./calibre_runs` | where run outputs + the run registry live |
| `--config F` | `./calibre_gui_config.json` | config file (persisted from the Config tab) |
| `--open` | off | try to open a browser |
| `--log PATH` | — | prefill the Run tab from an existing result log on startup; pass twice to also preset the Compare tab (A/B) |

## Features

- **Run DRC / LVS** on a selected library / cell / view. The tool streams the layout
  out of OpenAccess (`strmout` → GDS), generates a Calibre SVRF runset that `INCLUDE`s
  your rule deck, and launches `calibre -drc` / `-lvs`. You can also point at an
  existing GDS to skip stream-out.
- **Searchable inputs** — library / cell / view are type-to-filter combo boxes.
- **Prefill from a log** — paste a `.drc.summary` / `.lvs.report` / runset / strmout
  log and it fills tool + lib + cell + view (inferring the library from `cds.lib`
  when the log doesn't name it).
- **Log search** — scan configurable roots for result logs, with sortable columns
  and a spinner. **Auto-discovers run directories** by parsing Calibre Interactive
  state (`~/.cgidrcdb`, `~/.cgilvsdb`, and `*Runset*` files → `*RunDir`), so it finds
  your logs wherever you actually ran them without configuring paths. Bounded by an
  8 s wall-clock budget so a slow/NFS root can't hang the UI.
- **Deck auto-discovery** — globs your deck directory and auto-selects the **newest
  revision** (so you never point at a stale, deleted deck version).
- **Environment auto-load** — if `strmout` / `calibre` aren't on `PATH`, it runs
  `module load <modules>` for you (sourcing the Environment Modules / Lmod init so
  `module` works even in a bare shell) and merges the resulting env.
- **Live progress bar with ETA** — estimates % and time-remaining by comparing the
  growing run log against your most recent comparable run; indeterminate animation
  when there's no baseline.
- **Result parsing** — DRC per-rule violation counts; LVS CORRECT / INCORRECT plus
  unmatched instance/net/port tallies.
- **Compare** two results (from run history or any two pasted log paths): per-rule
  DRC diff (improved / worse / new), LVS status change, and a raw unified text diff.
  An **Easy compare** button auto-fills the two most recent logs, labeled by cell.
- **⚡ Easy** button — one click: load modules → find your latest DRC log → re-run it
  with the newest deck → live progress.
- **Debug log** at `<base>/gui_debug.log`, also viewable from the env banner.

## Configuration

Nothing site-specific is baked into the script. Keys (all editable in the Config tab):

| Key | Example | Notes |
|-----|---------|-------|
| `cds_lib` | `/proj/cds.lib` | `cds.lib` that `DEFINE`s your OA libraries |
| `techlib` / `layermap` | `<tech_lib>` / `.../stream.layermap` | stream-out settings |
| `drc_deck` / `lvs_deck` | deck file paths | INCLUDEd by the generated runset |
| `drc_deck_glob` / `lvs_deck_glob` | `.../DECK.*` | newest match auto-selected |
| `modules` | `calibre cadence/ic618` | auto-`module load`ed when tools are missing |
| `sim_roots` | `/sim/{user}` … | log-search roots (`{user}` expands) |
| `*_cmd` | command templates | `strmout` / `calibre` / `module load` invocations |

> **Note on `strmout`:** it ships with **Cadence Virtuoso** (e.g. `cadence/ic618`),
> not the Calibre module — so `modules` usually needs both, e.g.
> `calibre cadence/ic618`.

## How it works

```
lib/cell/view ──strmout──▶ cell.calibre.db (GDS)
                                │
        generated SVRF runset ──┤  LAYOUT PATH / PRIMARY + INCLUDE <deck>
                                ▼
                    calibre -drc / -lvs  ──▶  cell.drc.summary / cell.lvs.report
                                                        │
                                                   parsed + compared
```

Each run lands in `calibre_runs/<tool>_<lib>_<cell>_<view>_<timestamp>/` with the
generated runset, the full `run.log`, the result report, and a `metadata.json` used
by the History and Compare tabs.

## Requirements

- Python 3.6+ (standard library only)
- Siemens Calibre (`calibre`) and, for OA stream-out, Cadence Virtuoso (`strmout`)
- A browser on the same host (or via SSH port-forward / X)

## Troubleshooting

The console prints numbered `===== STEP N =====` phase banners, the exact command
per step (`------ command used for X ------`), a 5-second heartbeat, and errors as
`-E-` lines. The current build shows **top-right in the GUI** (`rev N`).

| Symptom | Cause & fix |
|---------|-------------|
| `ImportError: ThreadingHTTPServer` | Python < 3.7. Run with `python3` (works on 3.5+ now); check `python3 --version`. |
| `-E- No DRC rule deck configured` / `no deck matched glob` | The deck path isn't set/valid **on this host**. The tool now auto-detects it from your existing runsets/logs; if none, set `drc_deck` + `drc_deck_glob` (and `lvs_deck`) in the **Config** tab. Find it with `find / -iname "<DECK_NAME>*" 2>/dev/null`. Site/PDK paths differ per host. |
| `strmout: command not found` | `strmout` ships with **Cadence Virtuoso**, not Calibre. Set `modules` to include your Cadence module, e.g. `calibre cadence/ic618`, or `module load` both before launching. |
| `INCL1 … problem with access/file open` | The `INCLUDE`d deck isn't readable here — wrong path/mount, or you're not in the PDK unix group (`ls -l <deck>; id -nG`). |
| `GMD unable to load shared library` (strmout) | Cadence env issue on that host (`LD_LIBRARY_PATH` / version mismatch). Bypass it: use **Advanced → existing GDS** or the **Existing runsets** panel to run on a `.calibre.db` directly (skips stream-out). |
| Log search "stalled" / very slow | A search root is on a slow/stalled NFS mount. Each root now has a per-root timeout (abandoned + reported), but narrow **Config → `sim_roots`** to your fast log dir for speed. |
| Browser errors on `--open` (`XPCOMGlueLoad`, `gio`, `PNG…`) | A broken/remote browser. Run **without `--open`** and open the URL yourself, or forward it: `ssh -L 8899:127.0.0.1:8899 you@host`. |
| `server_bind` / address in use | The port is busy — the tool now auto-hops to the next free port and prints it. |
| Config "save error" | The config file isn't writable from your launch dir. Edit `calibre_gui_config.json` directly, or launch where you can write. |
| Updated but GUI still shows old `rev` | `raw.githubusercontent.com` is CDN-cached ~5 min. Use the API fetch in [Updating](#updating). |

**Per-host config:** nothing site-specific is baked in. Each machine keeps its own
`calibre_gui_config.json` (gitignored) — set `cds_lib`, `layermap`, decks, and
`modules` for that host once via the Config tab.

## License

MIT — see [LICENSE](LICENSE).
