#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibre_drc_lvs_gui.py  --  Browser GUI to launch Calibre DRC / LVS and compare runs.

WHY A LOCAL SERVER (and not just a static .html):
    A plain HTML file cannot run shell commands. To launch Calibre *in the same
    environment where you started this script* (so your license vars, PATH,
    MGC_HOME, etc. are all inherited) we run a tiny local web server IN THIS SHELL.
    Every Calibre / strmout call is a subprocess of this process, so it inherits
    os.environ verbatim -- exactly the shell you launched from. No new terminal,
    no re-sourcing your setup.

USAGE:
    # In the SAME shell where `calibre`, `strmout`, licenses, etc. are set up:
    python3 calibre_drc_lvs_gui.py
    # then open the printed URL, e.g. http://127.0.0.1:8899/

    Options:
      --port N      port to serve on (default 8899)
      --base DIR    base working dir for runs (default ./calibre_runs)
      --config F    config json path (default ./calibre_gui_config.json)
      --open        try to auto-open a browser (needs a display)

No third-party dependencies -- Python 3.6+ stdlib only.
"""

import sys

if sys.version_info < (3, 5):
    sys.stderr.write(
        "\n  This tool requires Python 3.5+  (you are running %s).\n"
        "  Run it with 'python3' instead of 'python'.\n\n"
        % sys.version.split()[0])
    raise SystemExit(1)

import argparse
import difflib
import fnmatch
import getpass
import glob as globmod
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    # Python 3.7+
    from http.server import ThreadingHTTPServer
except ImportError:
    # Python 3.6 and earlier: build the threaded server ourselves
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

# --------------------------------------------------------------------------- #
#  Configuration
#
#  All site/PDK-specific values default to blank or to an environment variable,
#  and are meant to be filled in via the Config tab (persisted to a local
#  <config>.json) or via env vars -- nothing about a particular site or PDK is
#  baked into this file. See config.example.json for a worked example.
# --------------------------------------------------------------------------- #

def _env(name, default=""):
    return os.environ.get(name, default)


DEFAULT_CONFIG = {
    # Executables (looked up on the inherited PATH, or give an absolute path).
    "calibre_bin": _env("CALIBRE_BIN", "calibre"),
    "strmout_bin": _env("STRMOUT_BIN", "strmout"),

    # cds.lib that DEFINEs your OA libraries (lib name -> path on disk).
    "cds_lib": _env("CDS_LIB", ""),                 # e.g. /path/to/project/cds.lib

    # Stream-out (OA -> GDS) defaults.
    "techlib": _env("TECHLIB", ""),                 # e.g. the Virtuoso tech library name
    "layermap": _env("LAYERMAP", ""),               # e.g. $PDK/.../stream.layermap

    # Calibre rule decks (the files INCLUDEd by the generated runset).
    "drc_deck": _env("DRC_DECK", ""),
    "drc_antenna_deck": _env("DRC_ANTENNA_DECK", ""),
    "lvs_deck": _env("LVS_DECK", ""),

    # Deck auto-discovery: glob for available deck revisions; the NEWEST (by
    # mtime, excluding .orig) is auto-selected so you never point at a stale
    # revision. A blank deck field on a run falls back to the latest match.
    "drc_deck_glob": _env("DRC_DECK_GLOB", ""),     # e.g. /pdk/.../MAIN_DRC/DECK.*
    "lvs_deck_glob": _env("LVS_DECK_GLOB", ""),

    # Command templates. {placeholders} are filled in per run, then shlex-split
    # and executed WITHOUT a shell (so inherited env is used, no re-quoting bugs).
    "strmout_cmd": ("{strmout_bin} -library {lib} -topCell {cell} -view {view} "
                    "-strmFile {gds} -logFile {strmlog} -techLib {techlib} "
                    "-layerMap {layermap} -case preserve -convertDot node "
                    "-convertPcellPin geometry"),
    "drc_cmd": "{calibre_bin} -drc -hier -turbo {runfile}",
    "lvs_cmd": "{calibre_bin} -lvs -hier -spice {spiceout} {runfile}",

    # ---- LVS source netlist (schematic -> CDL/SPICE) ---------------------- #
    # The LVS *source* is netlisted from the SCHEMATIC (not the layout). If a
    # source netlist for the cell already exists it is reused as-is; otherwise
    # the GUI generates one. "netlist_mode" selects how:
    #   si     -> Cadence auCDL netlister: write a si.env into the run dir and
    #             run `si <run_dir> -batch -command netlist` (ships with
    #             Virtuoso -- same module as strmout). This is the default.
    #   skill  -> run a generated SKILL script under `virtuoso -nograph`
    #             (deOpenCellViewByType + auCdl createNetlist).
    #   custom -> use `netlist_cmd` verbatim (a site wrapper / other netlister).
    #   off    -> never generate; require a pre-existing source netlist.
    # A non-blank "netlist_cmd" always wins (back-compat with older configs).
    "netlist_mode": _env("NETLIST_MODE", "si"),
    "si_bin": _env("SI_BIN", "si"),
    "virtuoso_bin": _env("VIRTUOSO_BIN", "virtuoso"),
    # Schematic view to netlist for the LVS source (usually "schematic" --
    # NOT the layout view used for strmout).
    "netlist_view": _env("NETLIST_VIEW", "schematic"),
    # Command templates. {placeholders}: run_dir cell lib netlist_view src_net
    # cdslib si_bin virtuoso_bin skill_script netlist_log calibre_bin.
    "si_cmd": "{si_bin} {run_dir} -batch -command netlist -cdslib {cdslib}",
    "netlist_skill_cmd": ("{virtuoso_bin} -nograph -replay {skill_script} "
                          "-log {netlist_log}"),
    # Fully custom netlister (wins over netlist_mode when non-blank).
    "netlist_cmd": _env("NETLIST_CMD", ""),

    # Environment / module auto-loading. If strmout/calibre are not on PATH,
    # the tool can run `module load <modules>` for you (in a login shell) and
    # merge the resulting environment so every later Calibre call inherits it.
    # Set to your site's module names, e.g. "calibre cadence/ic618".
    "modules": _env("EDA_MODULES", ""),
    # Source the Environment Modules / Lmod init (so `module` is defined even in
    # a bare bash), then load and dump the resulting env. Harmless if a given
    # init path is absent. Override in Config if your site differs.
    "module_load_cmd": (
        "bash -c '"
        'source "${MODULESHOME:-/usr/share/Modules}/init/bash" 2>/dev/null; '
        "source /etc/profile.d/modules.sh 2>/dev/null; "
        "source /etc/profile.d/lmod.sh 2>/dev/null; "
        "source /etc/profile.d/z00_lmod.sh 2>/dev/null; "
        "module load {modules} 1>&2; env -0'"),
    "auto_load_modules": "yes",   # yes|no

    # Log search. {user} is expanded to sim_user (or the login name if blank).
    # Roots are searched (bounded depth) for Calibre result logs.
    # discover_run_dirs: also auto-add run directories parsed from Calibre
    # Interactive state (~/.cgidrcdb, ~/.cgilvsdb, and *Runset* files -> *RunDir).
    "discover_run_dirs": "yes",
    # Easy button: skip designs whose result-log filename contains any of these
    # (space/comma separated) when auto-picking -- e.g. the slow top-level chip.
    "easy_skip_cells": "chipTop chip_top",
    "sim_user": "",
    "sim_roots": ("/sim/{user}\n"
                  "/sim/{user}/calibre\n"
                  "/home/{user}/sim\n"
                  "/home/{user}/simulation\n"
                  "~/simulation\n"
                  "~/sim\n"
                  "./calibre_runs"),

    # Extra SVRF lines injected into the generated runsets (optional).
    "drc_extra_svrf": "",
    "lvs_extra_svrf": "LVS REPORT OPTION NONE\nLVS RECOGNIZE GATES ALL",

    # Known-good LVS runset used as a TEMPLATE for EVERY LVS run: the tool copies
    # it and rewrites only the cell name + LAYOUT/SOURCE/REPORT paths, preserving
    # the deck INCLUDEs and all setup (crucially #!tvf / tvf::VERBATIM for TVF
    # decks, the MASK SVDB spec, LVS REPORT OPTION, #DEFINEs). Point this at a
    # passing _calibre.lvs_ once and every cell reuses it. Blank -> synthesize a
    # plain-SVRF runset (fine for plain-SVRF decks). A runset prefilled per-run
    # takes precedence over this.
    "lvs_runset_template": _env("LVS_RUNSET_TEMPLATE", ""),
}

CONFIG_LOCK = threading.Lock()
CONFIG = {}
CONFIG_PATH = None
RUNS_BASE = None
STARTUP_LOGS = []      # result logs passed on the command line (--log), for prefill/compare
APP_REVISION = 39      # incremental build number, shown top-right in the GUI


# Superseded module_load_cmd values -> auto-upgraded to the current default.
_OLD_MODULE_CMDS = {
    'bash -lc "module load {modules} 1>&2; env -0"',
    'bash -lc "module load {modules} && env -0"',
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        except Exception as e:
            sys.stderr.write("WARN: could not read config %s: %s\n" % (path, e))
    # migrate a stale module-load command to the improved default
    if cfg.get("module_load_cmd", "").strip() in _OLD_MODULE_CMDS:
        cfg["module_load_cmd"] = DEFAULT_CONFIG["module_load_cmd"]
    return cfg


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
#  cds.lib parsing + OA library scanning
# --------------------------------------------------------------------------- #

def resolve_cds_lib(cfg=None):
    """Return an EXISTING cds.lib file path, or '' if none can be found.

    Order: the configured cds_lib (if it's a real file) -> a cds.lib in the launch
    directory or any parent (how Cadence itself finds it) -> $CDS_LIB / $HOME.
    Never returns a directory or a blank->cwd artifact."""
    if cfg is None:
        with CONFIG_LOCK:
            cfg = dict(CONFIG)
    cand = (cfg.get("cds_lib") or "").strip()
    if cand:
        cand = os.path.abspath(os.path.expanduser(os.path.expandvars(cand)))
        if os.path.isfile(cand):
            return cand
    # walk up from the launch dir looking for a cds.lib
    d = os.getcwd()
    while True:
        fp = os.path.join(d, "cds.lib")
        if os.path.isfile(fp):
            return fp
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for fp in (os.environ.get("CDS_LIB", ""),
               os.path.join(os.path.expanduser("~"), "cds.lib")):
        fp = os.path.abspath(os.path.expanduser(os.path.expandvars(fp))) if fp else ""
        if fp and os.path.isfile(fp):
            return fp
    return ""


def parse_cds_lib(cds_path, _seen=None):
    """Return {libname: abspath} from a cds.lib, following INCLUDEs one level deep."""
    libs = {}
    if _seen is None:
        _seen = set()
    cds_path = os.path.abspath(os.path.expanduser(cds_path))
    if cds_path in _seen or not os.path.isfile(cds_path):
        return libs
    _seen.add(cds_path)
    base = os.path.dirname(cds_path)
    try:
        with open(cds_path, "r", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # strip trailing inline comments
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                key = parts[0].upper()
                if key == "DEFINE" and len(parts) >= 3:
                    name = parts[1]
                    p = os.path.expanduser(parts[2])
                    p = os.path.expandvars(p)
                    if not os.path.isabs(p):
                        p = os.path.join(base, p)
                    libs.setdefault(name, os.path.normpath(p))
                elif key in ("INCLUDE", "SOFTINCLUDE") and len(parts) >= 2:
                    # SOFTINCLUDE is like INCLUDE but silently ignores a missing
                    # file -- parse_cds_lib already no-ops on a nonexistent path.
                    inc = os.path.expanduser(parts[1])
                    inc = os.path.expandvars(inc)
                    if not os.path.isabs(inc):
                        inc = os.path.join(base, inc)
                    for n, pth in parse_cds_lib(inc, _seen).items():
                        libs.setdefault(n, pth)
    except Exception as e:
        sys.stderr.write("WARN parse cds.lib %s: %s\n" % (cds_path, e))
    return libs


def _is_oa_cell_dir(path):
    """A cell dir contains view subdirs; ignore OA housekeeping files/dirs."""
    if not os.path.isdir(path):
        return False
    name = os.path.basename(path)
    if name.startswith(".") or name in ("data.dm",):
        return False
    return True


def list_cells(lib_path):
    cells = []
    try:
        for name in sorted(os.listdir(lib_path)):
            p = os.path.join(lib_path, name)
            if _is_oa_cell_dir(p):
                # must contain at least one view (subdir)
                try:
                    if any(os.path.isdir(os.path.join(p, v)) and not v.startswith(".")
                           for v in os.listdir(p)):
                        cells.append(name)
                except OSError:
                    pass
    except OSError as e:
        raise RuntimeError("cannot list cells in %s: %s" % (lib_path, e))
    return cells


def list_views(lib_path, cell):
    views = []
    cell_dir = os.path.join(lib_path, cell)
    try:
        for name in sorted(os.listdir(cell_dir)):
            p = os.path.join(cell_dir, name)
            if os.path.isdir(p) and not name.startswith(".") and name != "data.dm":
                views.append(name)
    except OSError as e:
        raise RuntimeError("cannot list views in %s: %s" % (cell_dir, e))
    return views


# --------------------------------------------------------------------------- #
#  Result parsers  (matched to Calibre v2020.4 output)
# --------------------------------------------------------------------------- #

_RULECHECK_RE = re.compile(
    r"^RULECHECK\s+(\S+)\s+\.+\s+TOTAL Result Count\s*=\s*(\d+)\s*\((\d+)\)")


def parse_drc_summary(text):
    """Parse a .drc.summary -> dict with per-rule counts and totals."""
    rules = {}  # rule -> {"count": N, "orig": M}
    cell = None
    version = None
    date = None
    for line in text.splitlines():
        m = _RULECHECK_RE.match(line.strip())
        if m:
            rules[m.group(1)] = {"count": int(m.group(2)), "orig": int(m.group(3))}
            continue
        s = line.strip()
        if s.startswith("Layout Primary Cell:"):
            cell = s.split(":", 1)[1].strip()
        elif s.startswith("Calibre Version:"):
            version = s.split(":", 1)[1].strip()
        elif s.startswith("Execution Date/Time:"):
            date = s.split(":", 1)[1].strip()
    violations = {r: d for r, d in rules.items() if d["count"] > 0}
    return {
        "type": "drc",
        "cell": cell,
        "version": version,
        "date": date,
        "total_rules": len(rules),
        "violated_rules": len(violations),
        "total_violations": sum(d["count"] for d in violations.values()),
        "status": "CLEAN" if not violations else "VIOLATIONS",
        "rules": rules,
        "violations": violations,
    }


def parse_lvs_report(text):
    """Parse a .lvs.report -> overall CORRECT/INCORRECT + unmatched counts."""
    status = "UNKNOWN"
    # The big ASCII box says CORRECT or INCORRECT on the smiley/frowny banner.
    if re.search(r"#\s+CORRECT\s+#", text):
        status = "CORRECT"
    elif re.search(r"#\s+INCORRECT\s+#", text):
        status = "INCORRECT"
    else:
        # fall back to cell-summary line
        m = re.search(r"^\s*(CORRECT|INCORRECT)\s+\S+\s+\S+", text, re.M)
        if m:
            status = m.group(1)

    cell = None
    version = None
    m = re.search(r"^LAYOUT NAME:\s+.*\('([^']+)'\)", text, re.M)
    if m:
        cell = m.group(1)
    m = re.search(r"^CALIBRE VERSION:\s+(.+)$", text, re.M)
    if m:
        version = m.group(1).strip()
    date = None
    m = re.search(r"^CREATION TIME:\s+(.+)$", text, re.M)
    if m:
        date = m.group(1).strip()

    # Unmatched tallies:  "Total Inst:  646  646  0  0"
    unmatched = {}
    for label, key in (("Inst", "inst"), ("Nets", "nets"), ("Ports", "ports")):
        m = re.search(r"Total %s:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)" % label, text)
        if m:
            unmatched[key] = {
                "layout_matched": int(m.group(1)),
                "source_matched": int(m.group(2)),
                "layout_unmatched": int(m.group(3)),
                "source_unmatched": int(m.group(4)),
            }
    total_unmatched = sum(u["layout_unmatched"] + u["source_unmatched"]
                          for u in unmatched.values())
    return {
        "type": "lvs",
        "cell": cell,
        "version": version,
        "date": date,
        "status": status,
        "unmatched": unmatched,
        "total_unmatched": total_unmatched,
    }


def extract_design_from_file(path):
    """Best-effort pull of {tool, lib, cell, view} from a Calibre log / summary /
    report / runset, so the GUI can prefill. Missing fields come back as ''."""
    out = {"tool": "", "lib": "", "cell": "", "view": "", "source": path, "notes": []}
    if not os.path.isfile(path):
        out["notes"].append("no such file: %s" % path)
        return out
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except Exception as e:
        out["notes"].append(str(e))
        return out

    def first(*patterns):
        for pat in patterns:
            m = re.search(pat, text, re.M)
            if m:
                return m.group(1).strip().strip('"')
        return ""

    # tool
    low = path.lower()
    if "drc" in low or "CALIBRE::DRC" in text or "drcLayoutPrimary" in text:
        out["tool"] = "drc"
    if "lvs" in low or "L V S" in text or "lvsLayoutPrimary" in text or "LVS REPORT" in text:
        out["tool"] = "lvs"

    # cell / primary
    out["cell"] = first(
        r"^\*(?:drc|lvs)LayoutPrimary:\s*(\S+)",      # runset
        r"^LAYOUT PRIMARY\s+\"?([^\"\n]+)\"?",         # svrf runfile
        r"^Layout Primary Cell:\s*(\S+)",              # drc.summary
        r"^LAYOUT NAME:.*\('([^']+)'\)",               # lvs.report
        r"^\s*topCell\s+(\S+)",                        # strmout log
        r"\A(\S+)\s+\d+\s*$",                          # drc.results line 1: "<cell> 1000"
    )
    # view
    out["view"] = first(
        r"^\*(?:drc|lvs|cmnFDI)LayoutView:\s*(\S+)",
        r"^\s*view\s+(\S+)",                           # strmout log
    ) or ""
    # library (often absent from calibre reports -> may stay blank)
    out["lib"] = first(
        r"^\*(?:drc|lvs|cmnFDI)LayoutLibrary:\s*(\S+)",
        r"^\s*library\s+(\S+)",                        # strmout log
    ) or ""

    # If this is an SVRF rule file (a Calibre-Interactive _calibre.lvs_ / _calibre.drc_
    # meta deck, a *.lvs.rule runset, etc.), recover the SOURCE netlist, the INCLUDEd
    # deck and the LAYOUT path so a paste-to-prefill reproduces the full rerun setup
    # (matching the "Existing runsets" panel). These fields are absent from plain
    # result logs, so they only appear when the input really is a rule file.
    rf = _parse_rule_file(path)
    if rf:
        if not out["tool"]:
            out["tool"] = rf["tool"]
        if not out["cell"] and rf.get("cell"):
            out["cell"] = rf["cell"]
        if rf.get("source_path"):
            out["source_path"] = rf["source_path"]
        if rf.get("deck"):
            out["deck"] = rf["deck"]
            out["deck_exists"] = os.path.isfile(rf["deck"])
        if rf.get("layout_path"):
            # layout path in a rule file is usually relative to the rule file's dir
            lp = rf["layout_path"]
            if not os.path.isabs(lp):
                lp = os.path.join(os.path.dirname(os.path.abspath(path)), lp)
            out["layout_path"] = lp
            out["layout_exists"] = os.path.isfile(lp)
        out["rulefile"] = os.path.abspath(path)   # the known-good runset itself
        out["notes"].append("SVRF rule file: recovered source/deck for rerun")

    # If lib unknown but we have a cell, look it up by scanning cds.lib libraries.
    if out["cell"] and not out["lib"]:
        matches = []
        try:
            libs = parse_cds_lib(resolve_cds_lib())
            for name, p in libs.items():
                if os.path.isdir(os.path.join(p, out["cell"])):
                    matches.append(name)
        except Exception:
            pass
        if len(matches) == 1:
            out["lib"] = matches[0]
            out["notes"].append("lib inferred from cds.lib")
        elif len(matches) > 1:
            out["lib"] = matches[0]
            out["lib_candidates"] = matches
            out["notes"].append("cell found in %d libs: %s" % (len(matches), ", ".join(matches)))
        else:
            out["notes"].append("lib not in file and cell not found in any cds.lib library")
    return out


def detect_and_parse(path):
    """Read a result file and parse by type; returns dict (raw text included)."""
    if not os.path.isfile(path):
        return {"type": "error", "error": "no such file: %s" % path}
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return {"type": "error", "error": str(e)}
    low = path.lower()
    if low.endswith(".drc.summary") or ("CALIBRE::DRC" in text and "RULECHECK" in text):
        res = parse_drc_summary(text)
    elif low.endswith(".lvs.report") or "L V S   R E P O R T" in text or "LVS REPORT" in text:
        res = parse_lvs_report(text)
    else:
        res = {"type": "raw"}
    res["path"] = os.path.abspath(path)
    res["text"] = text
    return res


# --------------------------------------------------------------------------- #
#  Job management (background subprocess pipelines)
# --------------------------------------------------------------------------- #

class Job(object):
    def __init__(self, job_id, meta):
        self.id = job_id
        self.meta = meta            # tool, lib, cell, view, run_dir, ...
        self.state = "queued"       # queued|running|done|failed
        self.steps = []             # list of {name, cmd, rc, state}
        self.log_path = os.path.join(meta["run_dir"], "run.log")
        self.result = None
        self.error = None
        self.started = time.time()
        self.finished = None
        self.proc = None                 # currently-running subprocess (for Stop)
        self.stop_requested = False
        self._lock = threading.Lock()

    def stop(self):
        """Request stop: kill the running subprocess if any."""
        self.stop_requested = True
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
        return p is not None

    def snapshot(self, log_tail_bytes=200000):
        with self._lock:
            data = {
                "id": self.id,
                "state": self.state,
                "steps": list(self.steps),
                "meta": self.meta,
                "error": self.error,
                "result": self.result,
                "started": self.started,
                "finished": self.finished,
            }
        # read log tail
        log = ""
        try:
            if os.path.isfile(self.log_path):
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - log_tail_bytes))
                    log = f.read()
        except Exception:
            pass
        data["log"] = log
        data["log_size"] = size
        elapsed = (self.finished or time.time()) - self.started
        data["elapsed"] = elapsed
        base = self.meta.get("baseline_bytes") or 0
        # progress estimate: current run.log size vs a comparable prior run's size
        if self.state == "done":
            data["progress"] = 100
        elif self.state == "failed":
            data["progress"] = None
        elif base > 0 and size > 0:
            data["progress"] = min(99, int(size * 100.0 / base))
            rate = size / elapsed if elapsed > 0 else 0
            data["eta"] = max(0, (base - size) / rate) if (rate > 0 and base > size) else 0
        else:
            data["progress"] = None    # indeterminate (no baseline)
        data["baseline_bytes"] = base
        return data


JOBS = {}
JOBS_LOCK = threading.Lock()
_JOB_COUNTER = [0]


def _log(job, msg):
    with open(job.log_path, "a", encoding="utf-8", errors="replace") as f:
        f.write(msg)


def _banner(tag, desc):
    """Big flushed phase separator to the launching terminal."""
    line = "=" * 72
    sys.stdout.write("\n%s\n=====  %s  %s\n%s\n" % (line, tag, desc, line))
    sys.stdout.flush()


def _err(msg):
    """Print an error EDA-style (-E-) to the launching terminal."""
    for ln in (str(msg).splitlines() or [""]):
        sys.stdout.write("-E- %s\n" % ln)
    sys.stdout.flush()


def _human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return "%.0f%s" % (n, u)
        n /= 1024.0
    return "%.1fTB" % n


def _artifact(label, path):
    """Print a step's output file with full path, timestamp and size."""
    try:
        st = os.stat(path)
        sys.stdout.write("   -> %-14s %s   (%s, %s)\n" % (
            label + ":", path, _human_size(st.st_size),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))))
    except OSError:
        sys.stdout.write("   -> %-14s %s   (NOT FOUND)\n" % (label + ":", path))
    sys.stdout.flush()


