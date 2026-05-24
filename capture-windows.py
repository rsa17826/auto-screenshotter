#!/usr/bin/env python3
"""
capture-windows.py
──────────────────
Launches your project, watches for new GUI windows, screenshots them,
and keeps only visually distinct ones in .images/ (or --out <dir>).

Usage:
  python capture-windows.py -- python myapp.py
  python capture-windows.py -- ./my_gui_app --some-flag
  python capture-windows.py --threshold 8 --out .images -- npx electron .

Dedup logic: perceptual hash (pHash). Two images are "the same" when
their Hamming distance < --threshold (default 10 out of 64 bits).
Lower threshold = stricter (keeps more). Higher = looser (keeps fewer).

Dependencies:
  pip install Pillow imagehash

Linux (X11):
  sudo apt install imagemagick xdotool    # or: brew install imagemagick xdotool

macOS:
  pip install pyobjc-framework-Quartz     # for window-level screenshots
  (screencapture is built-in)

Hyprland / Wayland (wlroots):
  sudo pacman -S grim        # screenshot tool
  (hyprctl is built into Hyprland)
  xdotool is NOT needed on Hyprland — hyprctl handles window tracking.
"""

import sys
import os
import time
import argparse
import platform
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

# ── Dependency check ────────────────────────────────────────────────────────

try:
  from PIL import Image
  import imagehash
except ImportError:
  print(
    "\n[capture] Missing Python dependencies.\n"
    "  Run: pip install Pillow imagehash\n",
    file=sys.stderr,
  )
  sys.exit(1)

PLATFORM = platform.system() # 'Linux', 'Darwin', 'Windows'

# ── Backend detection ────────────────────────────────────────────────────────


def detect_backend() -> str:
  """
  Detect the best window-capture backend for this environment.

  Returns one of: 'hyprland', 'x11', 'macos', 'unsupported'
  """
  if PLATFORM == "Darwin":
    return "macos"

  if PLATFORM == "Linux":
    # Hyprland sets this env var in every child process
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or shutil.which("hyprctl"):
      # Double-check hyprctl actually responds (user might have it installed
      # but not be running Hyprland right now)
      try:
        r = subprocess.run(
          ["hyprctl", "version"],
          capture_output=True,
          timeout=3,
        )
        if r.returncode == 0:
          return "hyprland"
      except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Generic Wayland (non-Hyprland) — limited support
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session == "wayland":
      return "wayland-generic"

    # X11 or XWayland
    if shutil.which("xdotool"):
      return "x11"

    # Last resort: try xdotool anyway
    return "x11"

  return "unsupported"


# ── CLI ─────────────────────────────────────────────────────────────────────


def parse_args():
  p = argparse.ArgumentParser(
    description="Auto-screenshot project windows; keep only distinct ones.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
  )
  p.add_argument(
    "--threshold",
    type=int,
    default=10,
    help="pHash Hamming distance below which two images are 'same' (default: 10). "
    "Range 0-64. Lower = keep more, higher = keep fewer.",
  )
  p.add_argument(
    "--out",
    default=".images",
    help="Output directory relative to cwd (default: .images)",
  )
  p.add_argument(
    "--poll",
    type=float,
    default=0.8,
    help="Window-poll interval in seconds (default: 0.8)",
  )
  p.add_argument(
    "--delay",
    type=float,
    default=1.8,
    help="Seconds to wait after a new window appears before screenshotting "
    "(lets the UI finish rendering; default: 1.8)",
  )
  p.add_argument(
    "--prefix",
    default="screenshot",
    help="Filename prefix for saved images (default: screenshot)",
  )
  p.add_argument(
    "--retake",
    action="store_true",
    help="Re-screenshot all windows periodically (every --retake-interval s) "
    "to catch UI state changes",
  )
  p.add_argument(
    "--retake-interval",
    type=float,
    default=5.0,
    help="How often to retake shots of known windows (default: 5.0s, only with --retake)",
  )
  p.add_argument(
    "--shell",
    "-s",
    action="store_true",
    help="Run the command through $SHELL (needed for &&, pipes, cd, etc.). "
    "Example: --shell -- 'cd ../myapp && go run .'",
  )
  p.add_argument(
    "--cwd",
    default=None,
    help="Working directory for the launched command (default: current dir). "
    "Example: --cwd ../input-display -- go run .",
  )
  p.add_argument(
    "command",
    nargs=argparse.REMAINDER,
    help="Command to run (put after --)",
  )
  return p.parse_args()


