# The word lists of the keyboard's completion (plinius-words, make-words.py) and how
# stevia-plinius takes them in: case from hunspell, one list per language of the phone, the
# words completer as Stevia's default.

import re
import subprocess
import sys

import pytest

from conftest import PKG, load_script

WORDS = PKG / "plinius-words"
SCRIPT = WORDS / "make-words.py"
mw = load_script(SCRIPT)

# hunspell -l -L as the German dictionary answers: the lines with a word it rejects
FAKE_HUNSPELL = r"""
while IFS= read -r line; do
    case "$line" in
        bahnhof|ichabod|Ichabod|harry|Harry|geschichte) printf '%s\n' "$line" ;;
    esac
done
"""


def pkgbuild_array(path, name):
    # Comments first: they may hold parentheses
    text = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())
    body = re.search(rf"^{name}=\((.*?)\)", text, re.M | re.S).group(1)
    return re.findall(r"'[^']*'|\"[^\"]*\"|\S+", body)


@pytest.mark.parametrize("word,ok", [
    ("hallo", True), ("Bahnhof", True), ("e-mail", True), ("geht's", True), ("l’homme", True),
    ("goin'", True), ("naïve", True), ("नमस्ते", True),
    ("", False), ("-ab", False), ("ab-", False), ("'ab", False), ("a--b", False),
    ("a-'b", False), ("abc1", False), ("a b", False), ("a.b", False),
])
def test_what_is_a_word(word, ok):
    assert mw.is_word(word) is ok


@pytest.mark.parametrize("word,stutter", [
    ("ich-ich", True), ("No-no-NO", True), ("e-mail", False), ("hallo", False),
    ("ich-ich-du", False),
])
def test_what_is_a_stutter(word, stutter):
    assert mw.is_stutter(word) is stutter


def make(tmp_path, fake_bin, lines, dictionary=True):
    src = tmp_path / "de_50k.txt"
    src.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    out = tmp_path / "de.words"
    fake_bin.stub("hunspell", FAKE_HUNSPELL)
    args = [sys.executable, str(SCRIPT), str(src), str(out)]
    if dictionary:
        args.append("/usr/share/hunspell/de_DE")
    r = subprocess.run(args, capture_output=True, text=True, env=fake_bin.env())
    assert r.returncode == 0, r.stderr
    return out.read_text(encoding="utf-8").splitlines()


LIST = ["ich 5890279", "hallo 128561", "", "bahnhof 2487", "ichabod 900", "harry 800",
        "ich-ich 700", "geschichte 600", "e-mail 500", "hallo 400", "zu1 300", "nicht 200",
        "Café 100", "café 90"]


def test_case_from_the_dictionary(tmp_path, fake_bin):
    # Short words, stutters, non-words and repeats are left out; what hunspell takes in lower
    # case stays so, what it takes only capitalized is capitalized, the rest goes. Words that
    # differ in
    # case only (the keyboard keeps the first of them)
    assert make(tmp_path, fake_bin, LIST) == ["hallo", "Bahnhof", "Geschichte", "e-mail",
                                              "nicht", "Café", "café"]
    calls = fake_bin.calls("hunspell")
    assert calls == [["-i", "utf-8", "-l", "-L", "-d", "/usr/share/hunspell/de_DE"]] * 2


def test_without_a_dictionary_the_words_stay(tmp_path, fake_bin):
    assert make(tmp_path, fake_bin, LIST, dictionary=False) == [
        "hallo", "bahnhof", "ichabod", "harry", "geschichte", "e-mail", "nicht", "Café", "café"]
    assert fake_bin.calls("hunspell") == []


def test_a_capitalized_word_that_is_in_the_list_twice(tmp_path, fake_bin):
    # "Bahnhof" itself and "bahnhof" capitalized: one word
    assert make(tmp_path, fake_bin, ["Bahnhof 9", "bahnhof 8"]) == ["Bahnhof"]


def test_usage(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "only-one"], capture_output=True, text=True)
    assert r.returncode == 2 and r.stderr.startswith("usage: ")


def test_main_in_process(tmp_path, fake_bin, monkeypatch):
    src = tmp_path / "en_50k.txt"
    src.write_text("hello 3\nworld 2\n")
    assert mw.main(["make-words.py", str(src), str(tmp_path / "en.words")]) == 0
    assert (tmp_path / "en.words").read_text() == "hello\nworld\n"
    assert mw.main(["make-words.py"]) == 2


def test_a_list_for_every_language_of_the_phone():
    phone = re.search(r"^_langs=\(([^)]*)\)", (PKG / "plinius-desktop" / "PKGBUILD").read_text(),
                      re.M).group(1).split()
    langs = dict(entry.split(":") for entry in pkgbuild_array(WORDS / "PKGBUILD", "_langs"))
    assert set(phone) | {"en"} == set(langs)
    makedepends = pkgbuild_array(WORDS / "PKGBUILD", "makedepends")
    # Every dictionary comes from a hunspell package; Arch has none for Portuguese
    assert langs["pt"] == "-"
    for lang, dictionary in langs.items():
        if dictionary != "-":
            assert dictionary.startswith(lang + "_")
            assert any(p.startswith(f"hunspell-{lang}") for p in makedepends), lang


def test_the_lists_are_pinned():
    text = (WORDS / "PKGBUILD").read_text()
    commit = re.search(r"^_commit=([0-9a-f]{40})$", text, re.M)
    assert commit
    langs = pkgbuild_array(WORDS / "PKGBUILD", "_langs")
    sums = pkgbuild_array(WORDS / "PKGBUILD", "sha256sums")
    # make-words.py, LICENSE, then one list per language with its checksum
    assert len(sums) == 2 + len(langs)
    assert all(re.fullmatch(r"'[0-9a-f]{64}'", s) for s in sums[2:])


def test_stevia_completes_from_the_lists():
    stevia = PKG / "stevia-plinius"
    assert "plinius-words" in pkgbuild_array(stevia / "PKGBUILD", "depends")
    override = (stevia / "50_stevia-plinius.gschema.override").read_text()
    assert "[mobi.phosh.osk.Completers]\ndefault='words'" in override
    text = (stevia / "PKGBUILD").read_text()
    prepare = text[text.index("prepare()"):text.index("build()")]
    # The core goes in before the patch that builds it
    assert prepare.index("pos-words.c") < prepare.index("0015-")
    assert "50_stevia-plinius.gschema.override" in text[text.index("package()"):]