def _console_step(num, name, state, cwd=None, cmd=None, rc=None):
    """Print the exact command (copy/paste-able) and its result under a phase."""
    if state == "start":
        sys.stdout.write("\n------ command used for %s ------\n"
                         "  cd %s && %s\n" % (name, shlex.quote(cwd), cmd))
    else:
        sys.stdout.write("------ %s: %s (rc=%s) ------\n"
                         % (name, "OK" if rc == 0 else "FAILED", rc))
    sys.stdout.flush()


def _run_step(job, name, cmd_list, cwd):
    with job._lock:
        step = {"name": name, "cmd": " ".join(shlex.quote(c) for c in cmd_list),
                "rc": None, "state": "running"}
        job.steps.append(step)
        num = len(job.steps)
        step["num"] = num
    _log(job, "\n" + "=" * 78 + "\n")
    _log(job, "### STEP %d: %s\n### CMD : %s\n### CWD : %s\n" %
         (num, name, step["cmd"], cwd))
    _log(job, "=" * 78 + "\n")
    _console_step(num, name, "start", cwd, step["cmd"])
    try:
        # decode tool output as UTF-8 with replacement so a stray non-ASCII byte
        # (e.g. under a C/POSIX locale) can't crash the reader with a decode error
        proc = subprocess.Popen(cmd_list, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=1,
                                encoding="utf-8", errors="replace",
                                env=os.environ)  # inherit the launching shell's env
    except FileNotFoundError as e:
        _log(job, "!! executable not found: %s\n" % e)
        step["state"] = "failed"
        step["rc"] = 127
        _console_step(num, name, "end", rc=127)
        return 127
    job.proc = proc                       # expose for the Stop button
    # heartbeat: every 5s print the newest-changing file + size in the run dir
    stop_hb = threading.Event()

    def _heartbeat():
        while not stop_hb.wait(5.0):
            try:
                newest, nsize, nmt = None, 0, 0
                for e in os.scandir(cwd):
                    if e.is_file():
                        st = e.stat()
                        if st.st_mtime >= nmt:
                            nmt, nsize, newest = st.st_mtime, st.st_size, e.name
                if newest:
                    sys.stdout.write("       ...running (%s): latest %s = %s, %ds ago\n" %
                                     (name, newest, _human_size(nsize), int(time.time() - nmt)))
                    sys.stdout.flush()
            except Exception:
                pass
    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()
    tail = []                             # keep last ~50 output lines for on-fail dump
    try:
        with open(job.log_path, "a", encoding="utf-8", errors="replace") as lf:
            for line in iter(proc.stdout.readline, ""):
                lf.write(line)
                lf.flush()
                tail.append(line)
                if len(tail) > 50:
                    del tail[0]
            proc.stdout.close()
        rc = proc.wait()
    finally:
        stop_hb.set()
        job.proc = None
    step["rc"] = rc
    step["state"] = "done" if rc == 0 else "failed"
    _log(job, "\n### STEP %d %s finished rc=%d\n" % (num, name, rc))
    _console_step(num, name, "end", rc=rc)
    if rc != 0:                           # surface the tool's own error on the console
        sys.stdout.write("-E- ----- last output of '%s' (rc=%d) -----\n" % (name, rc))
        for ln in tail:
            sys.stdout.write("-E- %s" % (ln if ln.endswith("\n") else ln + "\n"))
        sys.stdout.write("-E- ----- full log: %s -----\n" % job.log_path)
        # also echo any strmout/calibre side log(s) in the run dir
        for extra in sorted(globmod.glob(os.path.join(cwd, "*.log"))):
            sys.stdout.write("-E- side log: %s\n" % extra)
        sys.stdout.flush()
    return rc


def list_decks(kind):
    """Discover available deck revisions for kind in {'drc','lvs'}, newest first.
    Excludes *.orig / backup files."""
    with CONFIG_LOCK:
        cfg = dict(CONFIG)
    pat = cfg.get(kind + "_deck_glob", "")
    pat = os.path.expanduser(os.path.expandvars(pat))
    decks = []
    for p in globmod.glob(pat):
        if p.endswith((".orig", "~", ".bak")) or not os.path.isfile(p):
            continue
        try:
            stt = os.stat(p)
        except OSError:
            continue
        decks.append({"path": p, "name": os.path.basename(p),
                      "mtime": stt.st_mtime, "size": stt.st_size})
    decks.sort(key=lambda d: d["mtime"], reverse=True)   # latest revision first
    return {"kind": kind, "glob": pat, "decks": decks,
            "latest": decks[0]["path"] if decks else ""}


def latest_deck(kind):
    try:
        return list_decks(kind)["latest"]
    except Exception:
        return ""


def discover_src_net(cell, cfg, run_dir=None):
    """Find an existing LVS source netlist for `cell` on this host: <cell>.src.net
    (or .sp / .cdl / .spi) in the run dir, the cds.lib library dirs, discovered
    run dirs, and the log-search roots. Returns newest match, or ''."""
    # Prefer canonical SCHEMATIC source netlists (.src.net auCdl, .cdl) over a
    # bare .sp -- a <cell>.sp is often the LAYOUT-extracted spice from a prior LVS
    # run, which as the "source" would be a false CORRECT (layout vs layout).
    ext_priority = (".src.net", ".cdl", ".spi", ".net", ".sp")
    dirs = []
    if run_dir:
        dirs.append(run_dir)
    try:
        dirs += list(parse_cds_lib(resolve_cds_lib(cfg)).values())
    except Exception:
        pass
    try:
        dirs += discover_run_dirs(cfg)
    except Exception:
        pass
    for line in re.split(r"[\n,]+", cfg.get("sim_roots", "")):
        line = line.strip().replace("{user}", getpass.getuser())
        if line:
            dirs.append(os.path.expanduser(os.path.expandvars(line)))
    uniq = []
    seen = set()
    for d in dirs:
        d = os.path.abspath(d)
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            uniq.append(d)
    for ext in ext_priority:                 # first extension with any hit wins
        best, best_mt = "", -1
        for d in uniq:
            p = os.path.join(d, cell + ext)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if os.path.isfile(p) and mt > best_mt:
                best, best_mt = p, mt
        if best:
            return best
    return ""


LOG_PATTERNS = ["*.drc.summary", "*.drc.results", "*.lvs.report", "*.lvs.report.ext",
                "*.drc.rule", "*.lvs.rule", "*Runset*", "*runset*",
                "*drc*.log", "*lvs*.log", "*DRC*.log", "*LVS*.log",
                "strmOut*.log", "strmout*.log", "*.erc.summary"]


def _guess_log_type(name):
    n = name.lower()
    if "lvs" in n or n.endswith(".lvs.report") or n.endswith(".lvs.report.ext"):
        return "lvs"
    if "drc" in n or n.endswith(".drc.summary") or n.endswith(".drc.results"):
        return "drc"
    if "runset" in n:
        return "runset"
    if "strmout" in n or "strmout" == n[:7]:
        return "strmout"
    return "log"


_RF_LAYOUT_PATH = re.compile(r'^LAYOUT\s+PATH\s+"?([^"\n]+?)"?\s*$', re.M | re.I)
_RF_LAYOUT_PRIM = re.compile(r'^LAYOUT\s+PRIMARY\s+"?([^"\n]+?)"?\s*$', re.M | re.I)
_RF_SOURCE_PATH = re.compile(r'^SOURCE\s+PATH\s+"?([^"\n]+?)"?\s*$', re.M | re.I)
_RF_SOURCE_PRIM = re.compile(r'^SOURCE\s+PRIMARY\s+"?([^"\n]+?)"?\s*$', re.M | re.I)
_RF_INCLUDE = re.compile(r'^INCLUDE\s+"?([^"\n]+?)"?\s*$', re.M | re.I)
_RF_DRC_DB = re.compile(r'^DRC\s+RESULTS\s+DATABASE\s+"?([^"\n]+?)"?', re.M | re.I)
_RF_LVS_REP = re.compile(r'^LVS\s+REPORT\s+"?([^"\n]+?)"?', re.M | re.I)

RULE_FILE_PATTERNS = ["_*.drc_", "_*.lvs_", "_calibre.drc_", "_calibre.lvs_",
                      "*.drc.rule", "*.lvs.rule", "_*_"]


def _parse_rule_file(path):
    """Parse a Calibre SVRF rule file (runset / _calibre.lvs_ / *.drc.rule) into
    its DRC/LVS settings. Returns None if it doesn't look like an SVRF file."""
    try:
        with open(path, "r", errors="replace") as f:
            txt = f.read(200000)
    except Exception:
        return None
    prim = _RF_LAYOUT_PRIM.search(txt)
    inc = _RF_INCLUDE.search(txt)
    if not (prim or inc):
        return None                                   # not an SVRF rule file
    g = lambda r: (r.search(txt).group(1).strip() if r.search(txt) else "")
    is_lvs = bool(_RF_SOURCE_PRIM.search(txt) or _RF_SOURCE_PATH.search(txt)) \
        or ".lvs" in os.path.basename(path).lower()
    return {"file": path, "tool": "lvs" if is_lvs else "drc",
            "cell": g(_RF_LAYOUT_PRIM), "layout_path": g(_RF_LAYOUT_PATH),
            "source_path": g(_RF_SOURCE_PATH), "source_primary": g(_RF_SOURCE_PRIM),
            "deck": g(_RF_INCLUDE), "result": g(_RF_DRC_DB) or g(_RF_LVS_REP)}


def scan_rule_files(max_seconds=6.0, max_depth=3, limit=80):
    """Find existing Calibre rule files (runsets) under the launch dir, the run
    registry, and discovered run dirs -- so a prior DRC/LVS setup (layout, cell,
    source, deck) can be reused directly."""
    with CONFIG_LOCK:
        cfg = dict(CONFIG)
    roots = [os.getcwd()]
    if RUNS_BASE:
        roots.append(RUNS_BASE)
    try:
        roots += discover_run_dirs(cfg)
    except Exception:
        pass
    seen, uroots = set(), []
    for r in roots:
        ap = os.path.abspath(r)
        if ap not in seen and os.path.isdir(ap):
            seen.add(ap)
            uroots.append(ap)
    deadline = time.time() + max_seconds
    found, seen_files, timed_out = [], set(), False
    for root in uroots:
        base = root.rstrip("/").count("/")
        for dirpath, dirs, files in os.walk(root):
            if time.time() > deadline:
                timed_out = True
                break
            if dirpath.rstrip("/").count("/") - base >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("svdb", "__pycache__", ".git")]
            for fn in files:
                low = fn.lower()
                if not any(fnmatch.fnmatch(fn, p) for p in RULE_FILE_PATTERNS):
                    continue
                if fn.startswith("__") or "rcx" in low or "pex" in low or "erc" in low:
                    continue                          # skip extraction/meta, not DRC/LVS
                fp = os.path.join(dirpath, fn)
                if fp in seen_files:
                    continue
                seen_files.add(fp)
                d = _parse_rule_file(fp)
                if not d or not d["cell"]:            # need a real LAYOUT PRIMARY
                    continue
                if d["deck"] and d["deck"].lower().endswith((".rcx", ".pex")):
                    continue                          # extraction deck, not DRC/LVS
                rd = os.path.dirname(fp)
                for k in ("layout_path", "source_path"):
                    v = d.get(k)
                    if v and not os.path.isabs(v):
                        d[k] = os.path.normpath(os.path.join(rd, v))
                try:
                    d["mtime"] = os.path.getmtime(fp)
                except OSError:
                    d["mtime"] = 0
                d["layout_exists"] = bool(d["layout_path"]) and os.path.isfile(d["layout_path"])
                d["deck_exists"] = bool(d["deck"]) and os.path.isfile(d["deck"])
                found.append(d)
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        if timed_out or len(found) >= limit:
            break
    found.sort(key=lambda x: x["mtime"], reverse=True)
    dedup, seenkey = [], set()
    for d in found:                                   # keep newest per tool+cell+deck
        k = (d["tool"], d["cell"], d["deck"])
        if k in seenkey:
            continue
        seenkey.add(k)
        dedup.append(d)
    return {"count": len(dedup), "timed_out": timed_out, "rulefiles": dedup[:limit]}


_RULEFILE_PATH_RE = re.compile(
    r"^(?:Rule File Pathname|RULE FILE)\s*:\s*(\S+)", re.M | re.I)


def discover_decks(kind):
    """Find rule-deck paths this host actually has, by reading the INCLUDE line of
    existing runsets AND the 'Rule File Pathname' of existing DRC/LVS result logs.
    Returns readable deck paths, most-recently-used first. Lets a run proceed even
    when drc_deck/drc_deck_glob aren't configured on this host."""
    decks, seen = [], set()

    def add(path, mtime, via, depth=0):
        if not path or depth > 3:
            return
        path = os.path.expanduser(os.path.expandvars(path))
        if not (os.path.isfile(path) and os.access(path, os.R_OK)):
            return
        base = os.path.basename(path).lower()
        if base.endswith((".rcx", ".pex")) or "rcx" in base or "pex" in base:
            return                                 # extraction deck, not DRC/LVS
        if base.startswith("_") and base.endswith("_"):
            # Calibre-Interactive wrapper: resolve to the real deck it INCLUDEs
            rf = _parse_rule_file(path)
            if rf and rf.get("deck"):
                add(rf["deck"], mtime, via + "->INCLUDE", depth + 1)
            return
        if path in seen:
            return
        seen.add(path)
        decks.append({"deck": path, "mtime": mtime, "via": via})

    # 1) INCLUDE paths from existing runsets (the real PDK deck)
    try:
        for d in scan_rule_files().get("rulefiles", []):
            if d.get("tool") == kind and d.get("deck"):
                add(d["deck"], d.get("mtime", 0), "runset:%s" % d.get("cell", "?"))
    except Exception as e:
        _dbg("discover_decks scan_rule_files: %s" % e)

    # 2) 'Rule File Pathname'/'RULE FILE' from existing result logs
    try:
        pats = ["*.drc.summary"] if kind == "drc" else ["*.lvs.report"]
        for r in (search_logs(max_seconds=6.0).get("results", [])):
            if any(fnmatch.fnmatch(r["name"], p) for p in pats):
                try:
                    with open(r["path"], "r", errors="replace") as f:
                        head = f.read(4000)
                except OSError:
                    continue
                m = _RULEFILE_PATH_RE.search(head)
                if m:
                    add(m.group(1), r.get("mtime", 0), "log:%s" % r["name"])
    except Exception as e:
        _dbg("discover_decks logs: %s" % e)

    decks.sort(key=lambda d: d["mtime"], reverse=True)
    return decks


def recent_layouts(cfg=None, limit=12, max_seconds=6.0, views=("layout",)):
    """Scan the WRITABLE cds.lib libraries for the most recently modified layout
    cellviews (i.e. the design you were just editing). Read-only PDK/other-user
    libraries are skipped for speed and relevance. Bounded by max_seconds."""
    if cfg is None:
        with CONFIG_LOCK:
            cfg = dict(CONFIG)
    libs = parse_cds_lib(resolve_cds_lib(cfg))
    deadline = time.time() + max_seconds
    items, timed_out = [], False
    for lib, libpath in sorted(libs.items()):
        if time.time() > deadline:
            timed_out = True
            break
        if not os.path.isdir(libpath):
            continue
        # skip obvious read-only PDK/system libs for speed; include everything
        # else (incl. SOFTINCLUDE'd design libs that may not be W_OK to you)
        low = libpath.lower()
        if (libpath.startswith(("/usr/", "/opt/", "/cad/", "/tools/", "/pkg/"))
                or "/pdk" in low or "cdslib" in low):
            continue
        try:
            cells = os.listdir(libpath)
        except OSError:
            continue
        for cell in cells:
            if cell.startswith(".") or cell == "data.dm":
                continue
            cdir = os.path.join(libpath, cell)
            if not os.path.isdir(cdir):
                continue
            for view in views:
                vdir = os.path.join(cdir, view)
                if not os.path.isdir(vdir):
                    continue
                try:                                  # 1 stat typical (layout.oa)
                    mt = os.path.getmtime(os.path.join(vdir, "layout.oa"))
                except OSError:
                    try:
                        mt = os.path.getmtime(vdir)
                    except OSError:
                        mt = None
                if mt is not None:
                    items.append({"lib": lib, "cell": cell, "view": view,
                                  "mtime": mt, "path": vdir})
            if time.time() > deadline:
                timed_out = True
                break
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"count": len(items), "recent": items[:limit], "timed_out": timed_out}


_RUNDIR_RE = re.compile(r"^\*(?:drc|lvs|pex|cmn)\w*RunDir:\s*(\S+)", re.M)


def _parse_rundirs(path):
    """Return (rundirs, referenced_runset_files) parsed from a runset / cgi db."""
    try:
        with open(path, "r", errors="replace") as f:
            txt = f.read()
    except Exception:
        return [], []
    dirs = _RUNDIR_RE.findall(txt)
    refs = []
    for line in txt.splitlines():
        line = line.strip()
        # ~/.cgidrcdb lists runset file paths (one per line); follow them
        if line.startswith("/") and " " not in line and os.path.isfile(line):
            refs.append(line)
    return dirs, refs


def discover_run_dirs(cfg):
    """Auto-discover Calibre run directories from Interactive state files
    (~/.cgidrcdb, ~/.cgilvsdb, and *Runset*/*runset* files in the home dir,
    the cds.lib library dirs, and the CWD) by reading their *RunDir keys."""
    home = os.path.expanduser("~")
    scan_dirs = {home, os.getcwd()}
    try:
        for p in parse_cds_lib(resolve_cds_lib(cfg)).values():
            scan_dirs.add(p)
    except Exception:
        pass
    files = []
    for d in scan_dirs:
        for name in (".cgidrcdb", ".cgilvsdb"):
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                files.append(fp)
        try:
            for fn in os.listdir(d):
                if "runset" in fn.lower():
                    files.append(os.path.join(d, fn))
        except OSError:
            pass

    found, seen = set(), set()
    queue = list(dict.fromkeys(files))
    i, cap = 0, 500                       # bounded so a weird file can't loop forever
    while i < len(queue) and i < cap:
        fp = queue[i]
        i += 1
        if fp in seen or not os.path.isfile(fp):
            continue
        seen.add(fp)
        dirs, refs = _parse_rundirs(fp)
        for dpath in dirs:
            dpath = os.path.expanduser(os.path.expandvars(dpath))
            if os.path.isdir(dpath):
                found.add(os.path.abspath(dpath))
        for r in refs:
            if r not in seen:
                queue.append(r)
    return sorted(found)


