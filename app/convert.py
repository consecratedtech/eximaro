# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (C) 2026 Consecrated Tech
"""Convert a PowerPoint file into one image per slide.

Heavy work stays off the displays: a .pptx is rendered to a PDF with LibreOffice
(headless) and then split into one PNG per slide with poppler's pdftoppm. The
displays only ever receive the finished images. Requires the 'libreoffice' and
'poppler-utils' packages, which the installer adds for the controller role.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config


def pptx_to_pngs(pptx_path, out_dir) -> list:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    if not soffice or not pdftoppm:
        raise RuntimeError(
            "PowerPoint conversion needs libreoffice and poppler-utils installed."
        )

    # Scratch must live in the disk-backed work dir, never /tmp: on Trixie /tmp is
    # tmpfs, and the hardened service (ProtectSystem=strict) makes it read-only —
    # only the data dir is writable. LibreOffice also writes a user profile, and
    # ProtectHome=true masks the home dir, so point its UserInstallation here too.
    work = Path(config.WORK)
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        profile = Path(tmp) / "lo_profile"
        # LibreOffice (and fontconfig/dconf underneath it) need a writable HOME
        # and cache/config dirs. The hardened service makes the real home
        # read-only, so point them all at the work dir for this one run.
        env = dict(os.environ)
        env["HOME"] = tmp
        env["TMPDIR"] = tmp  # LibreOffice puts its IPC pipe here; /tmp is read-only
        env["XDG_CACHE_HOME"] = str(Path(tmp) / "cache")
        env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
        try:
            subprocess.run(
                [soffice, f"-env:UserInstallation=file://{profile}",
                 "--headless", "--convert-to", "pdf", "--outdir", tmp, str(pptx_path)],
                check=True, capture_output=True, timeout=180, env=env,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[:500]
            raise RuntimeError(f"LibreOffice could not convert the file: {detail}") from exc
        pdfs = list(Path(tmp).glob("*.pdf"))
        if not pdfs:
            raise RuntimeError("Conversion produced no PDF.")
        subprocess.run(
            [pdftoppm, "-png", "-r", "150", str(pdfs[0]), str(out_dir / "slide")],
            check=True, capture_output=True, timeout=180,
        )

    return sorted(out_dir.glob("slide*.png"))
