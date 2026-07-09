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

import argparse
import difflib
import fnmatch
import getpass
import glob as globmod
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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

    # Optional: command to generate the LVS source netlist from schematic.
    # Leave blank -> the GUI expects an existing source netlist file instead.
    "netlist_cmd": "",

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
}

CONFIG_LOCK = threading.Lock()
CONFIG = {}
CONFIG_PATH = None
RUNS_BASE = None


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
                elif key == "INCLUDE" and len(parts) >= 2:
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

    # If lib unknown but we have a cell, look it up by scanning cds.lib libraries.
    if out["cell"] and not out["lib"]:
        matches = []
        try:
            libs = parse_cds_lib(CONFIG["cds_lib"])
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
        self._lock = threading.Lock()

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
                with open(self.log_path, "r", errors="replace") as f:
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
    with open(job.log_path, "a") as f:
        f.write(msg)


def _run_step(job, name, cmd_list, cwd):
    with job._lock:
        step = {"name": name, "cmd": " ".join(shlex.quote(c) for c in cmd_list),
                "rc": None, "state": "running"}
        job.steps.append(step)
    _log(job, "\n" + "=" * 78 + "\n")
    _log(job, "### STEP: %s\n### CMD : %s\n### CWD : %s\n" %
         (name, step["cmd"], cwd))
    _log(job, "=" * 78 + "\n")
    try:
        proc = subprocess.Popen(cmd_list, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1,
                                env=os.environ)  # inherit the launching shell's env
    except FileNotFoundError as e:
        _log(job, "!! executable not found: %s\n" % e)
        step["state"] = "failed"
        step["rc"] = 127
        return 127
    with open(job.log_path, "a") as lf:
        for line in iter(proc.stdout.readline, ""):
            lf.write(line)
            lf.flush()
        proc.stdout.close()
    rc = proc.wait()
    step["rc"] = rc
    step["state"] = "done" if rc == 0 else "failed"
    _log(job, "\n### STEP %s finished rc=%d\n" % (name, rc))
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


def search_logs(user=None, extra=None, max_results=800, max_depth=5):
    """Search /sim/<user> and other common simulation roots for Calibre logs."""
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
    # de-dupe, keep order
    seen, uroots = set(), []
    for r in roots:
        ap = os.path.abspath(r)
        if ap not in seen:
            seen.add(ap)
            uroots.append(ap)

    results, scanned = [], []
    for root in uroots:
        exists = os.path.isdir(root)
        scanned.append({"root": root, "exists": exists})
        if not exists:
            continue
        base = root.rstrip("/").count("/")
        for dirpath, dirs, files in os.walk(root):
            if dirpath.rstrip("/").count("/") - base >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("svdb", "__pycache__", ".git")]
            for fn in files:
                if any(fnmatch.fnmatch(fn, p) for p in LOG_PATTERNS):
                    p = os.path.join(dirpath, fn)
                    try:
                        stt = os.stat(p)
                    except OSError:
                        continue
                    results.append({"path": p, "name": fn, "dir": dirpath,
                                    "size": stt.st_size, "mtime": stt.st_mtime,
                                    "type": _guess_log_type(fn)})
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    results.sort(key=lambda r: r["mtime"], reverse=True)
    return {"user": user, "login": getpass.getuser(), "roots": scanned,
            "count": len(results), "truncated": len(results) >= max_results,
            "results": results}


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


def _fill(template, mapping):
    return shlex.split(template.format(**mapping))


# --------------------------------------------------------------------------- #
#  Environment / module auto-loading
# --------------------------------------------------------------------------- #

def _dbg(msg):
    """Append a timestamped line to the on-disk GUI debug log."""
    try:
        line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        sys.stderr.write(line)
        if RUNS_BASE:
            with open(os.path.join(RUNS_BASE, "gui_debug.log"), "a") as f:
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
    _log(job, "\n" + "=" * 78 + "\n### STEP: module load %s  (auto: tools missing: %s)\n"
         % (mods, ", ".join(missing)) + "=" * 78 + "\n")
    res = load_modules(mods)
    _log(job, "module load applied %s env vars\n" % res.get("applied_vars"))
    if res.get("stderr"):
        _log(job, "module stderr:\n%s\n" % res["stderr"])
    still = [t for t in tools if not shutil.which(_bin_for(cfg, t))]
    step["state"] = "done" if not still else "failed"
    step["rc"] = 0 if not still else 1
    if still:
        raise RuntimeError(
            "after 'module load %s', still not found: %s. Check the module name, "
            "or set absolute paths (strmout_bin/calibre_bin) in Config." %
            (mods, ", ".join(still)))
    _log(job, "### tools resolved: %s\n" %
         ", ".join("%s=%s" % (t, shutil.which(_bin_for(cfg, t))) for t in tools))