def search_logs(user=None, extra=None, max_results=800, max_depth=4, max_seconds=12.0):
    """Search /sim/<user> and other simulation roots for Calibre logs.

    Bounded by wall-clock (max_seconds) so a huge or slow (NFS) root can never
    hang the UI; progress is written to the debug log per root."""
    with CONFIG_LOCK:
        cfg = dict(CONFIG)
    user = (user or cfg.get("sim_user") or getpass.getuser()).strip()
    roots = []
    for line in re.split(r"[\n,]+", cfg.get("sim_roots", "")):
        line = line.strip()
        if not line:
            continue
        line = line.replace("{user}", user)
        roots.append(os.path.expanduser(os.path.expandvars(line)))
    if extra:
        for e in re.split(r"[\n,]+", extra):
            e = e.strip()
            if e:
                roots.append(os.path.expanduser(os.path.expandvars(e.replace("{user}", user))))
    # auto-discovered Calibre run directories (searched first -- most likely hits)
    discovered = set()
    if str(cfg.get("discover_run_dirs", "yes")).strip().lower() in ("1", "yes", "true", "on"):
        try:
            discovered = set(discover_run_dirs(cfg))
        except Exception as e:
            _dbg("discover_run_dirs error: %s" % e)
        if discovered:
            _dbg("discover_run_dirs: %s" % sorted(discovered))
            roots = sorted(discovered) + roots
    # de-dupe, keep order
    seen, uroots = set(), []
    for r in roots:
        ap = os.path.abspath(r)
        if ap not in seen:
            seen.add(ap)
            uroots.append(ap)

    deadline = time.time() + max_seconds
    _dbg("searchlogs: user=%s roots=%s budget=%.1fs" % (user, uroots, max_seconds))
    sys.stdout.write("\n" + "=" * 72 +
                     "\n======== LOG SEARCH:  scanning %d root(s) for Calibre logs "
                     "(budget %ds)\n" % (len(uroots), int(max_seconds)) + "=" * 72 + "\n")
    sys.stdout.flush()
    results, scanned, timed_out = [], [], False
    for root in uroots:
        exists = os.path.isdir(root)
        rec = {"root": root, "exists": exists, "hits": 0, "seconds": 0.0,
               "timed_out": False, "discovered": root in discovered}
        scanned.append(rec)
        if not exists:
            _dbg("searchlogs:   skip (absent) %s" % root)
            sys.stdout.write("   (absent)  %s\n" % root)
            sys.stdout.flush()
            continue
        sys.stdout.write("   scanning  %s ...\n" % root)
        sys.stdout.flush()
        t0 = time.time()
        # Walk each root in a worker thread with a per-root time cap, so a single
        # stalled/NFS root (where os.walk blocks in a syscall) is ABANDONED and we
        # move on to the next root instead of hanging the whole search.
        per_root = max(1.0, min(6.0, deadline - time.time()))
        abandon = threading.Event()
        rlock = threading.Lock()

        def _walk(root=root, rec=rec):
            base = root.rstrip("/").count("/")
            try:
                for dirpath, dirs, files in os.walk(root, onerror=lambda e: None):
                    if abandon.is_set():
                        return
                    if dirpath.rstrip("/").count("/") - base >= max_depth:
                        dirs[:] = []
                        continue
                    dirs[:] = [d for d in dirs if not d.startswith(".")
                               and d not in ("svdb", "__pycache__", ".git")]
                    for fn in files:
                        if any(fnmatch.fnmatch(fn, pp) for pp in LOG_PATTERNS):
                            p = os.path.join(dirpath, fn)
                            try:
                                stt = os.stat(p)
                            except OSError:
                                continue
                            if not stat.S_ISREG(stt.st_mode):
                                continue
                            with rlock:
                                results.append({"path": p, "name": fn, "dir": dirpath,
                                                "size": stt.st_size, "mtime": stt.st_mtime,
                                                "type": _guess_log_type(fn)})
                                rec["hits"] += 1
                                if len(results) >= max_results:
                                    return
            except Exception:
                pass

        th = threading.Thread(target=_walk, daemon=True)
        th.start()
        th.join(per_root)
        if th.is_alive():                 # root too slow -> abandon it, keep going
            abandon.set()
            rec["timed_out"] = True
            timed_out = True
            _dbg("searchlogs:   ABANDONED slow root %s after %.1fs" % (root, per_root))
        rec["seconds"] = round(time.time() - t0, 2)
        _dbg("searchlogs:   %s -> %d hits in %.2fs%s" %
             (root, rec["hits"], rec["seconds"], " (ABANDONED)" if rec["timed_out"] else ""))
        sys.stdout.write("      -> %d logs in %.1fs%s\n" %
                         (rec["hits"], rec["seconds"],
                          "   *** SLOW ROOT ABANDONED ***" if rec["timed_out"] else ""))
        sys.stdout.flush()
        if len(results) >= max_results or time.time() > deadline:
            break
    results.sort(key=lambda r: r["mtime"], reverse=True)
    _dbg("searchlogs: done -> %d logs, timed_out=%s" % (len(results), timed_out))
    sys.stdout.write("======== LOG SEARCH DONE:  %d logs%s ========\n" %
                     (len(results), "   (TIMED OUT on a slow root -- narrow sim_roots in Config)"
                      if timed_out else ""))
    sys.stdout.flush()
    return {"user": user, "login": getpass.getuser(), "roots": scanned,
            "count": len(results), "truncated": len(results) >= max_results,
            "timed_out": timed_out, "results": results}


def _require_deck(deck, kind):
    """Fail early (before Calibre's INCL1) if the rule deck the runset will
    INCLUDE isn't a readable file on THIS host."""
    if not deck:
        raise RuntimeError(
            "No %s rule deck configured. Set %s_deck / %s_deck_glob in the Config "
            "tab." % (kind, kind.lower(), kind.lower()))
    if not os.path.exists(deck):
        raise RuntimeError(
            "%s rule deck not found on this host:\n  %s\n"
            "This path may not be mounted here (you are on a different host). "
            "Set the correct deck in Config -> %s_deck / %s_deck_glob."
            % (kind, deck, kind.lower(), kind.lower()))
    if not os.access(deck, os.R_OK):
        raise RuntimeError(
            "%s rule deck exists but is NOT readable by you:\n  %s\n"
            "You are probably not in the PDK unix group on this host "
            "(check: `ls -l <deck>` and `id -nG`)." % (kind, deck))


