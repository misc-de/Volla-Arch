# The word suggestions of the on-screen keyboard (stevia-plinius): Stevia's default completer is
# hunspell in en_US and finds a dictionary by language and region (<lang>_<REGION>.dic in
# /usr/share/hunspell). Without one it has no completer at all, also with completion-mode on.

import re

from conftest import PKG

PKGBUILD = (PKG / "stevia-plinius" / "PKGBUILD").read_text()


def array(name):
    body = re.search(rf"^{name}=\((.*?)^\)", PKGBUILD, re.M | re.S).group(1)
    body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    return re.findall(r"'[^']*'|\S+", body)


def test_the_default_dictionary_comes_along():
    assert "hunspell" in array("depends")
    assert "hunspell-en_us" in array("depends")


def test_a_dictionary_is_offered_for_every_language_of_the_phone():
    langs = re.search(r"^_langs=\(([^)]*)\)", (PKG / "plinius-desktop" / "PKGBUILD").read_text(),
                      re.M).group(1).split()
    offered = {entry.strip("'").split(":", 1)[0] for entry in array("optdepends")}
    assert all(":" in entry for entry in array("optdepends"))
    covered = {lang for lang in langs if any(p.startswith(f"hunspell-{lang}") for p in offered)}
    # Arch has no Portuguese hunspell dictionary
    assert set(langs) - covered == {"pt"}