def run_job(job):
    cfg = job.meta["cfg_snapshot"]
    run_dir = job.meta["run_dir"]
    tool = job.meta["tool"]
    lib = job.meta["lib"]
    cell = job.meta["cell"]
    view = job.meta["view"]
    job.state = "running"
    try:
        # baseline for the progress bar: newest comparable prior run's log size
        job.meta["baseline_bytes"] = _find_baseline_bytes(job.meta)

        # 0. make sure the EDA tools are on PATH (auto `module load` if needed).
        needed = ["calibre"] + ([] if job.meta.get("existing_gds") else ["strmout"])
        _ensure_tools(job, cfg, needed)

        gds = "%s.calibre.db" % cell
        gds_abs = os.path.join(run_dir, gds)

        # --- 1. stream out GDS from OA (unless user supplied an existing GDS) ---
        if job.meta.get("existing_gds"):
            src = os.path.abspath(os.path.expanduser(job.meta["existing_gds"]))
            _log(job, "Using existing GDS: %s\n" % src)
            if not os.path.isfile(src):
                raise RuntimeError("existing GDS not found: %s" % src)
            gds_abs = src
            gds = src
        else:
            # give strmout a cds.lib in the run dir that INCLUDEs the configured one
            local_cds = os.path.join(run_dir, "cds.lib")
            with open(local_cds, "w") as f:
                f.write('INCLUDE "%s"\n' % os.path.abspath(os.path.expanduser(cfg["cds_lib"])))
            mapping = {
                "strmout_bin": cfg["strmout_bin"], "lib": lib, "cell": cell,
                "view": view, "gds": gds, "strmlog": "strmout_%s.log" % cell,
                "techlib": cfg["techlib"], "layermap": cfg["layermap"],
            }
            cmd = _fill(cfg["strmout_cmd"], mapping)
            rc = _run_step(job, "strmout (OA->GDS)", cmd, run_dir)
            if rc != 0:
                raise RuntimeError("strmout failed (rc=%d) -- see log" % rc)
            if not os.path.isfile(gds_abs):
                raise RuntimeError("strmout reported ok but %s not found" % gds)

        # --- 2. optional source-netlist generation for LVS ---
        src_net = None
        if tool == "lvs":
            src_net = job.meta.get("src_net") or "%s.src.net" % cell
            src_net_abs = src_net if os.path.isabs(src_net) else os.path.join(run_dir, src_net)
            if not os.path.isfile(src_net_abs) and cfg.get("netlist_cmd", "").strip():
                mapping = {"cell": cell, "lib": lib, "view": view,
                           "src_net": src_net, "calibre_bin": cfg["calibre_bin"]}
                cmd = _fill(cfg["netlist_cmd"], mapping)
                rc = _run_step(job, "generate source netlist", cmd, run_dir)
                if rc != 0:
                    raise RuntimeError("netlist generation failed (rc=%d)" % rc)
            if not os.path.isfile(src_net_abs):
                raise RuntimeError(
                    "LVS source netlist not found: %s\n"
                    "Provide a path to an existing .src.net/.sp, or set a "
                    "'netlist_cmd' in Config." % src_net_abs)
            src_net = src_net_abs

        # --- 3. write runset + launch calibre ---
        if tool == "drc":
            deck = job.meta.get("deck") or latest_deck("drc") or cfg["drc_deck"]
            runfile = _write_drc_runset(run_dir, cell, gds, deck, cfg.get("drc_extra_svrf", ""))
            cmd = _fill(cfg["drc_cmd"], {"calibre_bin": cfg["calibre_bin"],
                                        "runfile": os.path.basename(runfile)})
            rc = _run_step(job, "calibre DRC", cmd, run_dir)
            result_file = os.path.join(run_dir, "%s.drc.summary" % cell)
        else:
            deck = job.meta.get("deck") or latest_deck("lvs") or cfg["lvs_deck"]
            runfile = _write_lvs_runset(run_dir, cell, gds, src_net, deck,
                                        cfg.get("lvs_extra_svrf", ""))
            cmd = _fill(cfg["lvs_cmd"], {"calibre_bin": cfg["calibre_bin"],
                                        "spiceout": "%s.sp" % cell,
                                        "runfile": os.path.basename(runfile)})
            rc = _run_step(job, "calibre LVS", cmd, run_dir)
            result_file = os.path.join(run_dir, "%s.lvs.report" % cell)

        # --- 4. parse result ---
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
    except Exception as e:
        job.error = str(e)
        job.state = "failed"
        _log(job, "\n!!! JOB FAILED: %s\n%s\n" % (e, traceback.format_exc()))
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

    # ---- helpers ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            if path == "/" or path == "/index.html":
                return self._send_html(INDEX_HTML)
            if path == "/api/config":
                with CONFIG_LOCK:
                    return self._send_json(dict(CONFIG))
            if path == "/api/libs":
                libs = parse_cds_lib(CONFIG["cds_lib"])
                items = [{"name": n, "path": p, "exists": os.path.isdir(p)}
                         for n, p in sorted(libs.items())]
                return self._send_json({"cds_lib": CONFIG["cds_lib"], "libs": items})
            if path == "/api/cells":
                lib = q.get("lib", [""])[0]
                libs = parse_cds_lib(CONFIG["cds_lib"])
                if lib not in libs:
                    return self._send_json({"error": "unknown lib %r" % lib}, 400)
                return self._send_json({"cells": list_cells(libs[lib])})
            if path == "/api/views":
                lib = q.get("lib", [""])[0]
                cell = q.get("cell", [""])[0]
                libs = parse_cds_lib(CONFIG["cds_lib"])
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
            if path == "/api/debuglog":
                dl = os.path.join(RUNS_BASE, "gui_debug.log")
                txt = ""
                if os.path.isfile(dl):
                    with open(dl, "r", errors="replace") as f:
                        txt = f.read()[-20000:]
                return self._send_json({"path": dl, "text": txt})
            if path == "/api/decks":
                return self._send_json(list_decks(q.get("kind", ["drc"])[0]))
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
        except Exception as e:
            return self._send_json({"error": str(e),
                                    "trace": traceback.format_exc()}, 500)

    def _handle_run(self, body):
        tool = body.get("tool")
        lib = body.get("lib")
        cell = body.get("cell")
        view = body.get("view")
        if tool not in ("drc", "lvs") or not (lib and cell and view):
            return self._send_json({"error": "need tool(drc|lvs), lib, cell, view"}, 400)
        with CONFIG_LOCK:
            cfg_snap = dict(CONFIG)
        meta = {
            "tool": tool, "lib": lib, "cell": cell, "view": view,
            "deck": body.get("deck", "").strip() or None,
            "src_net": body.get("src_net", "").strip() or None,
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
 /* big bright call-to-action button */
 .bigbtn{font-size:17px;padding:15px 34px;font-weight:700;border-radius:9px;letter-spacing:.02em;
         color:#fff;border:0;cursor:pointer;
         background:linear-gradient(135deg,#12c99b 0%,#0a8f6f 100%);
         box-shadow:0 3px 10px rgba(10,143,111,.38)}
 .bigbtn:hover{filter:brightness(1.08);box-shadow:0 4px 14px rgba(10,143,111,.5)}
 .bigbtn:disabled{opacity:.55;cursor:default;filter:none}
</style></head>
<body>
<header>
  <h1>Calibre&nbsp;DRC&nbsp;/&nbsp;LVS</h1>
  <span class="sub">runs in your launching shell &mdash; env inherited &bull; <span id="cdslabel"></span></span>
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
      <button id="easybtn" class="bigbtn">&#9889;&nbsp;Easy DRC &mdash; one click</button>
      <span class="muted" style="font-size:12px;max-width:640px">
        Loads the required modules &rarr; finds your most recent DRC log &rarr; re-runs DRC on that
        design with the latest rule deck &rarr; shows a live progress bar with %% and ETA below.</span>
    </div>
    <div id="easymsg" class="muted" style="margin-top:8px"></div>
  </div>

  <div id="envbanner" class="panel" style="display:none"></div>

  <div class="panel">
    <h2>Prefill from a previous log</h2>
    <div class="row">
      <div style="flex:3">
        <label>Path to a .log / .drc.summary / .lvs.report / runset &mdash; fills tool, lib, cell, view</label>
        <input type="text" id="prefillpath" placeholder="/path/to/cell.drc.summary  or  a runset  or  strmout.log">
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
          <th class="sortable" data-k="name">File <span class="arrow"></span></th>
          <th class="sortable" data-k="size">Size <span class="arrow"></span></th>
          <th class="sortable" data-k="dir">Folder <span class="arrow"></span></th>
          <th></th>
        </tr></thead><tbody></tbody></table>
      </div>
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
    <div id="lvsonly" class="row hidden">
      <div>
        <label>LVS source netlist (.src.net / .sp) &mdash; path, or blank for &lt;cell&gt;.src.net in run dir</label>
        <input type="text" id="srcnet" placeholder="(cell).src.net">
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
    <h2>Run status &nbsp;<span id="jobstate" class="pill muted"></span></h2>
    <div id="progwrap" style="margin:2px 0 14px">
      <div class="progbar"><div id="progfill" class="progfill"></div></div>
      <div id="progtext" class="muted" style="font-size:12px;margin-top:5px"></div>
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
      <button class="bigbtn" id="cmpeasybtn">&#9889;&nbsp;Easy compare &mdash; 2 latest logs</button>
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
    <div class="flexbtn"><button id="cmpbtn">Compare</button></div>
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
$('#prefillbtn').onclick=async()=>{
  const p=$('#prefillpath').value.trim();
  if(!p){$('#prefillmsg').textContent='enter a path';return;}
  $('#prefillmsg').textContent='reading...';
  const d=await jget('/api/prefill?path='+encodeURIComponent(p));
  if(d.tool){$$('input[name=tool]').forEach(r=>r.checked=(r.value===d.tool));
    $('#lvsonly').classList.toggle('hidden',d.tool!=='lvs');}
  if(d.lib)$('#lib').value=d.lib;
  if(d.lib)await loadCells();
  if(d.cell)$('#cell').value=d.cell;
  if(d.cell)await loadViews();
  if(d.view)$('#view').value=d.view;
  const got=['tool','lib','cell','view'].filter(k=>d[k]).map(k=>k+'='+d[k]).join('  ');
  let msg='prefilled: '+(got||'(nothing found)');
  if(d.lib_candidates)msg+='  &mdash; cell in multiple libs: '+d.lib_candidates.join(', ');
  if(d.notes&&d.notes.length)msg+='  ['+d.notes.join('; ')+']';
  $('#prefillmsg').innerHTML=esc0(msg);
};
function esc0(s){return s;} // msg already safe-ish; keep simple

// ---- Easy: one-click do-it-all DRC ----
$('#easybtn').onclick=easyRun;
async function easyRun(){
  const b=$('#easybtn'), set=m=>$('#easymsg').innerHTML=m;
  b.disabled=true;
  try{
    // 1) ensure tools on PATH (auto module load)
    set('<span class="spinner"></span>step 1/4 &mdash; checking environment / loading modules&hellip;');
    let env=await jget('/api/envcheck');
    if(!env.ok){ const r=await jpost('/api/loadmodules',{}); env=r.status||await jget('/api/envcheck'); }
    renderEnvBanner(env);
    // 2) find latest DRC log
    set('<span class="spinner"></span>step 2/4 &mdash; finding your most recent DRC log&hellip;');
    const s=await jget('/api/searchlogs?user='+encodeURIComponent($('#simuser').value.trim()));
    // prefer a .drc.summary (has clean cell header); fall back to .drc.results
    const drc=(s.results||[]).find(r=>/\.drc\.summary$/.test(r.name))
           || (s.results||[]).find(r=>r.type==='drc' && /\.drc\.(summary|results)$/.test(r.name));
    if(!drc){ set('<span class="pill warn">no previous DRC log found</span> pick a design manually below and click Run.'); return; }
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
    set('running <b>'+esc(cell)+'</b> &mdash; progress bar is below &#8595;');
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
  try{ d=await jget('/api/searchlogs?user='+u+'&extra='+x); }
  finally{ $('#searchbtn').disabled=false; }
  if($('#simuser').value.trim()==='' && d.user)$('#simuser').value=d.user;
  const found=(d.roots||[]).filter(r=>r.exists).map(r=>r.root);
  const miss=(d.roots||[]).filter(r=>!r.exists).map(r=>r.root);
  $('#searchroots').innerHTML='searched (user=<b>'+esc(d.user)+'</b>): '+
    found.map(r=>'<span style="color:var(--good)">'+esc(r)+'</span>').join(' , ')+
    (miss.length?' &nbsp;| not present: '+miss.map(esc).join(' , '):'');
  $('#searchmsg').textContent=d.count+' log(s) found'+(d.truncated?' (truncated)':'');
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
      '<td>'+esc(r.name)+'</td>'+
      '<td style="white-space:nowrap">'+fmtSize(r.size)+'</td>'+
      '<td class="muted" style="font-size:12px">'+esc(r.dir)+'</td>'+
      '<td style="white-space:nowrap"><button class="sec" style="padding:3px 9px" data-a="pre">use</button> '+
      '<button class="sec" style="padding:3px 9px" data-a="view">view</button></td>';
    tr.querySelector('[data-a=pre]').onclick=()=>{$('#prefillpath').value=r.path;$('#prefillbtn').click();
      $('#searchpanel').classList.add('hidden');};
    tr.querySelector('[data-a=view]').onclick=()=>{
      $$('.tab').forEach(x=>x.classList.remove('active'));
      document.querySelector('.tab[data-tab=history]').classList.add('active');
      $$('main>section').forEach(s=>s.classList.add('hidden'));
      $('#tab-history').classList.remove('hidden');viewResult(r.path);};
    tb.appendChild(tr);
  });
  if(!rows.length)tb.innerHTML='<tr><td colspan=6 class="muted">no logs found &mdash; adjust user or add a folder above</td></tr>';
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
  $('#resultpanel').style.display='none'; $('#livepanel').style.display='block'; }
// if the user scrolls up during a run, stop auto-following
window.addEventListener('wheel',()=>{FOLLOW_LOG=false;},{passive:true});
window.addEventListener('touchmove',()=>{FOLLOW_LOG=false;},{passive:true});
$('#runbtn').onclick=async()=>{
  const body={tool:currentTool(),lib:$('#lib').value,cell:$('#cell').value,view:$('#view').value,
    deck:$('#deck').value,src_net:$('#srcnet').value,existing_gds:$('#existinggds').value};
  if(!body.lib||!body.cell||!body.view){$('#runmsg').textContent='pick lib/cell/view';return;}
  $('#runbtn').disabled=true;$('#runmsg').textContent='launching...';
  const d=await jpost('/api/run',body);
  $('#runbtn').disabled=false;
  if(d.error){$('#runmsg').textContent='ERROR: '+d.error;return;}
  $('#runmsg').textContent='job '+d.job_id+'  ('+d.run_dir+')';
  startRunUI();
  $('#livepanel').scrollIntoView({behavior:'smooth',block:'start'});
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(()=>pollJob(d.job_id),1200);
  pollJob(d.job_id);
};
function fmtDur(s){s=Math.round(s||0);const m=Math.floor(s/60),ss=s%60;
  return m>0?(m+'m'+(ss<10?'0':'')+ss+'s'):(ss+'s');}
function renderProgress(d,st){
  const fill=$('#progfill'),txt=$('#progtext');
  fill.classList.remove('indet','bad','good');
  if(st==='done'){
    fill.classList.add('good');fill.style.width='100%';
    txt.innerHTML='&#10003; completed in '+fmtDur(d.elapsed);
  }else if(st==='failed'){
    fill.classList.add('bad');fill.style.width='100%';
    txt.innerHTML='&#10007; failed after '+fmtDur(d.elapsed);
  }else if(d.progress!=null){
    fill.style.width=d.progress+'%';
    txt.innerHTML='<span class="spinner"></span>'+d.progress+'% &bull; elapsed '+fmtDur(d.elapsed)+
      (d.eta?(' &bull; ~'+fmtDur(d.eta)+' remaining'):'')+
      ' <span class="muted">(est. from prior run)</span>';
  }else{
    fill.classList.add('indet');fill.style.width='35%';
    txt.innerHTML='<span class="spinner"></span>running&hellip; elapsed '+fmtDur(d.elapsed)+
      ' <span class="muted">(no prior run for ETA)</span>';
  }
}
async function pollJob(jid){
  const d=await jget('/api/job?id='+encodeURIComponent(jid));
  if(d.error){$('#jobstate').textContent=d.error;return;}
  const st=d.state;
  const cls=st==='done'?'good':(st==='failed'?'bad':'warn');
  $('#jobstate').className='pill '+cls;$('#jobstate').textContent=st;
  renderProgress(d,st);
  $('#steps').innerHTML=(d.steps||[]).map(s=>{
    const c=s.state==='done'?'good':(s.state==='failed'?'bad':'warn');
    return '<div style="margin:3px 0"><span class="pill '+c+'">'+esc(s.state)+'</span> '+
      esc(s.name)+(s.rc!=null?' <span class="muted">rc='+s.rc+'</span>':'')+
      '<br><code style="font-size:11px">'+esc(s.cmd)+'</code></div>';
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
  let h='<div style="margin-bottom:8px">'+statusPill(r)+' <span class="muted">'+esc(r.cell||'')+
        (r.version?(' &bull; '+esc(r.version)):'')+'</span></div>';
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
    h+='<div class="panel"><h2>DRC rule diff &nbsp;<span class="muted">total '+laA.tag+'='+dd.total_a+' &rarr; '+laB.tag+'='+dd.total_b+'</span></h2>';
    if(!dd.rows.length){h+='<div class="muted">no differing / non-zero rules</div>';}
    else{h+='<table><thead><tr><th>Rule</th><th>A<br><span class="muted" style="font-weight:400;font-size:11px">'+laA.short+'</span></th>'+
      '<th>B<br><span class="muted" style="font-weight:400;font-size:11px">'+laB.short+'</span></th><th>&Delta;</th><th>Status</th></tr></thead><tbody>';
      dd.rows.forEach(r=>{h+='<tr><td>'+esc(r.rule)+'</td><td>'+(r.a==null?'&mdash;':r.a)+
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
  netlist_cmd:'source-netlist command template (optional)',
  modules:'modules to auto-load if tools missing',
  module_load_cmd:'module-load command template ({modules})',
  auto_load_modules:'auto module-load on run (yes/no)',
  sim_user:'sim user for log search (blank = login name)',
  sim_roots:'log-search roots ({user} expands)',
  drc_extra_svrf:'extra DRC SVRF lines',lvs_extra_svrf:'extra LVS SVRF lines'};
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
loadLibs();
checkEnv();
loadDecks();
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def main():
    global CONFIG, CONFIG_PATH, RUNS_BASE
    ap = argparse.ArgumentParser(description="Calibre DRC/LVS browser GUI")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--base", default=os.path.abspath("./calibre_runs"),
                    help="base dir for run outputs")
    ap.add_argument("--config", default=os.path.abspath("./calibre_gui_config.json"))
    ap.add_argument("--open", action="store_true", help="open a browser")
    args = ap.parse_args()

    CONFIG_PATH = os.path.abspath(args.config)
    CONFIG = load_config(CONFIG_PATH)
    if not os.path.isfile(CONFIG_PATH):
        save_config(CONFIG_PATH, CONFIG)  # materialize defaults for editing
    RUNS_BASE = os.path.abspath(args.base)
    os.makedirs(RUNS_BASE, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 66)
    print(" Calibre DRC/LVS GUI")
    print("   URL       : %s" % url)
    print("   runs dir  : %s" % RUNS_BASE)
    print("   config    : %s" % CONFIG_PATH)
    print("   cds.lib   : %s" % CONFIG["cds_lib"])
    print("   NOTE: Calibre/strmout inherit THIS shell's environment.")
    print("   Ctrl-C to stop.")
    print("=" * 66)
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