def _write_drc_runset(run_dir, cell, gds, deck, extra):
    runfile = os.path.join(run_dir, "%s.drc.rule" % cell)
    with open(runfile, "w") as f:
        f.write('// Auto-generated DRC runset -- %s\n' % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.write('LAYOUT PATH "%s"\n' % gds)
        f.write('LAYOUT PRIMARY "%s"\n' % cell)
        f.write('LAYOUT SYSTEM GDSII\n\n')
        f.write('DRC RESULTS DATABASE "%s.drc.results"\n' % cell)
        f.write('DRC SUMMARY REPORT "%s.drc.summary" REPLACE\n' % cell)
        f.write('DRC MAXIMUM RESULTS 1000\n')
        f.write('DRC ICSTATION YES\n\n')
        if extra.strip():
            f.write(extra.strip() + "\n\n")
        f.write('INCLUDE "%s"\n' % deck)
    return runfile


def _write_lvs_runset(run_dir, cell, gds, src_net, deck, extra):
    runfile = os.path.join(run_dir, "%s.lvs.rule" % cell)
    with open(runfile, "w") as f:
        f.write('// Auto-generated LVS runset -- %s\n' % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.write('LAYOUT PATH "%s"\n' % gds)
        f.write('LAYOUT PRIMARY "%s"\n' % cell)
        f.write('LAYOUT SYSTEM GDSII\n\n')
        f.write('SOURCE PATH "%s"\n' % src_net)
        f.write('SOURCE PRIMARY "%s"\n' % cell)
        f.write('SOURCE SYSTEM SPICE\n\n')
        f.write('MASK SVDB DIRECTORY "svdb" QUERY\n')
        f.write('LVS REPORT "%s.lvs.report"\n' % cell)
        f.write('LVS REPORT MAXIMUM 50\n')
        f.write('DRC ICSTATION YES\n\n')
        if extra.strip():
            f.write(extra.strip() + "\n\n")
        f.write('INCLUDE "%s"\n' % deck)
    return runfile


def _write_lvs_runset_verbatim(run_dir, cell, gds, src_net, src_runset):
    """Rerun a *known-good* runset (e.g. a Calibre-Interactive `_calibre.lvs_`)
    VERBATIM: copy it and rewrite ONLY the LAYOUT/SOURCE paths + the LVS REPORT
    file, preserving everything else -- crucially the `#!tvf` first line,
    `tvf::VERBATIM { ... }` blocks, the full `MASK SVDB DIRECTORY ... XRC CCI
    IXF NXF SLPH SI` spec, `LVS REPORT OPTION ...`, every `#DEFINE` and `INCLUDE`.
    A synthesized plain-SVRF runset drops those and breaks TVF decks (undefined
    layers). Returns (runfile, changes) where changes lists what was rewritten."""
    with open(src_runset, "r", errors="replace") as f:
        text = f.read()
    changes = []

    def sub(pat, repl_val, label):
        nonlocal text
        new, n = re.subn(pat, lambda m: m.group(1) + '"%s"' % repl_val, text,
                         flags=re.I | re.M)
        if n:
            text = new
            changes.append("%s -> %s (%dx)" % (label, repl_val, n))

    # LAYOUT/SOURCE point at THIS run's artifacts; LVS REPORT lands in the run dir.
    sub(r'^([ \t]*LAYOUT[ \t]+PATH[ \t]+).*$',    gds,                     "LAYOUT PATH")
    sub(r'^([ \t]*LAYOUT[ \t]+PRIMARY[ \t]+).*$', cell,                    "LAYOUT PRIMARY")
    sub(r'^([ \t]*SOURCE[ \t]+PATH[ \t]+).*$',    src_net,                 "SOURCE PATH")
    sub(r'^([ \t]*SOURCE[ \t]+PRIMARY[ \t]+).*$', cell,                    "SOURCE PRIMARY")
    # only the `LVS REPORT "<file>"` line (not `LVS REPORT OPTION/MAXIMUM ...`).
    sub(r'^([ \t]*LVS[ \t]+REPORT[ \t]+)"[^"\n]*"[ \t]*$',
        "%s.lvs.report" % cell, "LVS REPORT")

    runfile = os.path.join(run_dir, "%s.lvs.rule" % cell)
    with open(runfile, "w") as f:
        f.write(text)
    return runfile, changes


def _write_si_env(run_dir, lib, cell, view, out_name):
    """Write a si.env that drives Cadence's auCDL netlister to emit a CDL
    source netlist for lib/cell/view. Read by `si <run_dir> -batch -command
    netlist`. Values are SKILL (t/nil). The netlist lands in the run dir as
    `out_name` (and/or a bare `netlist` file, which the caller normalizes)."""
    envfile = os.path.join(run_dir, "si.env")
    with open(envfile, "w", encoding="utf-8") as f:
        f.write(";; Auto-generated si.env (auCDL) -- %s\n"
                % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.write('simLibName = "%s"\n' % lib)
        f.write('simCellName = "%s"\n' % cell)
        f.write('simViewName = "%s"\n' % view)
        f.write('simSimulator = "auCdl"\n')
        f.write("simNotIncremental = 1\n")
        f.write("simReNetlistAll = nil\n")
        f.write("simViewList = '(\"auCdl\" \"schematic\")\n")
        f.write("simStopList = '(\"auCdl\")\n")
        f.write("simNetlistHier = t\n")
        f.write('hnlNetlistFileName = "%s"\n' % out_name)
        f.write('simNetlistName = "%s"\n' % out_name)
        f.write('resistorModel = ""\n')
        f.write("shortRES = 2000.0\n")
        f.write("preserveRES = t\n")
        f.write("checkRESVAL = t\n")
        f.write("checkRESSIZE = nil\n")
        f.write("preserveCAP = t\n")
        f.write("checkCAPVAL = t\n")
        f.write("checkCAPAREA = nil\n")
        f.write("checkCAPPERI = nil\n")
        f.write("preserveDIODE = t\n")
        f.write("checkDIODEAREA = nil\n")
        f.write("checkDIODEPERI = nil\n")
        f.write("preserveBIPOLAR = t\n")
        f.write("preservePMOS = t\n")
        f.write("preserveNMOS = t\n")
        f.write("shrinkFACTOR = 0.0\n")
        f.write('globalPowerSig = ""\n')
        f.write('globalGroundSig = ""\n')
        f.write("displayPININFO = t\n")
        f.write("preserveALL = t\n")
        f.write('setEQUIV = ""\n')
        f.write('incFILE = ""\n')
    return envfile


def _write_netlist_skill(run_dir, lib, cell, view, out_name):
    """Write a SKILL script that netlists lib/cell/view to a CDL source netlist,
    to be run headless via `virtuoso -nograph -replay`. Uses the auCdl analog
    netlister (simulator/design/createNetlist) after opening the cellview with
    deOpenCellViewByType. Adjust the netlister call for your site if needed."""
    ilfile = os.path.join(run_dir, "netlist_%s.il" % cell)
    out_path = os.path.join(run_dir, out_name)
    with open(ilfile, "w", encoding="utf-8") as f:
        f.write(";; Auto-generated CDL netlister -- %s\n"
                % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.write("let((cv produced outfile infile2 line)\n")
        f.write('  cv = deOpenCellViewByType("%s" "%s" "%s" "" "r")\n'
                % (lib, cell, view))
        f.write('  unless(cv error("cannot open %s/%s/%s for netlisting\\n"))\n'
                % (lib, cell, view))
        f.write("  simulator('auCdl)\n")
        f.write('  design("%s" "%s" "%s")\n' % (lib, cell, view))
        f.write('  resultsDir("%s")\n' % run_dir)
        f.write("  createNetlist(?recreateAll t ?display nil)\n")
        # auCdl writes <resultsDir>/netlist -- normalize to the expected name.
        f.write('  produced = "%s/netlist"\n' % run_dir)
        f.write('  when(isFile(produced)\n')
        f.write('    outfile = outfile("%s")\n' % out_path)
        f.write('    infile2 = infile(produced)\n')
        f.write('    when(infile2 && outfile\n')
        f.write('      while(gets(line infile2) fprintf(outfile "%s" line))\n')
        f.write('      close(infile2) close(outfile)))\n')
        f.write(")\n")
        f.write("exit(0)\n")
    return ilfile


def _generate_src_net(job, cfg, run_dir, lib, cell, src_view):
    """Generate an LVS source netlist (schematic -> CDL) for lib/cell/src_view
    into run_dir; return the netlist path, or '' if generation is off/failed.
    Dispatch: a non-blank netlist_cmd wins, else netlist_mode (si|skill|off)."""
    out_name = "%s.src.net" % cell
    out_path = os.path.join(run_dir, out_name)

    # a local cds.lib in the run dir that INCLUDEs the real one (for `si -cdslib`
    # and for virtuoso's DEFINE search). Reuse the one strmout may have written.
    cdslib = os.path.join(run_dir, "cds.lib")
    if not os.path.isfile(cdslib):
        cds = resolve_cds_lib(cfg)
        if cds:
            with open(cdslib, "w", encoding="utf-8") as f:
                f.write('INCLUDE "%s"\n' % cds)

    mode = (cfg.get("netlist_mode") or "si").strip().lower()
    custom = (cfg.get("netlist_cmd") or "").strip()
    mapping = {
        "run_dir": run_dir, "cell": cell, "lib": lib,
        "view": src_view, "netlist_view": src_view,
        "src_net": out_name, "cdslib": cdslib,
        "si_bin": cfg.get("si_bin") or "si",
        "virtuoso_bin": cfg.get("virtuoso_bin") or "virtuoso",
        "calibre_bin": cfg.get("calibre_bin") or "calibre",
        "netlist_log": "netlist_%s.log" % cell,
        "skill_script": "",
    }

    if custom:
        cmd = _fill(custom, mapping)
        _run_step(job, "generate source netlist (custom)", cmd, run_dir)
    elif mode == "off":
        sys.stdout.write("   -I- netlist_mode=off -- not generating a source netlist\n")
        sys.stdout.flush()
        return ""
    elif mode == "si":
        if not shutil.which(cfg.get("si_bin") or "si"):
            _ensure_tools(job, cfg, ["si"])       # auto module-load if missing
        _artifact("si.env", _write_si_env(run_dir, lib, cell, src_view, out_name))
        cmd = _fill(cfg.get("si_cmd") or "", mapping)
        _run_step(job, "generate source netlist (si auCDL)", cmd, run_dir)
    elif mode == "skill":
        if not shutil.which(cfg.get("virtuoso_bin") or "virtuoso"):
            _ensure_tools(job, cfg, ["virtuoso"])
        ilfile = _write_netlist_skill(run_dir, lib, cell, src_view, out_name)
        mapping["skill_script"] = os.path.basename(ilfile)
        _artifact("skill script", ilfile)
        cmd = _fill(cfg.get("netlist_skill_cmd") or "", mapping)
        _run_step(job, "generate source netlist (virtuoso -nograph)", cmd, run_dir)
    else:
        raise RuntimeError("unknown netlist_mode %r (use si|skill|custom|off)" % mode)

    # locate the produced netlist -- auCdl may emit `out_name`, a bare `netlist`,
    # or <cell>.cdl; normalize whichever exists to <cell>.src.net.
    for cand in (out_path, os.path.join(run_dir, "netlist"),
                 os.path.join(run_dir, "%s.cdl" % cell),
                 os.path.join(run_dir, "%s.src" % cell)):
        if os.path.isfile(cand) and os.path.getsize(cand) > 0:
            if os.path.abspath(cand) != os.path.abspath(out_path):
                shutil.copyfile(cand, out_path)
                sys.stdout.write("   -I- netlist %s -> %s\n"
                                 % (os.path.basename(cand), out_name))
                sys.stdout.flush()
            return out_path
    return ""


def _fill(template, mapping):
    return shlex.split(template.format(**mapping))


def _drop_valueless_flags(tokens, flags):
    """Remove a flag (and only the flag) when its value is missing/blank -- i.e.
    the flag is last, or the next token is another flag. Keeps blank techlib/
    layermap from corrupting the strmout command (e.g. '-techLib -layerMap ...')."""
    out, i, n = [], 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t in flags:
            nxt = tokens[i + 1] if i + 1 < n else None
            if nxt is None or nxt.startswith("-"):
                i += 1
                continue                       # drop the value-less flag
        out.append(t)
        i += 1
    return out


_STRM_TECHLIB_RE = re.compile(r"^\s*techLib\s+(\S+)", re.M)
_STRM_LAYERMAP_RE = re.compile(r"^\s*layerMap\s+(\S+)", re.M)


def discover_strmout_opts():
    """Read techLib / layerMap from existing strmout logs (most-recent first), for
    hosts where they aren't configured. Only returns a layerMap that exists here."""
    techlib, layermap = "", ""
    try:
        results = search_logs(max_seconds=6.0).get("results", [])
    except Exception:
        results = []
    for r in results:                          # already sorted newest-first
        if not (fnmatch.fnmatch(r["name"], "strmOut*.log")
                or fnmatch.fnmatch(r["name"], "strmout*.log")):
            continue
        try:
            with open(r["path"], "r", encoding="utf-8", errors="replace") as f:
                txt = f.read(12000)
        except OSError:
            continue
        if not techlib:
            m = _STRM_TECHLIB_RE.search(txt)
            if m:
                techlib = m.group(1)
        if not layermap:
            m = _STRM_LAYERMAP_RE.search(txt)
            if m and os.path.isfile(m.group(1)):
                layermap = m.group(1)
        if techlib and layermap:
            break
    return {"techlib": techlib, "layermap": layermap}


# --------------------------------------------------------------------------- #
#  Environment / module auto-loading
# --------------------------------------------------------------------------- #

def _dbg(msg):
    """Append a timestamped line to the on-disk GUI debug log (file only, so the
    console stays clean for the phase banners)."""
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        if RUNS_BASE:
            with open(os.path.join(RUNS_BASE, "gui_debug.log"), "a", encoding="utf-8", errors="replace") as f:
                f.write(line)
    except Exception:
        pass


def _bin_for(cfg, tool):
    """tool in {'strmout','calibre'} -> configured executable name/path."""
    return cfg.get(tool + "_bin") or tool


def env_status():
    """Report whether strmout/calibre resolve on the current (inherited) PATH."""
    with CONFIG_LOCK:
        cfg = dict(CONFIG)
    out = {"path": os.environ.get("PATH", ""), "modules": cfg.get("modules", "")}
    ok = True
    for tool in ("strmout", "calibre"):
        b = _bin_for(cfg, tool)
        p = shutil.which(b)
        out[tool] = {"bin": b, "path": p, "found": bool(p)}
        if not p:
            ok = False
    out["ok"] = ok
    _dbg("envcheck: ok=%s strmout=%s calibre=%s" %
         (ok, out["strmout"]["path"], out["calibre"]["path"]))
    return out


def load_modules(modules_str):
    """Run `module load <modules>` in a login shell, capture the resulting
    environment, and merge it into os.environ so future subprocess calls
    (strmout/calibre) inherit it. Returns a status dict."""
    with CONFIG_LOCK:
        cfg = dict(CONFIG)
    modules_str = (modules_str or cfg.get("modules", "")).strip()
    if not modules_str:
        return {"ok": env_status()["ok"], "error": "no modules specified",
                "status": env_status()}
    tmpl = cfg.get("module_load_cmd") or 'bash -lc "module load {modules} 1>&2; env -0"'
    # NB: use replace (not .format) -- the template contains shell ${...} braces.
    cmd = tmpl.replace("{modules}", modules_str)
    _dbg("loadmodules: modules=%r cmd=%s" % (modules_str, cmd))
    try:
        proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=180)
    except Exception as e:
        _dbg("loadmodules EXCEPTION: %s" % e)
        return {"ok": False, "error": str(e), "cmd": cmd, "status": env_status()}
    _dbg("loadmodules: rc=%s stdout=%dB stderr=%s" %
         (proc.returncode, len(proc.stdout), proc.stderr.decode("utf-8", "replace").strip()[:500]))

    applied = 0
    raw = proc.stdout
    if b"=" in raw:
        newenv = {}
        # env -0 => NUL-delimited KEY=VALUE; fall back to newline split if needed
        parts = raw.split(b"\0") if b"\0" in raw else raw.split(b"\n")
        for chunk in parts:
            if not chunk or b"=" not in chunk:
                continue
            k, v = chunk.split(b"=", 1)
            try:
                newenv[k.decode()] = v.decode()
            except Exception:
                continue
        if "PATH" in newenv:                      # sanity: only trust a real env dump
            for k, v in newenv.items():
                if os.environ.get(k) != v:
                    applied += 1
            os.environ.update(newenv)             # merge -> inherited by children
    stderr = proc.stderr.decode("utf-8", "replace")
    st = env_status()
    _dbg("loadmodules result: ok=%s applied_vars=%s strmout=%s calibre=%s" %
         (st["ok"], applied, st["strmout"]["path"], st["calibre"]["path"]))
    return {"ok": st["ok"], "applied_vars": applied, "cmd": cmd,
            "stderr": stderr[-6000:], "status": st,
            "stdout_bytes": len(proc.stdout)}


def _ensure_tools(job, cfg, tools):
    """Make sure `tools` resolve on PATH; if not and auto-load is on, run
    `module load` for the user as a logged job step."""
    missing = [t for t in tools if not shutil.which(_bin_for(cfg, t))]
    if not missing:
        return
    auto = str(cfg.get("auto_load_modules", "yes")).strip().lower() in ("1", "yes", "true", "on")
    mods = cfg.get("modules", "").strip()
    if not auto or not mods:
        raise RuntimeError(
            "required tools not on PATH: %s. Launch the GUI from a shell where "
            "they are set up, enable auto module-load, or set absolute paths in "
            "Config." % ", ".join(missing))
    tmpl = cfg.get("module_load_cmd", "")
    with job._lock:
        step = {"name": "module load %s" % mods,
                "cmd": tmpl.replace("{modules}", mods),
                "rc": None, "state": "running"}
        job.steps.append(step)
        num = len(job.steps)
        step["num"] = num
    _log(job, "\n" + "=" * 78 + "\n### STEP %d: module load %s  (auto: tools missing: %s)\n"
         % (num, mods, ", ".join(missing)) + "=" * 78 + "\n")
    _console_step(num, "module load %s" % mods, "start", os.getcwd(), step["cmd"])
    res = load_modules(mods)
    _log(job, "module load applied %s env vars\n" % res.get("applied_vars"))
    if res.get("stderr"):
        _log(job, "module stderr:\n%s\n" % res["stderr"])
    still = [t for t in tools if not shutil.which(_bin_for(cfg, t))]
    step["state"] = "done" if not still else "failed"
    step["rc"] = 0 if not still else 1
    _console_step(num, "module load %s" % mods, "end", rc=step["rc"])
    if still:
        raise RuntimeError(
            "after 'module load %s', still not found: %s. Check the module name, "
            "or set absolute paths (strmout_bin/calibre_bin) in Config." %
            (mods, ", ".join(still)))
    _log(job, "### tools resolved: %s\n" %
         ", ".join("%s=%s" % (t, shutil.which(_bin_for(cfg, t))) for t in tools))


def _scan_calibre_errors(log_path, max_lines=800):
    """Scan the tail of a run log for salient Calibre errors. Returns a dict:
      errs    -> notable error lines
      layers  -> undefined layer names  ('undefined layer name parameter: X')
      subckts -> [(name, netlist_file)] for a source-netlist gap
                 ('No matching ".SUBCKT" statement for "X" ... in the file "F"')"""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except Exception:
        return {"errs": [], "layers": [], "subckts": []}
    errs, layers, subckts, seen = [], [], [], set()
    err_re = re.compile(r"undefined layer|unrecognized|not defined|isn'?t defined|"
                        r"no matching|problem with access|syntax error|\*error\*|"
                        r"^error|\berror\b.*line\s+\d+|cannot find|missing", re.I)
    lyr_re = re.compile(r"undefined layer name parameter:\s*([A-Za-z0-9_./]+)", re.I)
    sub_re = re.compile(r'no matching\s+"?\.?SUBCKT"?\s+statement\s+for\s+"?([^"\s]+)"?'
                        r'(?:.*?in the file\s+"?([^"\n]+?)"?\s*)?$', re.I)
    for ln in lines:
        s = ln.strip()
        if s and err_re.search(s) and s not in seen:
            seen.add(s)
            errs.append(s)
        m = lyr_re.search(ln)
        if m and m.group(1) not in layers:
            layers.append(m.group(1))
        m = sub_re.search(s)
        if m:
            pair = (m.group(1), (m.group(2) or "").strip())
            if pair not in subckts:
                subckts.append(pair)
    return {"errs": errs[-20:], "layers": layers, "subckts": subckts}


def _debug_help(job, tool, run_dir, runfile, deck, runset_src, src_net=None):
    """On a Calibre failure, print a copy/paste debug block -- to the console AND
    the run log (so it shows in the browser log view too): the runset, the deck,
    detected errors, and ready-to-run diff/grep/reproduce commands. For an LVS
    source-netlist gap (missing .SUBCKT) it also diffs the generated netlist vs a
    known-good one and lists which subckts are missing."""
    q = shlex.quote
    cell = job.meta.get("cell", "")
    cfg = job.meta.get("cfg_snapshot", {})
    calibre = cfg.get("calibre_bin") or "calibre"
    deckdir = os.path.dirname(deck) if deck else ""
    info = _scan_calibre_errors(job.log_path)
    errs, layers, subckts = info["errs"], info["layers"], info["subckts"]
    base = os.path.basename(runfile) if runfile else ""
    repro = ("%s -lvs -hier -spice %s.sp %s" % (calibre, cell, q(base))) if tool == "lvs" \
        else ("%s -drc -hier -turbo %s" % (calibre, q(base)))

    L = ["", "=" * 72,
         "=====  DEBUG HELP  (copy/paste a command below, run it, read me the output)  =====",
         "=" * 72,
         "generated runset : %s" % runfile,
         "deck INCLUDEd    : %s" % (deck or "(none)"),
         "run directory    : %s" % run_dir,
         "full run log     : %s" % job.log_path]
    if src_net:
        L.append("source netlist   : %s" % src_net)
    if runset_src:
        L.append("known-good runset: %s%s" % (runset_src,
                 "" if os.path.isfile(runset_src) else "   (NOT FOUND)"))
    if errs:
        L.append("")
        L.append("detected Calibre error(s):")
        L += ["   %s" % e for e in errs[:12]]
    L.append("")
    L.append("# ---------- copy/paste commands ----------")
    if runset_src and os.path.isfile(runset_src):
        L.append("# 1) diff the GUI-generated runset vs your known-good runset")
        L.append("#    (shows any #DEFINE / INCLUDE / option / #!tvf we dropped):")
        L.append("diff -u %s %s" % (q(runset_src), q(runfile)))
    else:
        L.append("# 1) show the runset the GUI generated (paste it back to me):")
        L.append("cat %s" % q(runfile))
    for lyr in layers:
        L.append("# 2) find where layer '%s' is supposed to be derived in the deck tree:" % lyr)
        L.append("grep -rniE %s %s | head -30" %
                 (q(r"\b%s\b" % lyr), q(deckdir or "<deck-dir>")))

    # ---- LVS source-netlist gap (missing .SUBCKT) ----
    if tool == "lvs" and (subckts or src_net):
        netlist = src_net or ""
        for _n, _f in subckts:
            if _f:
                netlist = _f
                break
        L.append("")
        L.append("# ---- LVS source-netlist diagnostics ----")
        if netlist:
            L.append("# how many .SUBCKT definitions vs X-instance references are in the netlist:")
            L.append("grep -c '^\\.SUBCKT' %s ; grep -c '^X' %s" % (q(netlist), q(netlist)))
        for name, _f in subckts:
            nf = _f or netlist or "<netlist>"
            L.append("# subckt '%s' is instantiated but never defined -- show both uses:" % name)
            L.append("grep -niE %s %s" %
                     (q(r"(\.SUBCKT[[:space:]]+%s\b|\b%s\b)" % (re.escape(name), re.escape(name))), q(nf)))
        # a known-good netlist to diff against: the prefilled runset's SOURCE, else discovered
        known = ""
        if runset_src and os.path.isfile(runset_src):
            known = (_parse_rule_file(runset_src) or {}).get("source_path") or ""
        if not known:
            try:
                cand = discover_src_net(cell, cfg)
                if cand and os.path.abspath(cand) != os.path.abspath(netlist or "x"):
                    known = cand
            except Exception:
                pass
        if known and netlist and os.path.abspath(known) != os.path.abspath(netlist):
            L.append("# 3) diff the (incomplete) generated netlist vs a known-good one:")
            L.append("diff -u %s %s" % (q(known), q(netlist)))
            L.append("# 3b) subckts present in known-good but MISSING from ours (the culprits):")
            L.append("comm -23 <(grep -oE '^\\.SUBCKT +[^ ]+' %s | awk '{print $2}' | sort -u) "
                     "<(grep -oE '^\\.SUBCKT +[^ ]+' %s | awk '{print $2}' | sort -u)"
                     % (q(known), q(netlist)))
        L.append("# note: a missing .SUBCKT usually means a subcell has NO schematic/auCdl view,")
        L.append("#       or is excluded by simStopList -- check the netlister log:")
        L.append("sed -n '1,60p' %s" % q(os.path.join(run_dir, "netlist_%s.log" % cell)))

    L.append("# 4) reproduce the exact Calibre run by hand (env must be module-loaded):")
    L.append("cd %s && %s" % (q(run_dir), repro))
    L.append("# 5) the full log:")
    L.append("less %s" % q(job.log_path))
    L.append("=" * 72)
    L.append("")
    block = "\n".join(L)
    sys.stdout.write(block)
    sys.stdout.flush()
    _log(job, block)                       # also into run.log -> visible in the browser


def run_job(job):
    cfg = job.meta["cfg_snapshot"]
    run_dir = job.meta["run_dir"]
    tool = job.meta["tool"]
    lib = job.meta["lib"]
    cell = job.meta["cell"]
    view = job.meta["view"]
    job.state = "running"
    _step_no = [0]

    def phase(desc):
        _step_no[0] += 1
        _banner("STEP %d:" % _step_no[0], desc)

    try:
        _banner("JOB START:", "%s   %s / %s / %s   (run dir: %s)" %
                (tool.upper(), lib, cell, view, run_dir))
        # baseline for the progress bar: newest comparable prior run's log size
        job.meta["baseline_bytes"] = _find_baseline_bytes(job.meta)

        # 0. make sure the EDA tools are on PATH (auto `module load` if needed).
        phase("Check environment (calibre/strmout on PATH; module-load if needed)")
        needed = ["calibre"] + ([] if job.meta.get("existing_gds") else ["strmout"])
        _ensure_tools(job, cfg, needed)
        for t in needed:
            sys.stdout.write("   -> %-14s %s\n" % (t + ":", shutil.which(_bin_for(cfg, t)) or "NOT FOUND"))
        sys.stdout.flush()

        gds = "%s.calibre.db" % cell
        gds_abs = os.path.join(run_dir, gds)

        # --- 1. stream out GDS from OA (unless user supplied an existing GDS) ---
        phase("Layout: %s" % ("use existing GDS" if job.meta.get("existing_gds")
                              else "stream out from OpenAccess (strmout)"))
        if job.meta.get("existing_gds"):
            src = os.path.abspath(os.path.expanduser(job.meta["existing_gds"]))
            _log(job, "Using existing GDS: %s\n" % src)
            if not os.path.isfile(src):
                raise RuntimeError("existing GDS not found: %s" % src)
            gds_abs = src
            gds = src
        else:
            # resolve a real cds.lib (config -> launch dir/parents -> $HOME)
            cds = resolve_cds_lib(cfg)
            if not cds:
                raise RuntimeError(
                    "cds.lib not found. Set 'cds_lib' in Config, or launch the GUI "
                    "from your project directory (the one containing cds.lib).")
            _artifact("cds.lib", cds)
            # resolve techLib / layerMap: config, else auto-detect from strmout logs
            techlib = (cfg.get("techlib") or "").strip()
            layermap = (cfg.get("layermap") or "").strip()
            if not techlib or not layermap:
                opt = discover_strmout_opts()
                if not techlib and opt["techlib"]:
                    techlib = opt["techlib"]
                    sys.stdout.write("   techLib auto-detected from strmout log: %s\n" % techlib)
                if not layermap and opt["layermap"]:
                    layermap = opt["layermap"]
                    sys.stdout.write("   layerMap auto-detected from strmout log: %s\n" % layermap)
                sys.stdout.flush()
            # give strmout a cds.lib in the run dir that INCLUDEs the real one
            local_cds = os.path.join(run_dir, "cds.lib")
            with open(local_cds, "w", encoding="utf-8") as f:
                f.write('INCLUDE "%s"\n' % cds)
            mapping = {
                "strmout_bin": cfg["strmout_bin"], "lib": lib, "cell": cell,
                "view": view, "gds": gds, "strmlog": "strmout_%s.log" % cell,
                "techlib": techlib, "layermap": layermap,
            }
            # drop -techLib / -layerMap if still blank so the command isn't corrupted
            cmd = _drop_valueless_flags(_fill(cfg["strmout_cmd"], mapping),
                                        {"-techLib", "-layerMap", "-layermap"})
            rc = _run_step(job, "strmout (OA->GDS)", cmd, run_dir)
            if rc != 0:
                raise RuntimeError("strmout failed (rc=%d) -- see log" % rc)
            if not os.path.isfile(gds_abs):
                raise RuntimeError("strmout reported ok but %s not found" % gds)
        _artifact("GDS", gds_abs if os.path.isabs(gds_abs) else os.path.join(run_dir, gds_abs))

        # --- 2. optional source-netlist generation for LVS ---
        src_net = None
        if tool == "lvs":
            phase("LVS source netlist")
            # 1) explicit path from the form, if given
            src_net_abs = ""
            explicit = (job.meta.get("src_net") or "").strip()
            if explicit:
                src_net_abs = explicit if os.path.isabs(explicit) else os.path.join(run_dir, explicit)
                if not os.path.isfile(src_net_abs):
                    sys.stdout.write("   -I- given src_net not found: %s -- will auto-search\n" % src_net_abs)
                    src_net_abs = ""
            # 2) a <cell>.src.net already sitting next to the layout / run dir
            if not src_net_abs:
                cand = os.path.join(run_dir, "%s.src.net" % cell)
                if os.path.isfile(cand):
                    src_net_abs = cand
            # 3) auto-discover across cds.lib libs / run dirs / sim_roots
            if not src_net_abs:
                found = discover_src_net(cell, cfg, run_dir)
                if found:
                    src_net_abs = found
                    sys.stdout.write("   -I- source netlist auto-detected: %s\n" % found)
            # 4) generate it from the schematic (si auCDL / virtuoso SKILL /
            #    custom netlister) -- unless netlist_mode=off.
            if not src_net_abs:
                src_view = (job.meta.get("src_view")
                            or cfg.get("netlist_view") or "schematic").strip()
                if not lib or lib == "existingGDS":
                    sys.stdout.write("   -I- no OA library for this run: cannot netlist a "
                                     "schematic -- provide a source netlist path\n")
                    sys.stdout.flush()
                else:
                    sys.stdout.write("   -I- no source netlist found -- generating from "
                                     "schematic (%s/%s/%s, mode=%s)\n" %
                                     (lib, cell, src_view, cfg.get("netlist_mode") or "si"))
                    sys.stdout.flush()
                    gen = _generate_src_net(job, cfg, run_dir, lib, cell, src_view)
                    if gen:
                        src_net_abs = gen
            if not src_net_abs or not os.path.isfile(src_net_abs):
                raise RuntimeError(
                    "LVS source netlist not found/generated for cell '%s'.\n"
                    "Provide the path in the 'LVS source netlist' field (a .src.net / .sp / "
                    ".cdl from the schematic), put a %s.src.net next to the layout, set a "
                    "schematic 'netlist_view'/'netlist_mode' in Config so it can be generated "
                    "with `si`/virtuoso, or set a custom 'netlist_cmd'." % (cell, cell))
            _artifact("source net", src_net_abs)
            src_net = src_net_abs

        # --- 3. write runset + launch calibre ---
        phase("Generate runset + run Calibre %s (the long step)" % tool.upper())
        if tool == "drc":
            _ex, _gl, _cf = job.meta.get("deck"), latest_deck("drc"), cfg["drc_deck"]
            sys.stdout.write("   deck resolution: explicit=%r glob-latest=%r config.drc_deck=%r\n"
                             "                    glob pattern=%r\n" %
                             (_ex, _gl, _cf, cfg.get("drc_deck_glob")))
            sys.stdout.flush()
            deck = _ex or _gl or _cf
            if not deck:                          # nothing configured -> auto-detect
                found = discover_decks("drc")
                if found:
                    deck = found[0]["deck"]
                    sys.stdout.write("   deck auto-detected from %s: %s\n" %
                                     (found[0]["via"], deck))
                    sys.stdout.flush()
            _require_deck(deck, "DRC")
            _artifact("deck", deck)
            runfile = _write_drc_runset(run_dir, cell, gds, deck, cfg.get("drc_extra_svrf", ""))
            _artifact("runset", runfile)
            cmd = _fill(cfg["drc_cmd"], {"calibre_bin": cfg["calibre_bin"],
                                        "runfile": os.path.basename(runfile)})
            rc = _run_step(job, "calibre DRC", cmd, run_dir)
            result_file = os.path.join(run_dir, "%s.drc.summary" % cell)
        else:
            # Reuse a known-good runset as a TEMPLATE (preserving #!tvf /
            # tvf::VERBATIM / SVDB spec / LVS REPORT OPTION / #DEFINE / INCLUDE),
            # rewriting only the cell name + LAYOUT/SOURCE/REPORT paths. The deck
            # and TVF scaffolding are cell-independent, so this works for ANY cell
            # -- not just the cell the runset was originally written for. A
            # synthesized plain-SVRF runset would drop those and break a TVF deck.
            # Priority: the runset prefilled for this run, else a configured
            # lvs_runset_template that applies to every LVS run.
            runset_src = (job.meta.get("runset_src") or "").strip()
            tmpl = ""
            for cand in (runset_src, (cfg.get("lvs_runset_template") or "").strip()):
                if cand and os.path.isfile(cand):
                    _p = _parse_rule_file(cand) or {}
                    if _p.get("tool") == "lvs" or _p.get("source_path") \
                            or ".lvs" in os.path.basename(cand).lower():
                        tmpl = cand
                        break
            if tmpl:
                _p = _parse_rule_file(tmpl) or {}
                deck = _p.get("deck") or job.meta.get("deck")
                runfile, changes = _write_lvs_runset_verbatim(run_dir, cell, gds, src_net, tmpl)
                same = (_p.get("cell") or "") == cell
                how = ("VERBATIM rerun of known-good runset"
                       if same else
                       "known-good runset used as a TEMPLATE (originally cell %r)" % (_p.get("cell") or "?"))
                origin = "prefilled" if tmpl == runset_src else "config lvs_runset_template"
                sys.stdout.write(
                    "   -I- %s  [%s]\n"
                    "       source : %s\n"
                    "       for cell: %s\n"
                    "       kept   : #!tvf / tvf::VERBATIM / MASK SVDB spec / LVS REPORT OPTION / #DEFINE / INCLUDE\n"
                    "       rewrote: %s\n" % (how, origin, tmpl, cell, "; ".join(changes) or "(nothing)"))
                sys.stdout.flush()
                _log(job, "### %s (%s) for cell %s\n### source: %s\n### rewrote: %s\n"
                     % (how, origin, cell, tmpl, "; ".join(changes)))
                _artifact("runset (from template)", runfile)
                if deck:
                    _artifact("deck (from runset)", deck)
            else:
                deck = job.meta.get("deck") or latest_deck("lvs") or cfg["lvs_deck"]
                if not deck:                          # nothing configured -> auto-detect
                    found = discover_decks("lvs")
                    if found:
                        deck = found[0]["deck"]
                        sys.stdout.write("   deck auto-detected from %s: %s\n" %
                                         (found[0]["via"], deck))
                        sys.stdout.flush()
                _require_deck(deck, "LVS")
                _artifact("deck", deck)
                _artifact("source net", src_net)
                runfile = _write_lvs_runset(run_dir, cell, gds, src_net, deck,
                                            cfg.get("lvs_extra_svrf", ""))
                _artifact("runset", runfile)
            cmd = _fill(cfg["lvs_cmd"], {"calibre_bin": cfg["calibre_bin"],
                                        "spiceout": "%s.sp" % cell,
                                        "runfile": os.path.basename(runfile)})
            rc = _run_step(job, "calibre LVS", cmd, run_dir)
            result_file = os.path.join(run_dir, "%s.lvs.report" % cell)
        _artifact("result", result_file)
        # On a Calibre failure (nonzero rc, or no result produced), emit a
        # copy/paste debug block with a ready diff vs the known-good runset.
        if rc != 0 or not os.path.isfile(result_file):
            _debug_help(job, tool, run_dir, runfile, deck, job.meta.get("runset_src"),
                        src_net if tool == "lvs" else None)

        # --- 4. parse result ---
        phase("Parse results")
        if os.path.isfile(result_file):
            parsed = detect_and_parse(result_file)
            parsed.pop("text", None)  # keep snapshot small
            job.result = parsed
        else:
            job.result = {"type": "error",
                          "error": "result file not produced: %s" % os.path.basename(result_file)}
        if rc != 0 and not os.path.isfile(result_file):
            raise RuntimeError("calibre exited rc=%d and produced no result" % rc)

        # persist metadata for the run registry / compare tab
        meta_out = dict(job.meta)
        meta_out.pop("cfg_snapshot", None)
        meta_out["result_file"] = result_file
        meta_out["result"] = job.result
        meta_out["state"] = "done"
        meta_out["finished"] = time.time()
        with open(os.path.join(run_dir, "metadata.json"), "w") as f:
            json.dump(meta_out, f, indent=2)

        job.state = "done"
        _banner("JOB DONE:", "%s %s -> %s" % (tool.upper(), cell,
                (job.result or {}).get("status", "done")))
    except Exception as e:
        job.error = str(e)
        job.state = "failed"
        _log(job, "\n!!! JOB FAILED: %s\n%s\n" % (e, traceback.format_exc()))
        _banner("JOB FAILED:", "%s %s" % (tool.upper(), cell))
        _err(str(e))
    finally:
        job.finished = time.time()


def start_job(meta):
    with JOBS_LOCK:
        _JOB_COUNTER[0] += 1
        job_id = "job%d_%d" % (_JOB_COUNTER[0], int(time.time()))
    ts = time.strftime("%Y%m%d_%H%M%S")
    run_name = "%s_%s_%s_%s_%s" % (meta["tool"], meta["lib"], meta["cell"],
                                   meta["view"], ts)
    run_name = re.sub(r"[^A-Za-z0-9_.-]", "_", run_name)
    run_dir = os.path.join(RUNS_BASE, run_name)
    os.makedirs(run_dir, exist_ok=True)
    meta["run_dir"] = run_dir
    meta["run_name"] = run_name
    meta["timestamp"] = ts
    job = Job(job_id, meta)
    with JOBS_LOCK:
        JOBS[job_id] = job
    t = threading.Thread(target=run_job, args=(job,), daemon=True)
    t.start()
    return job


def _find_baseline_bytes(meta):
    """Newest comparable prior run's run.log size, for progress estimation.
    Prefer a run with the same tool AND cell; fall back to same tool."""
    same_cell = None
    any_tool = None
    for r in list_runs():                       # newest-first
        if r.get("tool") != meta.get("tool"):
            continue
        rp = os.path.join(RUNS_BASE, r["run_name"], "run.log")
        if not os.path.isfile(rp):
            continue
        sz = os.path.getsize(rp)
        if any_tool is None:
            any_tool = sz
        if r.get("cell") == meta.get("cell"):
            same_cell = sz
            break
    return same_cell if same_cell is not None else (any_tool or 0)


def list_runs():
    """Scan RUNS_BASE for completed runs (metadata.json)."""
    runs = []
    if not os.path.isdir(RUNS_BASE):
        return runs
    for name in sorted(os.listdir(RUNS_BASE), reverse=True):
        d = os.path.join(RUNS_BASE, name)
        mpath = os.path.join(d, "metadata.json")
        if os.path.isfile(mpath):
            try:
                with open(mpath) as f:
                    m = json.load(f)
                runs.append({
                    "run_name": name,
                    "tool": m.get("tool"),
                    "lib": m.get("lib"),
                    "cell": m.get("cell"),
                    "view": m.get("view"),
                    "timestamp": m.get("timestamp"),
                    "result_file": m.get("result_file"),
                    "status": (m.get("result") or {}).get("status", "?"),
                })
            except Exception:
                pass
    return runs


# --------------------------------------------------------------------------- #
#  Compare
# --------------------------------------------------------------------------- #

def compare_results(path_a, path_b):
    a = detect_and_parse(path_a)
    b = detect_and_parse(path_b)
    out = {"a": {k: v for k, v in a.items() if k != "text"},
           "b": {k: v for k, v in b.items() if k != "text"},
           "a_path": path_a, "b_path": path_b,
           "a_mtime": os.path.getmtime(path_a) if os.path.isfile(path_a) else 0,
           "b_mtime": os.path.getmtime(path_b) if os.path.isfile(path_b) else 0}

    # DRC vs DRC -> per-rule diff
    if a.get("type") == "drc" and b.get("type") == "drc":
        rules = sorted(set(a["rules"]) | set(b["rules"]))
        rows = []
        for r in rules:
            ca = a["rules"].get(r, {}).get("count")
            cb = b["rules"].get(r, {}).get("count")
            va = 0 if ca is None else ca
            vb = 0 if cb is None else cb
            if ca is None:
                st = "only_b"
            elif cb is None:
                st = "only_a"
            elif va == vb:
                st = "same"
            elif vb < va:
                st = "improved"
            else:
                st = "worse"
            if st != "same" or va > 0:
                rows.append({"rule": r, "a": ca, "b": cb, "delta": vb - va, "status": st})
        out["drc_diff"] = {
            "rows": rows,
            "total_a": a.get("total_violations"),
            "total_b": b.get("total_violations"),
        }

    # LVS vs LVS -> status diff
    if a.get("type") == "lvs" and b.get("type") == "lvs":
        out["lvs_diff"] = {
            "status_a": a.get("status"), "status_b": b.get("status"),
            "unmatched_a": a.get("total_unmatched"),
            "unmatched_b": b.get("total_unmatched"),
            "changed": a.get("status") != b.get("status"),
        }

    # Always provide a raw unified text diff (truncated).
    ta = a.get("text", "").splitlines()
    tb = b.get("text", "").splitlines()
    diff = list(difflib.unified_diff(ta, tb,
                                     fromfile=os.path.basename(path_a),
                                     tofile=os.path.basename(path_b), lineterm=""))
    MAXLINES = 4000
    truncated = len(diff) > MAXLINES
    out["text_diff"] = "\n".join(diff[:MAXLINES])
    out["text_diff_truncated"] = truncated
    return out


# --------------------------------------------------------------------------- #
#  HTTP server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "CalibreGUI/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet

    # Client disconnects mid-response are normal (cancelled polls, favicon, tab
    # close); swallow the resulting socket errors instead of dumping a traceback.
    _DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except self._DISCONNECT:
            self.close_connection = True

    # ---- helpers ----
    def _write(self, body, code, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except self._DISCONNECT:
            self.close_connection = True          # client went away; ignore

    def _send_json(self, obj, code=200):
        self._write(json.dumps(obj).encode("utf-8"), code, "application/json")

    def _send_html(self, text, code=200):
        self._write(text.encode("utf-8"), code, "text/html; charset=utf-8")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        try:
            if path == "/favicon.ico":
                return self._write(b"", 204, "image/x-icon")   # no favicon; skip 404
            if path == "/" or path == "/index.html":
                return self._send_html(INDEX_HTML.replace("__REV__", str(APP_REVISION)))
            if path == "/api/config":
                with CONFIG_LOCK:
                    return self._send_json(dict(CONFIG))
            if path == "/api/libs":
                libs = parse_cds_lib(resolve_cds_lib())
                items = [{"name": n, "path": p, "exists": os.path.isdir(p)}
                         for n, p in sorted(libs.items())]
                return self._send_json({"cds_lib": resolve_cds_lib(), "libs": items})
            if path == "/api/cells":
                lib = q.get("lib", [""])[0]
                libs = parse_cds_lib(resolve_cds_lib())
                if lib not in libs:
                    return self._send_json({"error": "unknown lib %r" % lib}, 400)
                return self._send_json({"cells": list_cells(libs[lib])})
            if path == "/api/views":
                lib = q.get("lib", [""])[0]
                cell = q.get("cell", [""])[0]
                libs = parse_cds_lib(resolve_cds_lib())
                if lib not in libs:
                    return self._send_json({"error": "unknown lib"}, 400)
                return self._send_json({"views": list_views(libs[lib], cell)})
            if path == "/api/runs":
                return self._send_json({"runs": list_runs()})
            if path == "/api/job":
                jid = q.get("id", [""])[0]
                job = JOBS.get(jid)
                if not job:
                    return self._send_json({"error": "no such job"}, 404)
                return self._send_json(job.snapshot())
            if path == "/api/envcheck":
                return self._send_json(env_status())
            if path == "/api/startup":
                return self._send_json({"logs": STARTUP_LOGS})
            if path == "/api/debuglog":
                dl = os.path.join(RUNS_BASE, "gui_debug.log")
                txt = ""
                if os.path.isfile(dl):
                    with open(dl, "r", errors="replace") as f:
                        txt = f.read()[-20000:]
                return self._send_json({"path": dl, "text": txt})
            if path == "/api/decks":
                return self._send_json(list_decks(q.get("kind", ["drc"])[0]))
            if path == "/api/discover_decks":
                return self._send_json({"decks": discover_decks(q.get("kind", ["drc"])[0])})
            if path == "/api/recent_layouts":
                return self._send_json(recent_layouts())
            if path == "/api/rulefiles":
                return self._send_json(scan_rule_files())
            if path == "/api/searchlogs":
                return self._send_json(search_logs(
                    user=q.get("user", [""])[0],
                    extra=q.get("extra", [""])[0]))
            if path == "/api/prefill":
                p = q.get("path", [""])[0]
                return self._send_json(extract_design_from_file(p))
            if path == "/api/result":
                p = q.get("path", [""])[0]
                res = detect_and_parse(p)
                # cap text size sent to browser
                if "text" in res and len(res["text"]) > 400000:
                    res["text"] = res["text"][:400000] + "\n...[truncated]..."
                return self._send_json(res)
            return self._send_json({"error": "not found"}, 404)
        except self._DISCONNECT:
            self.close_connection = True          # client closed mid-request
        except Exception as e:
            return self._send_json({"error": str(e),
                                    "trace": traceback.format_exc()}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        try:
            body = self._read_json_body()
            if path == "/api/run":
                return self._handle_run(body)
            if path == "/api/stop":
                job = JOBS.get(body.get("job_id", ""))
                if not job:
                    return self._send_json({"error": "no such job"}, 404)
                killed = job.stop()
                _err("STOP requested for job %s (%s)" % (job.id, job.meta.get("cell", "")))
                return self._send_json({"ok": True, "killed_process": killed})
            if path == "/api/loadmodules":
                return self._send_json(load_modules(body.get("modules", "")))
            if path == "/api/compare":
                a = body.get("a", "")
                b = body.get("b", "")
                if not a or not b:
                    return self._send_json({"error": "need both a and b"}, 400)
                return self._send_json(compare_results(a, b))
            if path == "/api/config":
                with CONFIG_LOCK:
                    for k, v in body.items():
                        if k in DEFAULT_CONFIG:
                            CONFIG[k] = v
                    save_config(CONFIG_PATH, CONFIG)
                    return self._send_json({"ok": True, "config": dict(CONFIG)})
            return self._send_json({"error": "not found"}, 404)
        except self._DISCONNECT:
            self.close_connection = True          # client closed mid-request
        except Exception as e:
            return self._send_json({"error": str(e),
                                    "trace": traceback.format_exc()}, 500)

    def _handle_run(self, body):
        tool = body.get("tool")
        lib = body.get("lib")
        cell = body.get("cell")
        view = body.get("view")
        existing_gds = body.get("existing_gds", "").strip()
        if tool not in ("drc", "lvs") or not cell:
            return self._send_json({"error": "need tool(drc|lvs) and cell"}, 400)
        if not existing_gds and not (lib and view):
            return self._send_json(
                {"error": "need lib and view (or provide an existing GDS)"}, 400)
        with CONFIG_LOCK:
            cfg_snap = dict(CONFIG)
        meta = {
            "tool": tool, "lib": lib or "existingGDS", "cell": cell,
            "view": view or "layout",
            "deck": body.get("deck", "").strip() or None,
            "src_net": body.get("src_net", "").strip() or None,
            "src_view": body.get("src_view", "").strip() or None,
            "runset_src": body.get("runset_src", "").strip() or None,  # known-good runset (debug diff)
            "existing_gds": body.get("existing_gds", "").strip() or None,
            "cfg_snapshot": cfg_snap,
        }
        job = start_job(meta)
        return self._send_json({"job_id": job.id, "run_dir": meta["run_dir"]})


# --------------------------------------------------------------------------- #
#  Front-end (single page, vanilla JS -- no external assets)
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Calibre DRC / LVS GUI</title>
<style>
 /* Confluence / Atlassian light theme (palette from borenw.github.io) */
 :root{--bg:#F4F5F7;--panel:#FFFFFF;--panel2:#F4F5F7;--fg:#172B4D;--muted:#5E6C84;
       --acc:#0052CC;--acc-dark:#003D99;--acc-light:#DEEBFF;--acc-lighter:#F4F8FF;
       --good:#00875A;--good-bg:#E3FCEF;--bad:#DE350B;--bad-bg:#FFEBE6;
       --warn:#FF8B00;--warn-bg:#FFF7E6;--grey:#97A0AF;--line:#DFE1E6;}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
 header{background:linear-gradient(135deg,var(--acc) 0%,var(--acc-dark) 100%);
        color:#fff;padding:14px 22px;display:flex;align-items:center;gap:16px}
 header h1{font-size:16px;margin:0;font-weight:600;color:#fff}
 header .sub{color:rgba(255,255,255,.85);font-size:12px}
 .tabs{display:flex;gap:2px;padding:0 18px;background:var(--panel);
       border-bottom:2px solid var(--line)}
 .tab{padding:11px 18px;cursor:pointer;color:var(--muted);font-weight:500;
      border-bottom:2px solid transparent;margin-bottom:-2px}
 .tab:hover{color:var(--acc)}
 .tab.active{color:var(--acc);border-bottom-color:var(--acc)}
 main{padding:22px 18px;max-width:1200px;margin:0 auto}
 .panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;
        padding:18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(9,30,66,.08)}
 .panel h2{margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;
           color:var(--muted);font-weight:700}
 label{display:block;font-size:12px;color:var(--muted);margin:8px 0 3px;font-weight:500}
 select,input[type=text],textarea{width:100%;background:#fff;color:var(--fg);
        border:1px solid var(--line);border-radius:4px;padding:8px 10px;font:inherit}
 select:focus,input[type=text]:focus,textarea:focus{outline:none;border-color:var(--acc);
        box-shadow:0 0 0 2px var(--acc-light)}
 textarea{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .row>div{flex:1;min-width:180px}
 button{background:var(--acc);color:#fff;border:0;border-radius:4px;padding:9px 18px;
        font-weight:600;cursor:pointer;font:inherit}
 button:hover{background:var(--acc-dark)}
 button.sec{background:#fff;color:var(--acc);border:1px solid var(--line)}
 button.sec:hover{background:var(--acc-lighter)}
 button:disabled{opacity:.5;cursor:default}
 .radio{display:inline-flex;gap:14px;margin-top:4px}
 .radio label{display:inline-flex;align-items:center;gap:5px;color:var(--fg);margin:0;font-weight:500}
 pre{background:var(--panel2);border:1px solid var(--line);border-radius:4px;padding:12px;
     overflow:auto;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
     color:var(--fg);max-height:420px;white-space:pre-wrap;word-break:break-word}
 table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
 th,td{border:1px solid var(--line);padding:6px 10px;text-align:left}
 th{background:var(--acc-light);color:var(--fg);font-weight:600}
 tr:hover td{background:var(--acc-lighter)}
 .pill{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600}
 .pill.good{background:var(--good-bg);color:var(--good)}
 .pill.bad{background:var(--bad-bg);color:var(--bad)}
 .pill.warn{background:var(--warn-bg);color:var(--warn)}
 .pill.muted{background:var(--panel2);color:var(--muted)}
 .muted{color:var(--muted)}
 .status-improved{color:var(--good);font-weight:600} .status-worse{color:var(--bad);font-weight:600}
 .status-only_a{color:var(--warn);font-weight:600} .status-only_b{color:var(--acc);font-weight:600}
 .hidden{display:none}
 .flexbtn{display:flex;gap:10px;align-items:center;margin-top:14px}
 details summary{cursor:pointer;color:var(--acc);font-size:12px;font-weight:500}
 .kv{display:grid;grid-template-columns:auto 1fr;gap:3px 14px;font-size:13px}
 .kv div:nth-child(odd){color:var(--muted)}
 code{background:var(--panel2);padding:1px 5px;border-radius:3px;
      font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em;color:#172B4D}
 .diff-add{color:var(--good)} .diff-del{color:var(--bad)} .diff-hdr{color:var(--acc);font-weight:600}
 /* spinner + progress bar */
 .spinner{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
          border-top-color:var(--acc);border-radius:50%;animation:spin .7s linear infinite;
          vertical-align:-2px;margin-right:5px}
 @keyframes spin{to{transform:rotate(360deg)}}
 .progbar{height:10px;background:var(--panel2);border:1px solid var(--line);
          border-radius:6px;overflow:hidden;position:relative}
 .progfill{height:100%;width:0;border-radius:6px;
           background:linear-gradient(90deg,var(--acc),var(--acc-dark));transition:width .5s ease}
 .progfill.indet{width:35%;position:absolute;animation:indet 1.2s infinite ease-in-out}
 @keyframes indet{0%{left:-35%}100%{left:100%}}
 .progfill.bad{background:var(--bad)} .progfill.good{background:var(--good)}
 th.sortable{cursor:pointer;user-select:none} th.sortable:hover{color:var(--acc)}
 th.sortable .arrow{opacity:.5;font-size:10px}
 .combowrap{display:flex;gap:4px;position:relative}
 .combobtn{padding:8px 11px;flex:0 0 auto;font-size:12px}
 .combomenu{position:absolute;top:calc(100% + 2px);left:0;right:0;z-index:60;background:var(--panel);
            border:1px solid var(--line);border-radius:6px;max-height:260px;overflow:auto;display:none;
            box-shadow:0 6px 18px rgba(9,30,66,.18)}
 .comboitem{padding:7px 11px;cursor:pointer;font-size:13px}
 .comboitem:hover{background:var(--acc-lighter)}
 /* big bright call-to-action button */
 .bigbtn{font-size:17px;padding:15px 34px;font-weight:700;border-radius:9px;letter-spacing:.02em;
         color:#fff;border:0;cursor:pointer;
         background:linear-gradient(135deg,#12c99b 0%,#0a8f6f 100%);
         box-shadow:0 3px 10px rgba(10,143,111,.38)}
 .bigbtn:hover{filter:brightness(1.08);box-shadow:0 4px 14px rgba(10,143,111,.5)}
 .bigbtn:disabled{opacity:.55;cursor:default;filter:none}
 /* big red round GO button */
 .gobtn{width:104px;height:104px;border-radius:50%;border:0;cursor:pointer;flex:0 0 auto;
        background:radial-gradient(circle at 38% 34%,#ff5a4d 0%,#e5352b 55%,#c31d14 100%);
        color:#fff;font-size:32px;font-weight:800;letter-spacing:2px;
        box-shadow:0 5px 16px rgba(197,29,20,.5),inset 0 2px 4px rgba(255,255,255,.25);
        transition:transform .08s ease,filter .15s ease}
 .gobtn:hover{filter:brightness(1.06)} .gobtn:active{transform:scale(.95)}
 .gobtn:disabled{opacity:.5;cursor:default;filter:grayscale(.3)}
</style></head>
<body>
<header>
  <h1>Calibre&nbsp;DRC&nbsp;/&nbsp;LVS</h1>
  <span class="sub">runs in your launching shell &mdash; env inherited &bull; <span id="cdslabel"></span></span>
  <span style="margin-left:auto;font-weight:700;font-size:13px;background:rgba(255,255,255,.20);padding:5px 13px;border-radius:14px;white-space:nowrap">rev __REV__</span>
</header>
<div class="tabs">
  <div class="tab active" data-tab="run">Run</div>
  <div class="tab" data-tab="history">History</div>
  <div class="tab" data-tab="compare">Compare</div>
  <div class="tab" data-tab="config">Config</div>
</div>
<main>

<!-- ============ RUN ============ -->
<section id="tab-run">
  <div class="panel" style="border:2px solid var(--acc)">
    <h2 style="color:var(--acc)">Easy &mdash; one click does everything</h2>
    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <button id="easybtn" class="gobtn">GO</button>
      <span class="muted" style="font-size:12px;max-width:640px">
        Loads modules &rarr; if you've pasted a log/runset in <b>Prefill</b> above (e.g. a
        Calibre <code>_calibre.lvs_</code>), re-runs <b>that</b>; otherwise finds your most
        recent DRC log &rarr; re-runs it &rarr; live progress bar with %% and ETA below.</span>
    </div>
    <div id="easymsg" class="muted" style="margin-top:8px"></div>
  </div>

  <div id="envbanner" class="panel" style="display:none"></div>

  <div class="panel">
    <h2>Prefill from a previous log</h2>
    <div class="row">
      <div style="flex:3">
        <label>Path to a .log / .drc.summary / .lvs.report / runset (incl. Calibre <code>_calibre.lvs_</code>) &mdash; fills tool, lib, cell, view; a rule file also fills deck + source netlist (+ reuses the prior GDS)</label>
        <input type="text" id="prefillpath" placeholder="/path/to/cell.lvs.report  or  a runset (_calibre.lvs_ / cell.lvs.rule)  or  strmout.log">
      </div>
      <div style="flex:0 0 auto;display:flex;align-items:flex-end;gap:8px">
        <button class="sec" id="prefillbtn">Prefill</button>
        <button class="sec" id="searchtoggle">Search logs&hellip;</button>
      </div>
    </div>
    <div id="prefillmsg" class="muted" style="margin-top:6px"></div>

    <div id="searchpanel" class="hidden" style="margin-top:14px;border-top:1px solid var(--line);padding-top:14px">
      <div class="row">
        <div><label>User <span class="muted">(defaults to your login name)</span></label>
          <input type="text" id="simuser" placeholder="(defaults to login name)"></div>
        <div style="flex:2"><label>Extra folder(s) to search <span class="muted">(optional, comma/newline)</span></label>
          <input type="text" id="simextra" placeholder="/some/other/path  (use {user} if you like)"></div>
        <div style="flex:0 0 auto;display:flex;align-items:flex-end">
          <button id="searchbtn">Search</button></div>
      </div>
      <div id="searchmsg" class="muted" style="margin:8px 0"></div>
      <div id="searchroots" class="muted" style="font-size:12px;margin-bottom:6px"></div>
      <div style="max-height:340px;overflow:auto">
        <table id="searchtable"><thead><tr>
          <th class="sortable" data-k="mtime">Modified <span class="arrow"></span></th>
          <th class="sortable" data-k="type">Type <span class="arrow"></span></th>
          <th class="sortable" data-k="path">Full path <span class="arrow"></span></th>
          <th class="sortable" data-k="size">Size <span class="arrow"></span></th>
          <th></th>
        </tr></thead><tbody></tbody></table>
      </div>
    </div>
  </div>

  <div class="panel" id="recentpanel">
    <h2>Recently edited layouts &mdash; suggested checks
      <button class="sec" id="recentrefresh" style="float:right;padding:4px 10px">refresh</button></h2>
    <div id="recentmsg" class="muted" style="font-size:12px;margin-bottom:6px"></div>
    <div style="max-height:230px;overflow:auto">
      <table id="recenttable"><thead><tr>
        <th>Modified</th><th>Library</th><th>Cell</th><th>View</th><th>Suggested</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class="panel" id="rulepanel">
    <h2>Existing runsets found &mdash; reuse a DRC/LVS setup
      <button class="sec" id="rulerefresh" style="float:right;padding:4px 10px">refresh</button></h2>
    <div id="rulemsg" class="muted" style="font-size:12px;margin-bottom:6px"></div>
    <div style="max-height:240px;overflow:auto">
      <table id="ruletable"><thead><tr>
        <th>Modified</th><th>Tool</th><th>Cell</th><th>Deck (INCLUDE)</th><th>Layout</th><th></th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class="panel">
    <h2>Select design</h2>
    <div class="radio" style="margin-bottom:8px">
      <label><input type="radio" name="tool" value="drc" checked> DRC</label>
      <label><input type="radio" name="tool" value="lvs"> LVS</label>
    </div>
    <div class="row">
      <div><label>Library <span class="muted">(type to search)</span></label>
        <input type="text" id="lib" list="liblist" autocomplete="off" placeholder="type or pick...">
        <datalist id="liblist"></datalist></div>
      <div><label>Cell <span class="muted">(type to search)</span></label>
        <input type="text" id="cell" list="celllist" autocomplete="off" placeholder="type or pick...">
        <datalist id="celllist"></datalist></div>
      <div><label>View</label>
        <input type="text" id="view" list="viewlist" autocomplete="off" placeholder="type or pick...">
        <datalist id="viewlist"></datalist></div>
    </div>
    <div class="row" style="margin-top:6px">
      <div>
        <label>Rule deck <span class="muted" id="deckhint">(auto: latest revision)</span></label>
        <input type="text" id="deck" list="decklist" autocomplete="off" placeholder="(latest revision auto-selected)">
        <datalist id="decklist"></datalist>
      </div>
    </div>
    <div id="lvsonly" class="hidden">
      <div class="row">
        <div>
          <label>LVS source netlist &mdash; <b>leave blank to auto-detect or generate</b> from the schematic, or give a path (.src.net / .cdl / .sp)</label>
          <input type="text" id="srcnet" placeholder="blank = find (cell).src.net, else netlist the schematic via si/virtuoso">
        </div>
      </div>
      <div class="row" style="margin-top:6px">
        <div>
          <label>Schematic source view <span class="muted">(for netlisting; blank = config <code>netlist_view</code>, default schematic)</span></label>
          <input type="text" id="srcview" placeholder="schematic">
        </div>
      </div>
    </div>
    <details style="margin-top:10px"><summary>Advanced: use existing GDS instead of streaming out</summary>
      <label>Existing GDS/OASIS path (skips strmout)</label>
      <input type="text" id="existinggds" placeholder="(leave blank to run strmout from OA)">
    </details>
    <div class="flexbtn">
      <button id="runbtn">Run</button>
      <span id="runmsg" class="muted"></span>
    </div>
  </div>

  <div class="panel" id="livepanel" style="display:none">
    <h2>Run status &nbsp;<span id="jobstate" class="pill muted"></span>
      <button class="sec" id="stopbtn" style="float:right;padding:4px 14px;color:var(--bad);border-color:var(--bad);display:none">&#9632; Stop</button></h2>
    <div id="progwrap" style="margin:2px 0 14px">
      <div style="display:flex;align-items:center;gap:14px">
        <div class="progbar" style="flex:1"><div id="progfill" class="progfill"></div></div>
        <div id="progpct" style="font-size:24px;font-weight:800;min-width:96px;text-align:right;white-space:nowrap"></div>
      </div>
      <div id="progtext" style="font-size:15px;margin-top:7px;color:var(--fg)"></div>
    </div>
    <div id="steps"></div>
    <details open><summary>Live log</summary><pre id="joblog">...</pre></details>
  </div>

  <div class="panel" id="resultpanel" style="display:none;border:2px solid var(--line)">
    <h2>Result &nbsp;<span id="resultpill"></span></h2>
    <div id="resultbody"></div>
    <div id="resultactions" style="margin-top:14px;display:none">
      <button class="sec" id="result2compare">Compare this run with previous &#9656;</button>
    </div>
  </div>
</section>

<!-- ============ HISTORY ============ -->
<section id="tab-history" class="hidden">
  <div class="panel">
    <h2>Previous runs <button class="sec" id="refreshruns" style="float:right;padding:4px 10px">refresh</button></h2>
    <table id="runstable"><thead><tr>
      <th>When</th><th>Tool</th><th>Lib</th><th>Cell</th><th>View</th><th>Status</th><th></th>
    </tr></thead><tbody></tbody></table>
  </div>
  <div class="panel" id="histresult" style="display:none">
    <h2>Result</h2><div id="histresultbody"></div>
  </div>
</section>

<!-- ============ COMPARE ============ -->
<section id="tab-compare" class="hidden">
  <div class="panel">
    <h2>Compare two results</h2>
    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:14px">
      <button class="gobtn" id="cmpeasybtn">GO</button>
      <span class="muted" style="font-size:12px;max-width:420px"><b>Easy compare</b> &mdash; auto-fill &amp; compare the 2 most recent result logs.</span>
      <span id="cmpmsg" class="muted" style="font-size:12px"></span>
    </div>
    <div class="row">
      <div>
        <label>Run A (from history)</label><select id="cmpRunA"></select>
        <label>Result path A <span class="muted">(auto-filled from the dropdown; editable)</span></label>
        <div style="display:flex;gap:6px">
          <input type="text" id="cmpPathA" placeholder="/path/to/cellA.drc.summary / .lvs.report / .log">
          <button class="sec" data-copy="cmpPathA" style="flex:0 0 auto;padding:8px 12px">copy</button>
        </div>
      </div>
      <div>
        <label>Run B (from history)</label><select id="cmpRunB"></select>
        <label>Result path B <span class="muted">(auto-filled from the dropdown; editable)</span></label>
        <div style="display:flex;gap:6px">
          <input type="text" id="cmpPathB" placeholder="/path/to/cellB.drc.summary / .lvs.report / .log">
          <button class="sec" data-copy="cmpPathB" style="flex:0 0 auto;padding:8px 12px">copy</button>
        </div>
      </div>
    </div>
    <div class="flexbtn" style="gap:14px"><button class="gobtn" id="cmpbtn">GO</button>
      <span class="muted" style="font-size:12px">Compare the A / B selected above.</span></div>
  </div>
  <div id="cmpout"></div>
</section>

<!-- ============ CONFIG ============ -->
<section id="tab-config" class="hidden">
  <div class="panel">
    <h2>Configuration &nbsp;<span class="muted">(persisted to config json)</span></h2>
    <div id="cfgfields"></div>
    <div class="flexbtn"><button id="savecfg">Save config</button><span id="cfgmsg" class="muted"></span></div>
  </div>
</section>

</main>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
async function jget(u){const r=await fetch(u);return r.json();}
async function jgetT(u,ms){                 // fetch with a hard timeout
  const c=new AbortController();const t=setTimeout(()=>c.abort(),ms);
  try{const r=await fetch(u,{signal:c.signal});return await r.json();}
  finally{clearTimeout(t);}
}
async function showDebugTail(target,intro){ // dump the tail of the server debug log on screen
  let txt='';try{const d=await jget('/api/debuglog');txt=(d.text||'').split('\n').slice(-30).join('\n');}catch(e){}
  target.innerHTML=intro+'<pre>'+esc(txt||'(debug log empty)')+'</pre>';
}
async function jpost(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json();}
function esc(s){return (s==null?'':(''+s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

// ---- tabs ----
$$('.tab').forEach(t=>t.onclick=()=>{
  $$('.tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');
  $$('main>section').forEach(s=>s.classList.add('hidden'));
  $('#tab-'+t.dataset.tab).classList.remove('hidden');
  if(t.dataset.tab==='history')loadRuns();
  if(t.dataset.tab==='compare')loadCmpRuns();
  if(t.dataset.tab==='config')loadConfig();
});

// ---- tool radio ----
$$('input[name=tool]').forEach(r=>r.onchange=()=>{
  $('#lvsonly').classList.toggle('hidden', currentTool()!=='lvs');
  loadDecks();
});
function currentTool(){return $$('input[name=tool]').find(r=>r.checked).value;}

// ---- deck auto-discovery (newest revision first) ----
function fmtDate(t){return new Date(t*1000).toLocaleDateString();}
async function loadDecks(){
  const kind=currentTool();
  const d=await jget('/api/decks?kind='+kind);
  fillList('decklist',(d.decks||[]).map(x=>x.path));
  const latest=(d.decks||[])[0];
  // auto-select the newest, unless the user has hand-edited the field
  if(d.latest && ($('#deck').value==='' || $('#deck').dataset.auto==='1')){
    $('#deck').value=d.latest;$('#deck').dataset.auto='1';
  }
  $('#deckhint').innerHTML = latest
    ? '&mdash; latest: <b>'+esc(latest.name)+'</b> ('+fmtDate(latest.mtime)+') &bull; '+
      (d.decks.length)+' rev'+(d.decks.length===1?'':'s')+' found'
    : '<span style="color:var(--bad)">no deck matched glob &mdash; set '+kind+'_deck_glob in Config</span>';
}
// if the user types their own deck, stop auto-overwriting it
$('#deck').addEventListener('input',()=>{$('#deck').dataset.auto='0';});

// ---- lib/cell/view cascades (searchable datalist inputs) ----
let KNOWN_LIBS=new Set();
function fillList(id,items){
  const dl=$('#'+id);dl.innerHTML='';
  items.forEach(v=>{const o=document.createElement('option');o.value=v;dl.appendChild(o);});
}
// give a datalist <input> an explicit dropdown button (click to see & filter all
// options) while keeping free-text typing. Works in every browser.
function attachCombo(inputId,listId){
  const inp=$('#'+inputId); if(!inp||inp.dataset.combo)return; inp.dataset.combo='1';
  const wrap=document.createElement('div'); wrap.className='combowrap';
  inp.parentNode.insertBefore(wrap,inp); wrap.appendChild(inp);
  const btn=document.createElement('button'); btn.type='button'; btn.className='sec combobtn';
  btn.textContent='▼'; wrap.appendChild(btn);
  const menu=document.createElement('div'); menu.className='combomenu'; wrap.appendChild(menu);
  const opts=()=>[...$('#'+listId).options].map(o=>o.value);
  function show(){
    const q=inp.value.toLowerCase();
    const items=opts().filter(v=>v.toLowerCase().indexOf(q)>=0);
    menu.innerHTML=items.length?items.map(v=>'<div class="comboitem">'+esc(v)+'</div>').join('')
      :'<div class="comboitem muted">no matches</div>';
    menu.querySelectorAll('.comboitem').forEach(el=>el.onclick=()=>{
      if(el.classList.contains('muted'))return;
      inp.value=el.textContent; menu.style.display='none'; inp.dispatchEvent(new Event('change'));});
    menu.style.display='block';
  }
  btn.onclick=()=>{ menu.style.display==='block'?menu.style.display='none':show(); };
  inp.addEventListener('input',()=>{ if(menu.style.display==='block')show(); });
  document.addEventListener('click',e=>{ if(!wrap.contains(e.target))menu.style.display='none'; });
}
async function loadLibs(){
  const d=await jget('/api/libs');
  $('#cdslabel').textContent='cds.lib: '+d.cds_lib;
  const names=(d.libs||[]).map(l=>l.name);
  KNOWN_LIBS=new Set(names);
  fillList('liblist',(d.libs||[]).map(l=>l.name+(l.exists?'':'  (missing!)')));
  // datalist option values carry the suffix; store clean names for matching
  fillList('liblist',names);
  if(names.length && !$('#lib').value){$('#lib').value=names[0];await loadCells();}
}
async function loadCells(){
  const lib=$('#lib').value.trim();if(!lib)return;
  $('#cell').placeholder='loading cells...';
  const d=await jget('/api/cells?lib='+encodeURIComponent(lib));
  if(d.error){$('#cell').placeholder=d.error;fillList('celllist',[]);return;}
  fillList('celllist',d.cells||[]);
  $('#cell').placeholder='type or pick... ('+(d.cells||[]).length+')';
}
async function loadViews(){
  const lib=$('#lib').value.trim(),cell=$('#cell').value.trim();
  if(!lib||!cell)return;
  const d=await jget('/api/views?lib='+encodeURIComponent(lib)+'&cell='+encodeURIComponent(cell));
  fillList('viewlist',d.views||[]);
  if(!$('#view').value && (d.views||[]).includes('layout'))$('#view').value='layout';
}
// react when a lib/cell is chosen or typed (change fires on datalist pick + blur)
$('#lib').addEventListener('change',()=>{loadCells();});
$('#cell').addEventListener('change',()=>{loadViews();});

// ---- prefill from a log ----
let LAST_RULEFILE='';   // known-good runset last prefilled from (for the failure-debug diff)
$('#prefillbtn').onclick=doPrefill;
async function doPrefill(){
  const p=$('#prefillpath').value.trim();
  if(!p){$('#prefillmsg').textContent='enter a path';return null;}
  $('#prefillmsg').textContent='reading...';
  const d=await jget('/api/prefill?path='+encodeURIComponent(p));
  LAST_RULEFILE = d.rulefile || '';   // set when the pasted file was an SVRF rule file
  if(d.tool){$$('input[name=tool]').forEach(r=>r.checked=(r.value===d.tool));
    $('#lvsonly').classList.toggle('hidden',d.tool!=='lvs');}
  if(d.lib)$('#lib').value=d.lib;
  if(d.lib)await loadCells();
  if(d.cell)$('#cell').value=d.cell;
  if(d.cell)await loadViews();
  if(d.view)$('#view').value=d.view;
  // From an SVRF rule file (_calibre.lvs_ / *.lvs.rule): also carry the deck and
  // the source netlist so a rerun reuses the exact same setup.
  if(d.deck){ $('#deck').value = d.deck_exists===false ? '' : d.deck;
              $('#deck').dataset.auto = d.deck_exists===false ? '1' : '0'; }
  if(d.tool==='lvs' && d.source_path) $('#srcnet').value=d.source_path;
  // rule files carry no view; reuse the exact prior GDS (if it still exists) so the
  // rerun needs no view and reproduces the signed-off layout + netlist.
  if(d.layout_path && d.layout_exists){ $('#existinggds').value=d.layout_path;
    const det=$('#existinggds').closest('details'); if(det)det.open=true; }
  const shown=['tool','lib','cell','view'].filter(k=>d[k]).map(k=>k+'='+d[k]);
  if(d.deck) shown.push('deck='+d.deck.split('/').pop()+(d.deck_exists===false?'(missing→auto)':''));
  if(d.source_path) shown.push('src='+d.source_path.split('/').pop());
  if(d.layout_path&&d.layout_exists) shown.push('gds='+d.layout_path.split('/').pop());
  let msg='prefilled: '+(shown.join('  ')||'(nothing found)');
  if(d.lib_candidates)msg+='  &mdash; cell in multiple libs: '+d.lib_candidates.join(', ');
  if(d.notes&&d.notes.length)msg+='  ['+d.notes.join('; ')+']';
  $('#prefillmsg').innerHTML=esc0(msg);
  return d;
}
function esc0(s){return s;} // msg already safe-ish; keep simple
// After a prefill-from-runset, if the user hand-edits the cell, clear the stale
// GDS + source-netlist (they were for the prefilled cell) so the new cell streams
// and netlists its own. The runset template (LAST_RULEFILE) still applies.
$('#cell').addEventListener('input',()=>{
  if(LAST_RULEFILE && ($('#existinggds').value || $('#srcnet').value)){
    $('#existinggds').value=''; $('#srcnet').value='';
    $('#runmsg').innerHTML='cell changed &mdash; cleared the prefilled GDS + source netlist so '+
      '<b>'+esc($('#cell').value||'?')+'</b> streams &amp; netlists its own (the runset template still applies). '+
      'Set <b>lib/view</b> if streaming out.';
  }
});

// ---- existing runsets (reuse a prior DRC/LVS setup) ----
$('#rulerefresh').onclick=loadRuleFiles;
async function loadRuleFiles(){
  $('#rulemsg').innerHTML='<span class="spinner"></span>scanning launch dir &amp; run dirs for runsets&hellip;';
  let d;try{ d=await jgetT('/api/rulefiles',20000); }
  catch(e){ $('#rulemsg').innerHTML='<span class="pill warn">scan stalled</span>'; return; }
  const tb=$('#ruletable tbody');tb.innerHTML='';
  (d.rulefiles||[]).forEach(r=>{
    const tr=document.createElement('tr');
    tr.innerHTML='<td style="white-space:nowrap">'+esc(fmtRel(r.mtime))+'</td>'+
      '<td><span class="pill '+(r.tool==='lvs'?'warn':'good')+'">'+esc(r.tool.toUpperCase())+'</span></td>'+
      '<td>'+esc(r.cell)+'</td>'+
      '<td class="muted" style="font-size:12px">'+(r.deck?esc(r.deck.split('/').pop())+
        (r.deck_exists?'':' <span class="pill bad">missing</span>'):'&mdash;')+'</td>'+
      '<td>'+(r.layout_exists?'<span class="pill good">GDS</span>':'<span class="muted">no gds</span>')+'</td>'+
      '<td><button class="sec" style="padding:3px 10px">use</button></td>';
    tr.querySelector('button').onclick=()=>useRuleFile(r);
    tb.appendChild(tr);
  });
  $('#rulemsg').innerHTML=(d.rulefiles||[]).length
    ? (d.count+' runset(s) found near the launch directory'+(d.timed_out?' <span class="pill warn">scan capped</span>':''))
    : 'no existing runsets found near the launch directory (launch python where your *.drc.rule / _calibre.lvs_ live)';
  if(!(d.rulefiles||[]).length)tb.innerHTML='<tr><td colspan=6 class="muted">none found</td></tr>';
}
function useRuleFile(r){
  $$('input[name=tool]').forEach(x=>x.checked=(x.value===r.tool));
  $('#lvsonly').classList.toggle('hidden',r.tool!=='lvs');
  $('#cell').value=r.cell;
  if(r.layout_exists){ $('#existinggds').value=r.layout_path;
    const det=$('#existinggds').closest('details'); if(det)det.open=true; }
  $('#deck').value = r.deck_exists ? r.deck : '';
  $('#deck').dataset.auto = r.deck_exists ? '0' : '1';   // stale deck -> auto-latest
  if(r.tool==='lvs' && r.source_path) $('#srcnet').value=r.source_path;
  loadCells(); loadDecks();
  $('#runmsg').innerHTML='reusing runset for <b>'+esc(r.cell)+'</b> ('+r.tool.toUpperCase()+')'+
    (r.deck_exists?'':' &mdash; original deck missing, using latest deck')+' &mdash; click Run';
  document.getElementById('runbtn').scrollIntoView({behavior:'smooth',block:'center'});
}

// ---- recently edited layouts (suggested DRC/LVS) ----
function fmtRel(t){const s=(Date.now()/1000)-t;
  if(s<90)return 'just now'; if(s<3600)return Math.round(s/60)+'m ago';
  if(s<86400)return Math.round(s/3600)+'h ago'; return Math.round(s/86400)+'d ago';}
$('#recentrefresh').onclick=loadRecent;
async function loadRecent(){
  $('#recentmsg').innerHTML='<span class="spinner"></span>scanning your writable OA libraries&hellip;';
  let d;try{ d=await jgetT('/api/recent_layouts',25000); }
  catch(e){ $('#recentmsg').innerHTML='<span class="pill warn">scan stalled</span> (large libraries)'; return; }
  const tb=$('#recenttable tbody');tb.innerHTML='';
  (d.recent||[]).forEach((r,i)=>{
    const tr=document.createElement('tr');
    tr.innerHTML='<td style="white-space:nowrap">'+esc(fmtRel(r.mtime))+'</td>'+
      '<td>'+esc(r.lib)+'</td><td>'+esc(r.cell)+(i===0?' <span class="pill good">latest</span>':'')+'</td>'+
      '<td>'+esc(r.view)+'</td>'+
      '<td style="white-space:nowrap"><button class="sec" data-t="drc" style="padding:3px 10px">DRC</button> '+
      '<button class="sec" data-t="lvs" style="padding:3px 10px">LVS</button></td>';
    tr.querySelector('[data-t=drc]').onclick=()=>useDesign('drc',r);
    tr.querySelector('[data-t=lvs]').onclick=()=>useDesign('lvs',r);
    tb.appendChild(tr);
  });
  const top=(d.recent||[])[0];
  $('#recentmsg').innerHTML=top
    ? ('most recently edited: <b>'+esc(top.lib+' / '+top.cell+' / '+top.view)+'</b> ('+fmtRel(top.mtime)+')'+
       (d.timed_out?' &bull; <span class="pill warn">scan capped &mdash; showing newest found</span>':''))
    : 'no writable layout libraries found in cds.lib';
  if(!(d.recent||[]).length)tb.innerHTML='<tr><td colspan=5 class="muted">nothing found</td></tr>';
}
function useDesign(tool,r){
  $$('input[name=tool]').forEach(x=>x.checked=(x.value===tool));
  $('#lvsonly').classList.toggle('hidden',tool!=='lvs');
  $('#lib').value=r.lib; $('#cell').value=r.cell; $('#view').value=r.view;
  loadCells(); loadDecks();
  $('#runmsg').innerHTML='loaded <b>'+esc(r.lib+' / '+r.cell+' / '+r.view)+'</b> for '+tool.toUpperCase()+' &mdash; click Run';
  document.getElementById('runbtn').scrollIntoView({behavior:'smooth',block:'center'});
}

// ---- Easy: one-click do-it-all DRC ----
$('#easybtn').onclick=easyRun;
async function easyRun(){
  const b=$('#easybtn'), set=m=>$('#easymsg').innerHTML=m;
  b.disabled=true;
  try{
    // If a specific log/runset is staged in the Prefill box, GO reruns THAT
    // (prefill -> run the current form) instead of auto-picking the latest DRC.
    // This is what makes "paste _calibre.lvs_ -> Prefill -> GO" rerun that LVS.
    const staged=$('#prefillpath').value.trim();
    if(staged){
      set('<span class="spinner"></span>ensuring tools / loading modules&hellip;');
      let env=await jget('/api/envcheck');
      if(!env.ok){ const r=await jpost('/api/loadmodules',{}); env=r.status||await jget('/api/envcheck'); }
      renderEnvBanner(env);
      set('<span class="spinner"></span>prefilling from <b>'+esc(staged.split('/').pop())+'</b>&hellip;');
      const pf=await doPrefill();
      if(!pf||!pf.cell){ set('<span class="pill warn">could not read a design</span> from that path &mdash; is it a Calibre result / runset (e.g. _calibre.lvs_)?'); return; }
      set('<span class="spinner"></span>rerunning <b>'+esc((pf.tool||'').toUpperCase())+'</b> on <b>'+esc(pf.cell)+'</b>&hellip;');
      const ok=await runCurrentForm();
      set(ok
        ? 'rerunning <b>'+esc((pf.tool||'').toUpperCase())+'</b> on <b>'+esc(pf.cell)+'</b> from <b>'+esc(staged.split('/').pop())+'</b> &mdash; progress below &#8595;'
        : '<span class="pill warn">could not launch</span> &mdash; see the note under the Run button (a rule file carries no view: keep the existing-GDS box, or set a view).');
      return;
    }
    // 1) ensure tools on PATH (auto module load)
    set('<span class="spinner"></span>step 1/4 &mdash; checking environment / loading modules&hellip;');
    let env=await jget('/api/envcheck');
    if(!env.ok){ const r=await jpost('/api/loadmodules',{}); env=r.status||await jget('/api/envcheck'); }
    renderEnvBanner(env);
    // 2) find latest DRC log (bounded; never hang the button)
    set('<span class="spinner"></span>step 2/4 &mdash; searching for your most recent DRC log (max ~10s)&hellip;');
    let s;
    try{ s=await jgetT('/api/searchlogs?user='+encodeURIComponent($('#simuser').value.trim()),25000); }
    catch(err){
      await showDebugTail($('#easymsg'),
        '<span class="pill bad">log search stalled</span> a search root is slow or unreachable '+
        '(e.g. an NFS mount). Trim <b>sim_roots</b> in the Config tab, then retry. Debug log:');
      return;
    }
    if(s.timed_out){
      const slow=(s.roots||[]).filter(r=>r.timed_out).map(r=>r.root).join(', ');
      $('#easymsg').innerHTML='<span class="pill warn">search hit its time budget</span> slow root(s): <b>'+
        esc(slow)+'</b> &mdash; continuing with '+s.count+' logs found so far. '+
        '(Trim <b>sim_roots</b> in Config to speed this up.)';
    }
    // skip slow top-level designs (e.g. chipTop) when auto-picking
    let skip=[]; try{const c=await jget('/api/config');
      skip=(c.easy_skip_cells||'').split(/[,\s]+/).filter(Boolean).map(x=>x.toLowerCase());}catch(e){}
    const skipHit=n=>skip.some(x=>n.toLowerCase().indexOf(x)>=0);
    const isDrc=r=>r.type==='drc' && /\.drc\.(summary|results)$/.test(r.name);
    const isSum=r=>/\.drc\.summary$/.test(r.name);
    // prefer: newest non-skipped .drc.summary -> non-skipped .drc.* -> any (fallback)
    const drc=(s.results||[]).find(r=>isSum(r)&&!skipHit(r.name))
           || (s.results||[]).find(r=>isDrc(r)&&!skipHit(r.name))
           || (s.results||[]).find(r=>isSum(r))
           || (s.results||[]).find(r=>isDrc(r));
    if(drc && skip.length && skipHit(drc.name))
      set('<span class="pill warn">only top-level (skipped) DRC logs found</span> using <b>'+esc(drc.name)+'</b>&hellip;');
    if(!drc){
      await showDebugTail($('#easymsg'),
        '<span class="pill warn">no previous DRC log found</span> in your search roots'+
        (s.timed_out?' (search also timed out)':'')+'. Pick a design manually below and click Run, '+
        'or widen <b>sim_roots</b> in Config. Debug log:');
      return;
    }
    // 3) read design (lib/cell/view) from that log
    set('<span class="spinner"></span>step 3/4 &mdash; reading design from <b>'+esc(drc.name)+'</b>&hellip;');
    const pf=await jget('/api/prefill?path='+encodeURIComponent(drc.path));
    const cell=pf.cell, lib=pf.lib, view=pf.view||'layout';
    if(!cell){ set('could not determine the cell from '+esc(drc.name)+'; pick manually below.'); return; }
    // reflect into the form so the user sees what will run
    $$('input[name=tool]').forEach(x=>x.checked=(x.value==='drc'));
    $('#lvsonly').classList.add('hidden');
    if(lib){ $('#lib').value=lib; await loadCells(); }
    $('#cell').value=cell; $('#view').value=view; loadDecks();
    // 4) launch — use OA strmout when we know the lib, else the sibling GDS next to the log
    const body={tool:'drc', cell:cell, view:view, deck:'', src_net:''};
    if(lib){ body.lib=lib; body.existing_gds=''; }
    else   { body.lib='existingGDS'; body.existing_gds=drc.dir+'/'+cell+'.calibre.db'; }
    set('<span class="spinner"></span>step 4/4 &mdash; launching DRC on <b>'+esc(cell)+'</b>&hellip;');
    const d=await jpost('/api/run',body);
    if(d.error){ set('<span class="pill bad">ERROR</span> '+esc(d.error)); return; }
    set('running <b>DRC</b> on <b>'+esc(cell)+'</b> ('+esc(lib||'existing GDS')+') &mdash; progress below &#8595;');
    startRunUI();
    $('#livepanel').scrollIntoView({behavior:'smooth',block:'start'});
    if(pollTimer)clearInterval(pollTimer);
    pollTimer=setInterval(()=>pollJob(d.job_id),1200); pollJob(d.job_id);
  }catch(e){ $('#easymsg').innerHTML='<span class="pill bad">error</span> '+esc(''+e); }
  finally{ b.disabled=false; }
}

// ---- environment banner + auto module load ----
async function checkEnv(){
  const d=await jget('/api/envcheck');renderEnvBanner(d);return d;
}
function renderEnvBanner(d){
  const b=$('#envbanner');b.style.display='block';
  if(d.ok){
    b.style.borderColor='var(--good)';
    b.innerHTML='<span class="pill good">environment ready</span> '+
      '<span class="muted" style="font-size:12px">strmout: '+esc(d.strmout.path)+
      ' &nbsp;&bull;&nbsp; calibre: '+esc(d.calibre.path)+'</span>';
    return;
  }
  const miss=['strmout','calibre'].filter(t=>!d[t].found);
  b.style.borderColor='var(--bad)';
  b.innerHTML='<div><span class="pill bad">tools not on PATH</span> &nbsp;missing: <b>'+esc(miss.join(', '))+
    '</b> <span class="muted">&mdash; runs will auto <code>module load</code>, or load now:</span></div>'+
    '<div class="row" style="margin-top:8px">'+
      '<div style="flex:3"><label>Modules to load</label><input type="text" id="envmods" value="'+esc(d.modules||'')+'"></div>'+
      '<div style="flex:0 0 auto;display:flex;align-items:flex-end"><button id="loadmodbtn">Load modules &amp; recheck</button></div>'+
    '</div><div id="envmsg" class="muted" style="margin-top:6px"></div>';
  $('#loadmodbtn').onclick=async()=>{
    $('#envmsg').textContent='running module load ...';
    const r=await jpost('/api/loadmodules',{modules:$('#envmods').value});
    if(r.error){$('#envmsg').textContent='ERROR: '+r.error;return;}
    if(r.ok){$('#envmsg').textContent='loaded ('+r.applied_vars+' env vars updated) — environment now ready';
      renderEnvBanner(r.status);}
    else{
      const st=r.status||{};
      const still=['strmout','calibre'].filter(t=>st[t]&&!st[t].found);
      $('#envmsg').innerHTML='still missing after load: <b>'+esc(still.join(', '))+
        '</b><div class="muted" style="margin-top:4px">ran: <code>'+esc(r.cmd||'')+'</code> &bull; '+
        'module stdout '+(r.stdout_bytes||0)+' B, applied '+(r.applied_vars||0)+' vars</div>'+
        '<div style="margin-top:4px">module stderr:</div><pre>'+esc(r.stderr||'(empty)')+'</pre>'+
        (still.includes('strmout')&&!still.includes('calibre')
          ? '<div class="pill warn">strmout is a Cadence (Virtuoso) tool — add your Cadence module to the box above, e.g. <code>'+
            esc($('#envmods').value)+' cadence/IC618</code></div>' : '');
    }
  };
  const dl=document.createElement('div');dl.style.marginTop='8px';
  dl.innerHTML='<details><summary>Debug log</summary><pre id="dbgpre">loading...</pre></details>';
  $('#envbanner').appendChild(dl);
  dl.querySelector('summary').onclick=async()=>{
    const d=await jget('/api/debuglog');
    $('#dbgpre').textContent=(d.text||'(empty)')+'\n\n['+d.path+']';
  };
}

// ---- log search ----
$('#searchtoggle').onclick=()=>{
  const p=$('#searchpanel');p.classList.toggle('hidden');
  if(!p.classList.contains('hidden') && !$('#searchtable tbody').children.length)doSearch();
};
$('#searchbtn').onclick=doSearch;
function fmtTime(t){const d=new Date(t*1000);return d.toLocaleString();}
function fmtSize(n){return n>1e6?(n/1e6).toFixed(1)+'M':(n>1e3?(n/1e3).toFixed(0)+'k':n+'B');}
let SEARCH_ROWS=[], SEARCH_SORT={k:'mtime',dir:-1};
async function doSearch(){
  $('#searchmsg').innerHTML='<span class="spinner"></span>searching&hellip;';
  $('#searchbtn').disabled=true;
  const u=encodeURIComponent($('#simuser').value.trim());
  const x=encodeURIComponent($('#simextra').value.trim());
  let d;
  try{ d=await jgetT('/api/searchlogs?user='+u+'&extra='+x, 30000); }
  catch(err){ $('#searchbtn').disabled=false;
    await showDebugTail($('#searchmsg'),'<span class="pill bad">search stalled</span> a root is slow/unreachable &mdash; trim sim_roots in Config. Debug log:');
    return; }
  finally{ $('#searchbtn').disabled=false; }
  if($('#simuser').value.trim()==='' && d.user)$('#simuser').value=d.user;
  const found=(d.roots||[]).filter(r=>r.exists);
  const miss=(d.roots||[]).filter(r=>!r.exists).map(r=>r.root);
  $('#searchroots').innerHTML='searched (user=<b>'+esc(d.user)+'</b>): '+
    found.map(r=>'<span style="color:var(--good)">'+esc(r.root)+
      (r.discovered?' <span class="pill muted" style="padding:0 6px">auto</span>':'')+'</span>').join(' , ')+
    (miss.length?' &nbsp;| not present: '+miss.map(esc).join(' , '):'');
  $('#searchmsg').innerHTML=d.count+' log(s) found'+(d.truncated?' (truncated)':'')+
    (d.timed_out?' <span class="pill warn">timed out &mdash; partial; trim sim_roots</span>':'');
  SEARCH_ROWS=d.results||[];
  renderSearchRows();
}
function renderSearchRows(){
  const k=SEARCH_SORT.k,dir=SEARCH_SORT.dir;
  const rows=SEARCH_ROWS.slice().sort((a,b)=>{
    let x=a[k],y=b[k];
    if(typeof x==='string'){x=x.toLowerCase();y=(y||'').toLowerCase();}
    return (x<y?-1:x>y?1:0)*dir;
  });
  // header arrows
  $$('#searchtable th.sortable').forEach(th=>{
    th.querySelector('.arrow').textContent=(th.dataset.k===k)?(dir<0?' ▼':' ▲'):'';
  });
  const tb=$('#searchtable tbody');tb.innerHTML='';
  rows.forEach(r=>{
    const tr=document.createElement('tr');
    tr.innerHTML='<td style="white-space:nowrap">'+esc(fmtTime(r.mtime))+'</td>'+
      '<td><span class="pill muted">'+esc(r.type)+'</span></td>'+
      '<td style="font-size:12px;word-break:break-all">'+esc(r.path)+'</td>'+
      '<td style="white-space:nowrap">'+fmtSize(r.size)+'</td>'+
      '<td style="white-space:nowrap"><button class="sec" style="padding:3px 9px" data-a="pre">use</button> '+
      '<button class="sec" style="padding:3px 9px" data-a="view">view</button> '+
      '<button class="sec" style="padding:3px 9px" data-a="copy">copy</button></td>';
    tr.querySelector('[data-a=copy]').onclick=(e)=>copyText(r.path,e.target);
    tr.querySelector('[data-a=pre]').onclick=()=>{$('#prefillpath').value=r.path;$('#prefillbtn').click();
      $('#searchpanel').classList.add('hidden');};
    tr.querySelector('[data-a=view]').onclick=()=>{
      $$('.tab').forEach(x=>x.classList.remove('active'));
      document.querySelector('.tab[data-tab=history]').classList.add('active');
      $$('main>section').forEach(s=>s.classList.add('hidden'));
      $('#tab-history').classList.remove('hidden');viewResult(r.path);};
    tb.appendChild(tr);
  });
  if(!rows.length)tb.innerHTML='<tr><td colspan=5 class="muted">no logs found &mdash; adjust user or add a folder above</td></tr>';
}
$$('#searchtable th.sortable').forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;
  if(SEARCH_SORT.k===k)SEARCH_SORT.dir*=-1;
  else SEARCH_SORT={k:k,dir:(k==='mtime'||k==='size')?-1:1};
  renderSearchRows();
});

// ---- run ----
let pollTimer=null, RESULT_SCROLLED=false, FOLLOW_LOG=true;
function startRunUI(){ RESULT_SCROLLED=false; FOLLOW_LOG=true;
  $('#resultpanel').style.display='none'; $('#livepanel').style.display='block';
  const sb=$('#stopbtn'); sb.disabled=false; sb.textContent='■ Stop'; sb.style.display='inline-block'; }
// if the user scrolls up during a run, stop auto-following
window.addEventListener('wheel',()=>{FOLLOW_LOG=false;},{passive:true});
window.addEventListener('touchmove',()=>{FOLLOW_LOG=false;},{passive:true});
async function runCurrentForm(){
  const body={tool:currentTool(),lib:$('#lib').value,cell:$('#cell').value,view:$('#view').value,
    deck:$('#deck').value,src_net:$('#srcnet').value,src_view:$('#srcview').value,
    runset_src:LAST_RULEFILE,   // pass the prefilled runset so a failure prints a diff vs it
    existing_gds:$('#existinggds').value};
  if(!body.cell){$('#runmsg').textContent='pick a cell (or use an existing GDS)';return false;}
  if(!body.existing_gds && (!body.lib||!body.view)){$('#runmsg').textContent='pick lib/cell/view, or set an existing GDS';return false;}
  $('#runbtn').disabled=true;$('#runmsg').textContent='launching...';
  const d=await jpost('/api/run',body);
  $('#runbtn').disabled=false;
  if(d.error){$('#runmsg').textContent='ERROR: '+d.error;return false;}
  $('#runmsg').textContent='job '+d.job_id+'  ('+d.run_dir+')';
  startRunUI();
  $('#livepanel').scrollIntoView({behavior:'smooth',block:'start'});
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(()=>pollJob(d.job_id),1200);
  pollJob(d.job_id);
  return true;
}
$('#runbtn').onclick=runCurrentForm;
function fmtDur(s){s=Math.round(s||0);const m=Math.floor(s/60),ss=s%60;
  return m>0?(m+'m'+(ss<10?'0':'')+ss+'s'):(ss+'s');}
function renderProgress(d,st){
  const fill=$('#progfill'),txt=$('#progtext'),pct=$('#progpct');
  fill.classList.remove('indet','bad','good');
  const big='font-size:18px;font-weight:700';
  if(st==='done'){
    fill.classList.add('good');fill.style.width='100%';
    pct.innerHTML='<span style="color:var(--good)">&#10003; 100%</span>';
    txt.innerHTML='completed in <span style="'+big+';color:var(--good)">'+fmtDur(d.elapsed)+'</span>';
  }else if(st==='failed'){
    fill.classList.add('bad');fill.style.width='100%';
    pct.innerHTML='<span style="color:var(--bad)">&#10007;</span>';
    txt.innerHTML='failed after <span style="'+big+';color:var(--bad)">'+fmtDur(d.elapsed)+'</span>';
  }else if(d.progress!=null){
    fill.style.width=d.progress+'%';
    pct.innerHTML='<span style="color:var(--acc)">'+d.progress+'%</span>';
    txt.innerHTML='<span class="spinner"></span>elapsed <span style="'+big+'">'+fmtDur(d.elapsed)+'</span>'+
      (d.eta?(' &bull; <span style="'+big+';color:var(--acc)">~'+fmtDur(d.eta)+'</span> remaining'):'')+
      ' <span class="muted" style="font-size:12px">(est. from prior run)</span>';
  }else{
    fill.classList.add('indet');fill.style.width='35%';
    pct.innerHTML='<span class="spinner"></span>';
    txt.innerHTML='running&hellip; elapsed <span style="'+big+'">'+fmtDur(d.elapsed)+'</span>'+
      ' <span class="muted" style="font-size:12px">(no prior run for ETA)</span>';
  }
}
let CURRENT_JOB=null;
$('#stopbtn').onclick=async()=>{
  if(!CURRENT_JOB)return;
  $('#stopbtn').disabled=true;$('#stopbtn').textContent='stopping...';
  await jpost('/api/stop',{job_id:CURRENT_JOB});
};
async function pollJob(jid){
  CURRENT_JOB=jid;
  const d=await jget('/api/job?id='+encodeURIComponent(jid));
  if(d.error){$('#jobstate').textContent=d.error;return;}
  const st=d.state;
  const cls=st==='done'?'good':(st==='failed'?'bad':'warn');
  $('#jobstate').className='pill '+cls;$('#jobstate').textContent=st;
  $('#stopbtn').style.display=(st==='running'||st==='queued')?'inline-block':'none';
  renderProgress(d,st);
  $('#steps').innerHTML=(d.steps||[]).map((s,i)=>{
    const c=s.state==='done'?'good':(s.state==='failed'?'bad':'warn');
    const active=s.state==='running';
    // check box on the left: [ ] running, [x] done, [!] failed
    const box=(s.state==='done'?'<span style="color:var(--good);font-size:18px">&#9745;</span>'
              :s.state==='failed'?'<span style="color:var(--bad);font-size:18px">&#9746;</span>'
              :'<span style="font-size:18px;color:var(--muted)">&#9744;</span>');
    const style='display:flex;gap:10px;align-items:flex-start;margin:7px 0;padding:9px 12px;border-radius:6px;'+
      (active
        ? 'border:3px solid var(--acc);background:var(--acc-light);box-shadow:0 2px 8px rgba(0,82,204,.25);'
        : 'border:1px solid var(--line);background:var(--panel);');
    return '<div style="'+style+'">'+box+'<div style="flex:1">'+
      '<b style="font-size:15px">'+(i+1)+'.</b> '+
      (active?'<span class="spinner"></span>':'')+
      '<span class="pill '+c+'">'+esc(s.state)+'</span> '+
      '<b>'+esc(s.name)+'</b>'+(s.rc!=null?' <span class="muted">rc='+s.rc+'</span>':'')+
      '<br><code style="font-size:11px">'+esc(s.cmd)+'</code></div></div>';
  }).join('');
  $('#joblog').textContent=d.log||'';
  $('#joblog').scrollTop=$('#joblog').scrollHeight;       // tail the live log
  // show the Result in its own section once available; auto-scroll there once
  if(d.result || d.error){
    const rp=$('#resultpanel');rp.style.display='block';
    rp.style.borderColor = st==='failed' ? 'var(--bad)' : (st==='done' ? 'var(--good)' : 'var(--line)');
    let h='';
    if(d.result) h+=renderResult(d.result);
    if(d.error) h+='<div class="pill bad" style="margin-top:8px">'+esc(d.error)+'</div>';
    $('#resultbody').innerHTML=h;
    $('#resultpill').innerHTML=d.result?statusPill(d.result):'';
    $('#resultactions').style.display=(st==='done' && d.result && d.result.type!=='error')?'block':'none';
    if((st==='done'||st==='failed') && !RESULT_SCROLLED){
      RESULT_SCROLLED=true;rp.scrollIntoView({behavior:'smooth',block:'start'});
    }
  }
  if(st==='running' && FOLLOW_LOG){                       // auto-scroll to follow progress
    $('#livepanel').scrollIntoView({behavior:'smooth',block:'nearest'});
  }
  if(st==='done'||st==='failed'){clearInterval(pollTimer);pollTimer=null;}
}

function statusPill(r){
  if(r.type==='drc'){const ok=r.status==='CLEAN';
    return '<span class="pill '+(ok?'good':'bad')+'">'+esc(r.status)+'</span>';}
  if(r.type==='lvs'){const ok=r.status==='CORRECT';
    return '<span class="pill '+(ok?'good':(r.status==='INCORRECT'?'bad':'warn'))+'">'+esc(r.status)+'</span>';}
  return '<span class="pill muted">'+esc(r.status||r.type)+'</span>';
}
function renderResult(r){
  if(!r)return '';
  if(r.type==='error')return '<div class="pill bad">'+esc(r.error)+'</div>';
  const ver=r.version?esc(r.version.split(/\s+/)[0]):'';   // strip Calibre build date
  let h='<div style="margin-bottom:8px">'+statusPill(r)+' <b style="font-size:16px">'+esc(r.cell||'')+'</b></div>'+
        (r.date?('<div style="font-size:16px;font-weight:600;margin-bottom:6px">run: '+esc(r.date)+'</div>'):'')+
        (ver?('<div class="muted" style="font-size:12px;margin-bottom:6px">Calibre '+ver+'</div>'):'');
  if(r.type==='drc'){
    h+='<div class="kv"><div>Rules checked</div><div>'+r.total_rules+'</div>'+
       '<div>Rules violated</div><div>'+r.violated_rules+'</div>'+
       '<div>Total violations</div><div>'+r.total_violations+'</div></div>';
    const v=r.violations||{};
    const keys=Object.keys(v);
    if(keys.length){
      h+='<table style="margin-top:8px"><thead><tr><th>Rule</th><th>Count</th></tr></thead><tbody>';
      keys.forEach(k=>h+='<tr><td>'+esc(k)+'</td><td class="status-worse">'+v[k].count+'</td></tr>');
      h+='</tbody></table>';
    }
  }else if(r.type==='lvs'){
    const u=r.unmatched||{};
    h+='<div class="kv"><div>Total unmatched</div><div>'+(r.total_unmatched!=null?r.total_unmatched:'?')+'</div></div>';
    const ks=Object.keys(u);
    if(ks.length){
      h+='<table style="margin-top:8px"><thead><tr><th>Class</th><th>Layout match</th><th>Source match</th><th>Layout unmatched</th><th>Source unmatched</th></tr></thead><tbody>';
      ks.forEach(k=>{const x=u[k];h+='<tr><td>'+esc(k)+'</td><td>'+x.layout_matched+'</td><td>'+x.source_matched+'</td><td>'+x.layout_unmatched+'</td><td>'+x.source_unmatched+'</td></tr>';});
      h+='</tbody></table>';
    }
  }
  if(r.path)h+='<div class="muted" style="margin-top:6px">'+esc(r.path)+'</div>';
  return h;
}

// ---- history ----
async function loadRuns(){
  const d=await jget('/api/runs');const tb=$('#runstable tbody');tb.innerHTML='';
  (d.runs||[]).forEach(r=>{
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+esc(r.timestamp)+'</td><td>'+esc(r.tool)+'</td><td>'+esc(r.lib)+
      '</td><td>'+esc(r.cell)+'</td><td>'+esc(r.view)+'</td><td>'+esc(r.status)+
      '</td><td><button class="sec" style="padding:3px 9px">view</button></td>';
    tr.querySelector('button').onclick=()=>viewResult(r.result_file);
    tb.appendChild(tr);
  });
  if(!(d.runs||[]).length)tb.innerHTML='<tr><td colspan=7 class="muted">no runs yet</td></tr>';
}
$('#refreshruns').onclick=loadRuns;
async function viewResult(path){
  const r=await jget('/api/result?path='+encodeURIComponent(path));
  $('#histresult').style.display='block';
  let h=renderResult(r);
  h+='<details style="margin-top:10px"><summary>Raw report</summary><pre>'+esc(r.text||'')+'</pre></details>';
  $('#histresultbody').innerHTML=h;
  $('#histresult').scrollIntoView({behavior:'smooth'});
}

// ---- compare ----
async function loadCmpRuns(){
  const d=await jget('/api/runs');
  const runs=d.runs||[];              // newest-first
  ['#cmpRunA','#cmpRunB'].forEach(sel=>{
    const s=$(sel);s.innerHTML='<option value="">(none)</option>';
    runs.forEach(r=>{const o=document.createElement('option');
      o.value=r.result_file;o.textContent=r.timestamp+' '+r.tool+' '+r.cell+' ['+r.status+']';
      s.appendChild(o);});
  });
  // auto-fill: A = latest run, B = its nearest sibling (same tool+cell, else same tool)
  if(runs.length>=2 && !$('#cmpPathA').value && !$('#cmpPathB').value){
    const A=runs[0];
    let B=runs.slice(1).find(r=>r.tool===A.tool&&r.cell===A.cell)
         || runs.slice(1).find(r=>r.tool===A.tool)
         || runs[1];
    $('#cmpRunA').value=A.result_file; $('#cmpPathA').value=A.result_file;
    $('#cmpRunB').value=B.result_file; $('#cmpPathB').value=B.result_file;
    $('#cmpmsg').innerHTML='auto-filled: <b>'+esc(A.cell)+'</b> '+esc(A.timestamp)+
      ' vs '+esc(B.timestamp)+' &mdash; click Compare';
  }
}
// selecting a run in the dropdown mirrors its path into the (copyable) text box
$('#cmpRunA').addEventListener('change',()=>{if($('#cmpRunA').value)$('#cmpPathA').value=$('#cmpRunA').value;});
$('#cmpRunB').addEventListener('change',()=>{if($('#cmpRunB').value)$('#cmpPathB').value=$('#cmpRunB').value;});
// copy buttons (with fallback for non-secure localhost contexts)
function copyText(t,btn){
  const done=()=>{const o=btn.textContent;btn.textContent='copied!';setTimeout(()=>btn.textContent=o,1200);};
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done);}
  else{const ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);
    ta.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(ta);done();}
}
$$('button[data-copy]').forEach(b=>b.onclick=()=>copyText($('#'+b.dataset.copy).value,b));
// "Compare this run with previous" from the Result section
$('#result2compare').onclick=async()=>{
  $$('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelector('.tab[data-tab=compare]').classList.add('active');
  $$('main>section').forEach(s=>s.classList.add('hidden'));
  $('#tab-compare').classList.remove('hidden');
  $('#cmpPathA').value=''; $('#cmpPathB').value='';   // let auto-fill run
  await loadCmpRuns();                                 // A=latest run, B=sibling
  $('#cmpbtn').click();
};

// Easy compare: pick the 2 most recent parseable result logs, label by parsed
// cell name (add date/time when both are the same cell), fill A/B, and compare.
$('#cmpeasybtn').onclick=async()=>{
  $('#cmpmsg').innerHTML='<span class="spinner"></span>finding two most recent result logs&hellip;';
  const s=await jget('/api/searchlogs?user='+encodeURIComponent(($('#simuser')?$('#simuser').value.trim():'')));
  const cand=(s.results||[]).filter(r=>/\.drc\.summary$/.test(r.name)||/\.lvs\.report$/.test(r.name));
  if(cand.length<2){$('#cmpmsg').innerHTML='<span class="pill warn">need at least 2 result logs</span>';return;}
  const A=cand[0], B=cand[1];
  const [pa,pb]=await Promise.all([
    jget('/api/prefill?path='+encodeURIComponent(A.path)),
    jget('/api/prefill?path='+encodeURIComponent(B.path))]);
  const ca=pa.cell||A.name, cb=pb.cell||B.name;
  $('#cmpPathA').value=A.path; $('#cmpPathB').value=B.path;
  $('#cmpRunA').value=''; $('#cmpRunB').value='';
  // label by cell; if same cell, distinguish by date/time
  let la='A = <b>'+esc(ca)+'</b>', lb='B = <b>'+esc(cb)+'</b>';
  if(ca===cb){ la+=' ('+esc(fmtTime(A.mtime))+')'; lb+=' ('+esc(fmtTime(B.mtime))+')'; }
  else { la+=' <span class="muted">('+esc(fmtDate(A.mtime))+')</span>';
         lb+=' <span class="muted">('+esc(fmtDate(B.mtime))+')</span>'; }
  $('#cmpmsg').innerHTML=la+' &nbsp;vs&nbsp; '+lb+' &mdash; comparing&hellip;';
  $('#cmpbtn').click();
};
$('#cmpbtn').onclick=async()=>{
  const a=$('#cmpPathA').value.trim()||$('#cmpRunA').value;
  const b=$('#cmpPathB').value.trim()||$('#cmpRunB').value;
  if(!a||!b){$('#cmpmsg').textContent='choose/enter both A and B';return;}
  $('#cmpmsg').textContent='comparing...';
  const d=await jpost('/api/compare',{a,b});
  $('#cmpmsg').textContent='';
  $('#cmpout').innerHTML=renderCompare(d);
};
function cmpLabel(side,d){
  const cell=(d[side]&&d[side].cell)||'?';
  const mt=d[side+'_mtime']||0;
  const other=(side==='a')?(d.b_mtime||0):(d.a_mtime||0);
  const when=mt?new Date(mt*1000).toLocaleString():'';
  const latest=mt && mt>=other;
  return {cell:cell, when:when, latest:latest,
    tag:(side==='a'?'A':'B')+' = '+esc(cell)+(when?(' ('+(latest?'latest, ':'')+esc(when)+')'):''),
    short:esc(cell)+(when?('<br><span class="muted" style="font-weight:400;font-size:11px">'+
          (latest?'latest, ':'')+esc(when)+'</span>'):'')};
}
function renderCompare(d){
  if(d.error)return '<div class="panel"><span class="pill bad">'+esc(d.error)+'</span></div>';
  const laA=cmpLabel('a',d), laB=cmpLabel('b',d);
  let h='<div class="panel"><h2>Summary</h2><div class="row">'+
    '<div><b>'+laA.tag+'</b> '+statusPill(d.a)+'<div class="muted" style="font-size:11px">'+esc(d.a_path)+'</div></div>'+
    '<div><b>'+laB.tag+'</b> '+statusPill(d.b)+'<div class="muted" style="font-size:11px">'+esc(d.b_path)+'</div></div></div></div>';
  if(d.drc_diff){
    const dd=d.drc_diff;
    const changed=(dd.rows||[]).filter(r=>r.status!=='same');
    const identical=changed.length===0;
    h+='<div class="panel" style="border:2px solid '+(identical?'var(--good)':'var(--bad)')+'">'+
       '<h2>DRC rule diff &nbsp;<span class="muted">total '+laA.tag+'='+dd.total_a+' &rarr; '+laB.tag+'='+dd.total_b+'</span></h2>';
    h+='<div style="margin-bottom:10px">'+(identical
      ? '<span class="pill good" style="font-size:14px;padding:6px 16px">&#10003; MATCH &mdash; identical DRC results</span>'
      : '<span class="pill bad" style="font-size:14px;padding:6px 16px">&#10007; DIFFERENT &mdash; '+changed.length+' rule(s) changed</span>')+'</div>';
    if(!dd.rows.length){h+='<div class="muted">no violations in either run</div>';}
    else{h+='<table><thead><tr><th>Rule</th><th>A<br><span class="muted" style="font-weight:400;font-size:11px">'+laA.short+'</span></th>'+
      '<th>B<br><span class="muted" style="font-weight:400;font-size:11px">'+laB.short+'</span></th><th>&Delta;</th><th>Status</th></tr></thead><tbody>';
      const BG={improved:'rgba(0,135,90,.10)',worse:'rgba(222,53,11,.10)',only_a:'rgba(255,139,0,.10)',only_b:'rgba(0,82,204,.08)'};
      dd.rows.forEach(r=>{h+='<tr style="background:'+(BG[r.status]||'')+'"><td>'+esc(r.rule)+'</td><td>'+(r.a==null?'&mdash;':r.a)+
        '</td><td>'+(r.b==null?'&mdash;':r.b)+'</td><td>'+(r.delta>0?'+':'')+r.delta+
        '</td><td class="status-'+r.status+'">'+r.status+'</td></tr>';});
      h+='</tbody></table>';}
    h+='</div>';
  }
  if(d.lvs_diff){
    const l=d.lvs_diff;
    h+='<div class="panel"><h2>LVS diff</h2><div class="kv">'+
      '<div>Status &mdash; '+laA.tag+'</div><div>'+esc(l.status_a)+'</div>'+
      '<div>Status &mdash; '+laB.tag+'</div><div>'+esc(l.status_b)+'</div>'+
      '<div>Unmatched '+laA.tag+'</div><div>'+l.unmatched_a+'</div>'+
      '<div>Unmatched '+laB.tag+'</div><div>'+l.unmatched_b+'</div>'+
      '<div>Changed</div><div>'+(l.changed?'<span class="pill warn">YES</span>':'<span class="pill good">no</span>')+'</div>'+
      '</div></div>';
  }
  if(d.text_diff!=null){
    const lines=d.text_diff.split('\n').map(ln=>{
      let c='';if(ln.startsWith('+')&&!ln.startsWith('+++'))c='diff-add';
      else if(ln.startsWith('-')&&!ln.startsWith('---'))c='diff-del';
      else if(ln.startsWith('@@')||ln.startsWith('+++')||ln.startsWith('---'))c='diff-hdr';
      return '<span class="'+c+'">'+esc(ln)+'</span>';
    }).join('\n');
    h+='<div class="panel"><h2>Raw text diff'+(d.text_diff_truncated?' <span class="muted">(truncated)</span>':'')+
       '</h2><pre>'+(lines||'<span class="muted">identical</span>')+'</pre></div>';
  }
  return h;
}

// ---- config ----
const CFG_LABELS={
  calibre_bin:'calibre executable',strmout_bin:'strmout executable',cds_lib:'cds.lib path',
  techlib:'tech library (strmout -techLib)',layermap:'GDS layer map',
  drc_deck:'DRC rule deck (fallback)',drc_antenna_deck:'DRC antenna deck',lvs_deck:'LVS rule deck (fallback)',
  drc_deck_glob:'DRC deck glob (newest auto-picked)',lvs_deck_glob:'LVS deck glob (newest auto-picked)',
  strmout_cmd:'strmout command template',drc_cmd:'DRC command template',lvs_cmd:'LVS command template',
  netlist_mode:'LVS source-netlist generator (si | skill | custom | off)',
  netlist_view:'schematic view to netlist for LVS source (default schematic)',
  si_bin:'si executable (auCDL netlister, ships with Virtuoso)',
  virtuoso_bin:'virtuoso executable (for skill netlist mode)',
  si_cmd:'si auCDL command template',
  netlist_skill_cmd:'virtuoso -nograph SKILL command template',
  netlist_cmd:'custom source-netlist command template (wins if set)',
  modules:'modules to auto-load if tools missing',
  module_load_cmd:'module-load command template ({modules})',
  auto_load_modules:'auto module-load on run (yes/no)',
  discover_run_dirs:'auto-find run dirs from Calibre runsets (yes/no)',
  easy_skip_cells:'Easy: skip these designs when auto-picking (e.g. chipTop)',
  sim_user:'sim user for log search (blank = login name)',
  sim_roots:'log-search roots ({user} expands)',
  drc_extra_svrf:'extra DRC SVRF lines',lvs_extra_svrf:'extra LVS SVRF lines',
  lvs_runset_template:'known-good LVS runset reused as a template for every cell (e.g. a TVF _calibre.lvs_)'};
async function loadConfig(){
  const c=await jget('/api/config');const box=$('#cfgfields');box.innerHTML='';
  Object.keys(CFG_LABELS).forEach(k=>{
    const big=k.endsWith('_cmd')||k.endsWith('_svrf')||k==='sim_roots';
    const wrap=document.createElement('div');
    wrap.innerHTML='<label>'+esc(CFG_LABELS[k])+' <code>'+k+'</code></label>'+
      (big?'<textarea rows="2" id="cfg_'+k+'"></textarea>':'<input type="text" id="cfg_'+k+'">');
    box.appendChild(wrap);
    $('#cfg_'+k).value=c[k]||'';
  });
}
$('#savecfg').onclick=async()=>{
  const body={};Object.keys(CFG_LABELS).forEach(k=>body[k]=$('#cfg_'+k).value);
  const d=await jpost('/api/config',body);
  $('#cfgmsg').textContent=d.ok?'saved':'error';
  if(d.ok){loadLibs();}
  setTimeout(()=>$('#cfgmsg').textContent='',2000);
};

// init
attachCombo('lib','liblist'); attachCombo('cell','celllist'); attachCombo('view','viewlist');
loadLibs();
checkEnv();
loadDecks();
loadRuleFiles();
loadRecent();
initStartup();
async function initStartup(){          // --log paths passed on the command line
  let d;try{ d=await jget('/api/startup'); }catch(e){ return; }
  const logs=(d&&d.logs)||[];
  if(!logs.length)return;
  $('#prefillpath').value=logs[0];
  $('#prefillbtn').click();            // prefill Run tab from the first log
  if(logs.length>=2){                  // preset Compare with the first two
    $('#cmpPathA').value=logs[0]; $('#cmpPathB').value=logs[1];
    $('#prefillmsg').innerHTML+=' &bull; 2 logs on command line &mdash; Compare tab preset';
  }
}
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def _force_utf8_console():
    """Make stdout/stderr tolerate non-ASCII (Calibre output under a C/POSIX
    locale would otherwise raise UnicodeEncodeError writing the banners/logs)."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # Python 3.7+
        except Exception:
            try:
                import io
                setattr(sys, stream_name,
                        io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                         errors="replace", line_buffering=True))
            except Exception:
                pass


def main():
    global CONFIG, CONFIG_PATH, RUNS_BASE
    _force_utf8_console()
    ap = argparse.ArgumentParser(description="Calibre DRC/LVS browser GUI")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--base", default=os.path.abspath("./calibre_runs"),
                    help="base dir for run outputs")
    ap.add_argument("--config", default=os.path.abspath("./calibre_gui_config.json"))
    ap.add_argument("--open", action="store_true", help="open a browser")
    ap.add_argument("--log", action="append", metavar="PATH", default=[],
                    help="existing result log (.drc.summary/.lvs.report/...) to "
                         "prefill on startup; repeat once more to preset a compare")
    args = ap.parse_args()

    global STARTUP_LOGS
    STARTUP_LOGS = [os.path.abspath(os.path.expanduser(p)) for p in (args.log or [])]

    CONFIG_PATH = os.path.abspath(args.config)
    CONFIG = load_config(CONFIG_PATH)
    if not os.path.isfile(CONFIG_PATH):
        save_config(CONFIG_PATH, CONFIG)  # materialize defaults for editing
    RUNS_BASE = os.path.abspath(args.base)
    os.makedirs(RUNS_BASE, exist_ok=True)

    # Bind, auto-hopping to a free port if the preferred one is taken.
    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError as e:
            if getattr(e, "errno", None) in (98, 48):        # EADDRINUSE (Linux/macOS)
                continue
            sys.stderr.write(
                "\nERROR: could not bind %s:%d -> %s\n"
                "  - if you passed a hostname, use --host 127.0.0.1\n"
                "  - for a permission error, pick a port above 1024 (--port 9100)\n\n"
                % (args.host, port, e))
            raise SystemExit(1)
    if httpd is None:
        sys.stderr.write(
            "\nERROR: ports %d-%d on %s are all in use.\n"
            "  A previous instance is probably still running. Either:\n"
            "    - just open the URL that instance already printed, or\n"
            "    - stop it:   pkill -f calibre_drc_lvs_gui.py\n"
            "    - or use another port:   python3 %s --port 9100 --open\n\n"
            % (args.port, args.port + 19, args.host, os.path.basename(sys.argv[0])))
        raise SystemExit(1)
    actual_port = httpd.server_address[1]
    if actual_port != args.port:
        print("(port %d busy -> using %d)" % (args.port, actual_port))
    url = "http://%s:%d/" % (args.host, actual_port)
    print("=" * 66)
    print(" Calibre DRC/LVS GUI")
    print("   URL       : %s" % url)
    print("   runs dir  : %s" % RUNS_BASE)
    print("   config    : %s" % CONFIG_PATH)
    print("   cds.lib   : %s" % (resolve_cds_lib() or "(none found -- set cds_lib or launch from your project dir)"))
    if STARTUP_LOGS:
        print("   --log     : %s" % ", ".join(STARTUP_LOGS))
    print("   NOTE: Calibre/strmout inherit THIS shell's environment.")
    print("   Ctrl-C to stop.")
    print("=" * 66)
    sys.stdout.flush()
    if args.open:
        if not os.environ.get("DISPLAY"):
            print("   (--open: no DISPLAY on this host -- open the URL above yourself,\n"
                  "    e.g. from your laptop after:  ssh -L %d:127.0.0.1:%d you@host)"
                  % (actual_port, actual_port))
        else:
            # Launch the browser with its stderr sent to /dev/null so a broken/
            # remote browser (XPCOMGlueLoad, deprecated xdg-open, etc.) can't spam
            # this console. If it doesn't appear, the URL above still works.
            try:
                import webbrowser
                devnull = os.open(os.devnull, os.O_WRONLY)
                saved = os.dup(2)
                os.dup2(devnull, 2)
                try:
                    webbrowser.open(url)
                finally:
                    os.dup2(saved, 2)
                    os.close(devnull)
                    os.close(saved)
            except Exception:
                pass
            print("   (if no browser opened, just paste the URL above into one)")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
