# Every package with patches: its prepare() still runs on the pinned upstream commit (all
# patches apply, in order), from the source cache and without network. A failure here is what
# the next build would stop at, found before building.

import shutil

import pytest

from conftest import PKG, git_files
from prepare_helper import SRCDEST, Unavailable, has_prepare, run_prepare

# Known findings, kept visible: the test fails again (strict xfail) once they are fixed
FUZZ = {}


def patched_packages():
    names = sorted({f.parent.name for f in git_files(PKG) if f.suffix == ".patch"})
    return [n for n in names if has_prepare(n)]


def params():
    return [pytest.param(n, marks=[pytest.mark.xfail(strict=True, reason=FUZZ[n])]
                         if n in FUZZ else []) for n in patched_packages()]


@pytest.mark.slow
@pytest.mark.skipif(not SRCDEST.is_dir(), reason="no source cache (build/cache/sources)")
@pytest.mark.parametrize("name", params())
def test_prepare_applies_all_patches(name, tmp_path):
    work = tmp_path / name
    try:
        r = run_prepare(name, work)
    except Unavailable as e:
        shutil.rmtree(work, ignore_errors=True)
        pytest.skip(str(e))
    try:
        assert r.returncode == 0, f"prepare() of {name} failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
        out = r.stdout + r.stderr
        assert "FAILED" not in out and ".rej" not in out, out[-3000:]
        # patch(1) applies a hunk whose context lines differ ("fuzz") without failing: it may
        # land in the wrong place. An offset alone (the lines moved) is harmless.
        fuzz = [line for line in out.splitlines() if " with fuzz " in line]
        assert not fuzz, "\n".join(fuzz)
    finally:
        shutil.rmtree(work, ignore_errors=True)
