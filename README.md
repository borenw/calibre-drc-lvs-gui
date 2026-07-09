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

## Features

- **Run DRC / LVS** on a selected library / cell / view. The tool streams the layout
  out of OpenAccess (`strmout` → GDS), generates a Calibre SVRF runset that `INCLUDE`s
  your rule deck, and launches `calibre -drc` / `-lvs`. You can also point at an
  existing GDS to skip stream-out.
- **Searchable inputs** — library / cell / view are type-to-filter combo boxes.
- **Prefill from a log** — paste a `.drc.summary` / `.lvs.report` / runset / strmout
  log and it fills tool + lib + cell + view (inferring the library from `cds.lib`
  when the log doesn't name it).
- **Log search** — scan `/sim/<user>` and other configurable roots for result logs,
  with sortable columns and a spinner.
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

## License

MIT — see [LICENSE](LICENSE).