# ── Image deduplication ──────────────────────────────────────────────────────


def _compute_hashes(img: Image.Image) -> dict:
  """Compute phash, dhash, and colorhash for an image."""
  return {
    "ph": imagehash.phash(img),
    "dh": imagehash.dhash(img),
    "ch": imagehash.colorhash(img),
  }


def load_existing_hashes(out_dir: Path) -> list[tuple]:
  """Return list of (hash_dict, Path) for every PNG already in out_dir."""
  result = []
  for p in sorted(out_dir.glob("*.png")):
    try:
      h = _compute_hashes(Image.open(p))
      result.append((h, p))
    except Exception:
      pass
  return result


def is_distinct(
  img_path: Path,
  existing: list[tuple],
  threshold: int,
) -> tuple[bool, dict | None]:
  """
  Return (True, hashes) if img_path is visually distinct from all existing images.

  Uses three complementary hashes:
    - phash  (perceptual): robust for structural layout changes
    - dhash  (difference): sensitive to edge/gradient differences
    - colorhash: catches color-palette changes that phash/dhash miss

  Two images are "the same" only when ALL three are within threshold.
  This prevents False-deduplication of screens that differ only in colour
  or only in layout.
  """
  try:
    nh = _compute_hashes(Image.open(img_path))
  except Exception:
    return False, None

  # colorhash has a 42-bit space vs 64-bit for ph/dh, scale threshold accordingly
  color_thr = max(2, threshold // 3)

  for eh, _ in existing:
    ph_close = (nh["ph"] - eh["ph"]) < threshold
    dh_close = (nh["dh"] - eh["dh"]) < threshold
    ch_close = (nh["ch"] - eh["ch"]) <= color_thr
    if ph_close and dh_close and ch_close:
      return False, nh # "same" — all three hashes agree it's similar

  return True, nh


# ── Platform: Linux / X11 ───────────────────────────────────────────────────


def check_linux_deps():
  missing = []
  for tool in ("xdotool", "import"): # 'import' is part of imagemagick
    if not shutil.which(tool):
      missing.append(tool)
  if missing:
    print(
      f"[capture] Warning: missing tools: {', '.join(missing)}\n"
      "  Install with: sudo apt install imagemagick xdotool\n"
    )


def get_window_ids_linux(pid: int) -> set[str]:
  """Return set of X11 window ID strings for the given PID."""
  try:
    r = subprocess.run(
      ["xdotool", "search", "--pid", str(pid)],
      capture_output=True,
      text=True,
      timeout=5,
    )
    ids = r.stdout.strip().split()
    return set(ids) if ids else set()
  except (FileNotFoundError, subprocess.TimeoutExpired):
    return set()


def get_window_title_linux(wid: str) -> str:
  try:
    r = subprocess.run(
      ["xdotool", "getwindowname", wid],
      capture_output=True,
      text=True,
      timeout=3,
    )
    return r.stdout.strip() or wid
  except Exception:
    return wid


def screenshot_window_linux(wid: str, outpath: Path) -> bool:
  """Screenshot a specific X11 window. Returns True on success."""
  # Raise and focus the window so it's not occluded
  subprocess.run(
    ["xdotool", "windowactivate", "--sync", wid],
    capture_output=True,
    timeout=5,
  )
  time.sleep(0.4)

  # Primary: ImageMagick import (captures by window ID, works without focus)
  if shutil.which("import"):
    r = subprocess.run(
      ["import", "-window", wid, str(outpath)],
      capture_output=True,
      timeout=15,
    )
    if r.returncode == 0 and outpath.exists() and outpath.stat().st_size > 0:
      return True

  # Fallback: scrot --focused (requires window to be active)
  if shutil.which("scrot"):
    r = subprocess.run(
      ["scrot", "--focused", str(outpath)],
      capture_output=True,
      timeout=15,
    )
    if r.returncode == 0 and outpath.exists() and outpath.stat().st_size > 0:
      return True

  # Fallback: xwd + convert
  if shutil.which("xwd") and shutil.which("convert"):
    xwd_path = outpath.with_suffix(".xwd")
    r1 = subprocess.run(
      ["xwd", "-id", wid, "-out", str(xwd_path)],
      capture_output=True,
      timeout=10,
    )
    if r1.returncode == 0:
      r2 = subprocess.run(
        ["convert", str(xwd_path), str(outpath)],
        capture_output=True,
        timeout=10,
      )
      xwd_path.unlink(missing_ok=True)
      if r2.returncode == 0 and outpath.exists():
        return True

  return False


# ── Platform: Hyprland (Wayland / wlroots) ───────────────────────────────────


def check_hyprland_deps():
  missing = []
  if not shutil.which("grim"):
    missing.append("grim")
  if not shutil.which("hyprctl"):
    missing.append("hyprctl")
  if missing:
    print(
      f"[capture] Warning: missing tools: {', '.join(missing)}\n"
      "  Install with: sudo pacman -S grim\n"
      "  (hyprctl ships with Hyprland itself)\n"
    )


def get_descendant_pids(root_pid: int) -> set[int]:
  """
  Return root_pid plus all its descendant PIDs.
  Needed because GUI toolkits often spawn helper child processes that
  own the actual window, not the parent process we launched.
  """
  pids = {root_pid}
  try:
    r = subprocess.run(
      ["pgrep", "--parent", str(root_pid)],
      capture_output=True,
      text=True,
      timeout=3,
    )
    for child_str in r.stdout.strip().split():
      try:
        child = int(child_str)
        pids |= get_descendant_pids(child)
      except ValueError:
        pass
  except (FileNotFoundError, subprocess.TimeoutExpired):
    pass
  return pids


def get_windows_hyprland(pids: set[int]) -> dict[str, dict]:
  """
  Query hyprctl for all on-screen windows belonging to any PID in `pids`.
  Returns {address: window_dict} where address is Hyprland's unique window key.

  Relevant fields in each window_dict:
    address  - unique hex string, e.g. "0x55a3b2c1d"
    title    - window title
    class    - app class (e.g. "firefox")
    pid      - owning process PID
    at       - [x, y]  position on screen (absolute, in pixels)
    size     - [w, h]  window dimensions in pixels
    mapped   - bool, whether the window is currently shown
  """
  import json

  try:
    r = subprocess.run(
      ["hyprctl", "clients", "-j"],
      capture_output=True,
      text=True,
      timeout=5,
    )
    if r.returncode != 0:
      return {}
    clients = json.loads(r.stdout)
    return {
      w["address"]: w
      for w in clients
      if w.get("pid") in pids
      and w.get("mapped", False)
      and w.get("size", [0, 0])[0] > 0 # skip zero-size windows
    }
  except (
    FileNotFoundError,
    subprocess.TimeoutExpired,
    json.JSONDecodeError,
    KeyError,
  ):
    return {}


def screenshot_window_hyprland(win: dict, outpath: Path) -> bool:
  """
  Screenshot a Hyprland window using grim with its geometry.

  grim geometry format: "X,Y WxH"
  We add a small 2px inset to avoid capturing the border/shadow.
  """
  x, y = win["at"]
  w, h = win["size"]

  if w <= 0 or h <= 0:
    return False

  geometry = f"{x},{y} {w}x{h}"

  r = subprocess.run(
    ["grim", "-g", geometry, str(outpath)],
    capture_output=True,
    timeout=15,
  )
  return r.returncode == 0 and outpath.exists() and outpath.stat().st_size > 0


def get_windows_macos(pid: int) -> list[dict]:
  """Return list of on-screen window dicts for the given PID."""
  try:
    from Quartz import (
      CGWindowListCopyWindowInfo,
      kCGWindowListOptionOnScreenOnly,
      kCGNullWindowID,
    )

    all_windows = CGWindowListCopyWindowInfo(
      kCGWindowListOptionOnScreenOnly, kCGNullWindowID
    )
    return [
      w
      for w in all_windows
      if w.get("kCGWindowOwnerPID") == pid
      and w.get("kCGWindowLayer") == 0 # normal windows only
      and w.get("kCGWindowAlpha", 0) > 0
    ]
  except ImportError:
    print(
      "[capture] pyobjc-framework-Quartz not found; "
      "install with: pip install pyobjc-framework-Quartz",
      file=sys.stderr,
    )
    return []


def screenshot_window_macos(window_id: int, outpath: Path) -> bool:
  """Screenshot a specific macOS window by CGWindowID."""
  r = subprocess.run(
    ["screencapture", "-l", str(window_id), "-x", str(outpath)],
    capture_output=True,
    timeout=15,
  )
  return r.returncode == 0 and outpath.exists() and outpath.stat().st_size > 0


# ── Core loop ────────────────────────────────────────────────────────────────


def try_capture(
  wid,
  out_dir: Path,
  prefix: str,
  existing_hashes: list,
  threshold: int,
  backend: str,
  win_info: dict | None = None, # hyprland passes the full window dict
) -> bool:
  """
  Screenshot window `wid`, test distinctness, save or discard.
  Returns True if a new image was saved.
  """
  timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
  tmp_path = out_dir / f"_tmp_{timestamp}.png"

  if backend == "hyprland":
    ok = screenshot_window_hyprland(win_info, tmp_path)
  elif backend == "x11":
    ok = screenshot_window_linux(str(wid), tmp_path)
  elif backend == "macos":
    ok = screenshot_window_macos(int(wid), tmp_path)
  else:
    ok = False

  if not ok:
    tmp_path.unlink(missing_ok=True)
    print(f"[capture]   ✗ Screenshot failed for window {wid}")
    return False

  distinct, new_hashes = is_distinct(tmp_path, existing_hashes, threshold)
  if distinct:
    final_path = out_dir / f"{prefix}_{timestamp}.png"
    tmp_path.rename(final_path)
    existing_hashes.append((new_hashes, final_path))
    print(f"[capture]   ✓ Saved → {final_path.name}")
    return True
  else:
    tmp_path.unlink(missing_ok=True)
    print(f"[capture]   ~ Skipped (too similar to an existing screenshot)")
    return False


def run(args):
  cmd = args.command
  if cmd and cmd[0] == "--":
    cmd = cmd[1:]
  if not cmd:
    print(
      "[capture] Error: no command given.\n"
      "  Example: python capture-windows.py -- python myapp.py\n"
      "  Shell:   python capture-windows.py --shell -- 'cd ../app && go run .'\n"
      "  Alt cwd: python capture-windows.py --cwd ../input-display -- go run .\n",
      file=sys.stderr,
    )
    sys.exit(1)

  # Auto-detect shell mode: if user passed a single string containing shell
  # operators (&& || | ; > <), they almost certainly want --shell
  shell_operators = {"&&", "||", "|", ";", ">", "<", ">>"}
  use_shell = args.shell
  if not use_shell and len(cmd) == 1 and any(op in cmd[0] for op in shell_operators):
    use_shell = True
    print("[capture] Auto-enabling --shell (detected shell operators in command)")

  # Build the actual Popen arguments
  if use_shell:
    # Join back into one string and let $SHELL parse it
    shell_cmd = cmd[0] if len(cmd) == 1 else " ".join(cmd)
    popen_args = shell_cmd
  else:
    popen_args = cmd

  # Resolve --cwd relative to wherever the script was invoked from
  launch_cwd = Path(args.cwd).resolve() if args.cwd else None
  if launch_cwd and not launch_cwd.is_dir():
    print(
      f"[capture] Error: --cwd {launch_cwd!r} is not a directory.",
      file=sys.stderr,
    )
    sys.exit(1)

  backend = detect_backend()
  out_dir = Path(args.out)
  out_dir.mkdir(parents=True, exist_ok=True)

  # Dep checks per backend
  if backend == "hyprland":
    check_hyprland_deps()
  elif backend == "x11":
    check_linux_deps()
  elif backend == "wayland-generic":
    print(
      "[capture] Non-Hyprland Wayland detected. Only Hyprland is fully supported.\n"
      "  If you're on Sway/river/etc., open an issue — the grim backend\n"
      "  can likely be adapted. Trying X11 fallback (may not work)…\n"
    )
    backend = "x11"
  elif backend == "unsupported":
    print(f"[capture] Platform '{PLATFORM}' is not supported. Exiting.")
    sys.exit(1)

  existing_hashes = load_existing_hashes(out_dir)

  print(f"[capture] Backend    : {backend}")
  print(f"[capture] Output dir : {out_dir.resolve()}")
  print(f"[capture] Threshold  : {args.threshold}  (pHash Hamming, 0-64)")
  print(f"[capture] Existing   : {len(existing_hashes)} screenshot(s)")
  print(f"[capture] Shell mode : {'yes' if use_shell else 'no'}")
  if launch_cwd:
    print(f"[capture] Launch cwd : {launch_cwd}")
  print(
    f"[capture] Launching  : {popen_args if isinstance(popen_args, str) else ' '.join(popen_args)}"
  )
  print()

  proc = subprocess.Popen(popen_args, shell=use_shell, cwd=launch_cwd)
  pid = proc.pid
  print(f"[capture] PID {pid} — watching for windows…")

  seen_windows: set = set()
  last_retake: dict[object, float] = {}
  saved = skipped = 0

  try:
    while proc.poll() is None:
      time.sleep(args.poll)

      # ── Hyprland ──────────────────────────────────────────────────────
      if backend == "hyprland":
        # Track the whole process tree so child-process windows are found
        all_pids = get_descendant_pids(pid)
        current = get_windows_hyprland(all_pids) # {address: win_dict}
        new_addrs = set(current) - seen_windows

        for addr in new_addrs:
          seen_windows.add(addr)
          win = current[addr]
          title = win.get("title") or win.get("class") or addr
          pos = win["at"]
          size = win["size"]
          print(
            f"[capture] New window: {title!r}  ({size[0]}×{size[1]} at {pos[0]},{pos[1]})"
          )
          print(f"[capture]   Waiting {args.delay}s for UI to render…")
          time.sleep(args.delay)

          # Re-fetch geometry — window may have moved/resized during delay
          refreshed = get_windows_hyprland(all_pids)
          win = refreshed.get(addr, win)

          if try_capture(
            addr,
            out_dir,
            args.prefix,
            existing_hashes,
            args.threshold,
            backend,
            win_info=win,
          ):
            saved += 1
          else:
            skipped += 1
          last_retake[addr] = time.time()

        if args.retake:
          now = time.time()
          refreshed = None
          for addr in list(seen_windows):
            if now - last_retake.get(addr, 0) >= args.retake_interval:
              if refreshed is None:
                refreshed = get_windows_hyprland(
                  get_descendant_pids(pid)
                )
              win = refreshed.get(addr)
              if win:
                if try_capture(
                  addr,
                  out_dir,
                  args.prefix,
                  existing_hashes,
                  args.threshold,
                  backend,
                  win_info=win,
                ):
                  saved += 1
                else:
                  skipped += 1
              last_retake[addr] = now

      # ── X11 ───────────────────────────────────────────────────────────
      elif backend == "x11":
        current = get_window_ids_linux(pid)
        new_wids = current - seen_windows
        for wid in new_wids:
          seen_windows.add(wid)
          title = get_window_title_linux(wid)
          print(f"[capture] New window: {title!r}  (id={wid})")
          print(f"[capture]   Waiting {args.delay}s for UI to render…")
          time.sleep(args.delay)
          if try_capture(
            wid,
            out_dir,
            args.prefix,
            existing_hashes,
            args.threshold,
            backend,
          ):
            saved += 1
          else:
            skipped += 1
          last_retake[wid] = time.time()

        if args.retake:
          now = time.time()
          for wid in list(seen_windows):
            if now - last_retake.get(wid, 0) >= args.retake_interval:
              if try_capture(
                wid,
                out_dir,
                args.prefix,
                existing_hashes,
                args.threshold,
                backend,
              ):
                saved += 1
              else:
                skipped += 1
              last_retake[wid] = now

      # ── macOS ─────────────────────────────────────────────────────────
      elif backend == "macos":
        windows = get_windows_macos(pid)
        current = {w["kCGWindowNumber"]: w for w in windows}
        new_ids = set(current) - seen_windows
        for wid in new_ids:
          seen_windows.add(wid)
          title = current[wid].get("kCGWindowName", str(wid))
          print(f"[capture] New window: {title!r}  (id={wid})")
          print(f"[capture]   Waiting {args.delay}s for UI to render…")
          time.sleep(args.delay)
          if try_capture(
            wid,
            out_dir,
            args.prefix,
            existing_hashes,
            args.threshold,
            backend,
          ):
            saved += 1
          else:
            skipped += 1
          last_retake[wid] = time.time()

        if args.retake:
          now = time.time()
          for wid in list(seen_windows):
            if now - last_retake.get(wid, 0) >= args.retake_interval:
              if try_capture(
                wid,
                out_dir,
                args.prefix,
                existing_hashes,
                args.threshold,
                backend,
              ):
                saved += 1
              else:
                skipped += 1
              last_retake[wid] = now

  except KeyboardInterrupt:
    print("\n[capture] Interrupted by user.")
    proc.terminate()

  proc.wait()
  print(
    f"\n[capture] Finished.  Saved: {saved}  |  Deduplicated/skipped: {skipped}\n"
    + f"[capture] Images in: {out_dir.resolve()}"
  )


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
  if not os.path.isfile(".gitignore"):
    with open(".gitignore", "w") as f:
      f.write(".images\n")
  else:
    # Use "a+" to read and append
    with open(".gitignore", "a+") as f:
      f.seek(0) # Move the cursor to the beginning to read the file
      lines = f.read().splitlines()

      if ".images" not in lines:
        # Add a newline before if the file doesn't end in one
        f.write("\n.images\n")

  run(parse_args())
