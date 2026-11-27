# Word completion of the on-screen keyboard (stevia-plinius 0015): the core pos-words.c, built
# on the host with a harness that drives it command by command. What it completes from, how
# learned words rank and are kept, and which language's list a layout gets.

import os
import stat
import subprocess

import pytest

from conftest import PKG, compile_c

STEVIA = PKG / "stevia-plinius"

HARNESS = r"""
#include "pos-words.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void
print_strv (const char *what, GStrv v)
{
  printf ("%s:", what);
  if (v == NULL)
    printf (" (none)");
  for (guint i = 0; v && v[i]; i++)
    printf (" %s", v[i]);
  printf ("\n");
}

int
main (int argc, char **argv)
{
  g_autoptr (PosWords) w = pos_words_new ();

  pos_words_free (NULL);
  for (int i = 1; i < argc; i++) {
    g_auto (GStrv) a = g_strsplit (argv[i], ":", 5);
    g_autoptr (GError) err = NULL;

    if (!strcmp (a[0], "list")) {
      gboolean ok = pos_words_load_list (w, a[1], &err);
      printf ("list %s %u\n", ok ? "ok" : "error", pos_words_get_n_words (w));
    } else if (!strcmp (a[0], "learned")) {
      gboolean ok = pos_words_load_learned (w, a[1], &err);
      printf ("learned %s %u\n", ok ? "ok" : "error", pos_words_get_n_words (w));
    } else if (!strcmp (a[0], "save")) {
      gboolean ok = pos_words_save_learned (w, a[1], &err);
      printf ("save %s\n", ok ? "ok" : err->message);
    } else if (!strcmp (a[0], "learn")) {
      printf ("learn %s %d\n", a[1], pos_words_learn (w, a[1]));
    } else if (!strcmp (a[0], "learnn")) {
      int n = atoi (a[2]);
      for (int j = 0; j < n; j++)
        pos_words_learn (w, a[1]);
    } else if (!strcmp (a[0], "learnmany")) {
      /* learnmany:<count>:<times>: words "zzz<i>" in letters only */
      int n = atoi (a[1]), times = atoi (a[2]);
      for (int j = 0; j < n; j++) {
        char word[32];
        int k = j, len = 0;
        word[len++] = 'q'; word[len++] = 'q'; word[len++] = 'q';
        do { word[len++] = 'a' + k % 26; k /= 26; } while (k);
        word[len] = '\0';
        for (int t = 0; t < times; t++)
          pos_words_learn (w, word);
      }
      printf ("n %u\n", pos_words_get_n_words (w));
    } else if (!strcmp (a[0], "forget")) {
      pos_words_forget_learned (w);
      printf ("forget %u\n", pos_words_get_n_words (w));
    } else if (!strcmp (a[0], "complete")) {
      g_auto (GStrv) c = pos_words_complete (w, a[1], a[2] ? atoi (a[2]) : 3);
      g_autofree char *what = g_strdup_printf ("complete %s", a[1]);
      print_strv (what, c);
    } else if (!strcmp (a[0], "pick")) {
      g_auto (GStrv) session = g_strsplit (a[4], ",", -1);
      g_autofree char *lang = pos_words_pick_language (a[1], *a[2] ? a[2] : NULL,
                                                       strcmp (a[3], "-") ? a[3] : NULL,
                                                       (const char * const *) session);
      printf ("pick %s\n", lang ? lang : "(null)");
    } else if (!strcmp (a[0], "picknull")) {
      g_autofree char *lang = pos_words_pick_language (a[1], a[2], a[3], NULL);
      printf ("pick %s\n", lang ? lang : "(null)");
    }
  }
  return 0;
}
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("words")
    (d / "harness.c").write_text(HARNESS)
    return compile_c(d / "harness", [d / "harness.c", STEVIA / "pos-words.c"],
                     flags=[f"-I{STEVIA}"], pkgs=("glib-2.0",))


def run(harness, *cmds):
    r = subprocess.run([str(harness), *cmds], capture_output=True, text=True,
                       env={**os.environ, "LC_ALL": "C.UTF-8"})
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


LIST = "\n".join([
    "nicht", "hallo", "halt", "halten", "Haltestelle", "Bahnhof", "Bahnsteig", "bahnen",
    "grüße", "grüßen", "Straße", "Geschichte", "schön", "schon", "schnell",
    "  hallo  ",       # a key seen before
    "", "1234", "-ab", "ab-", "a--b", "e-mail", "geht's", "l’homme", "x'", "'x",
    "Ärger", "ärgern",
]) + "\n"


@pytest.fixture
def lst(tmp_path):
    p = tmp_path / "de.words"
    p.write_text(LIST)
    return p


def test_the_list_loads_words_once_and_passes_over_the_rest(harness, lst):
    # 15 + e-mail, geht's, l’homme, x' (as "goin'") + Ärger, ärgern
    assert run(harness, f"list:{lst}") == ["list ok 21"]


def test_a_missing_list_is_an_error(harness, tmp_path):
    assert run(harness, f"list:{tmp_path}/none.words") == ["list error 0"]


def test_completion_from_three_characters_most_frequent_first(harness, lst):
    out = run(harness, f"list:{lst}", "complete:ha", "complete:hal", "complete:halt",
              "complete:halte", "complete:Hal", "complete:HAL", "complete:sch:2",
              "complete:bahn", "complete:Bahnh", "complete:xyz")
    assert out[1:] == [
        "complete ha: (none)",                         # too short
        "complete hal: hallo halt halten",
        "complete halt: halten Haltestelle",           # never the word itself
        "complete halte: halten Haltestelle",
        "complete Hal: Hallo Halt Halten",             # a capital typed stays one
        "complete HAL: Hallo Halt Halten",
        "complete sch: schön schon",                   # at most max
        "complete bahn: Bahnhof Bahnsteig bahnen",     # nouns keep their capital
        "complete Bahnh: Bahnhof",
        "complete xyz: (none)",
    ]


def test_completion_folds_case_and_sharp_s(harness, lst):
    out = run(harness, f"list:{lst}", "complete:grüs", "complete:STRAS", "complete:ärg",
              "complete:Ärg", "complete:geh", "complete:e-m")
    assert out[1:] == [
        "complete grüs: grüße grüßen",
        "complete STRAS: Straße",
        "complete ärg: Ärger ärgern",
        "complete Ärg: Ärger Ärgern",
        "complete geh: geht's",
        "complete e-m: e-mail",
    ]


def test_completion_of_what_is_no_word(harness, lst):
    out = run(harness, f"list:{lst}", "complete:12345", "complete:hal1", "complete:hal:0",
              "complete:", "complete:-ha")
    assert out[1:] == ["complete 12345: (none)", "complete hal1: (none)",
                       "complete hal: (none)", "complete : (none)", "complete -ha: (none)"]


def test_a_word_typed_twice_comes_first(harness, lst):
    out = run(harness, f"list:{lst}", "learn:halten", "complete:hal", "learn:halten",
              "complete:hal", "learnn:Haltestelle:5", "complete:hal")
    assert out[1:] == [
        "learn halten 1",
        "complete hal: hallo halt halten",             # once is not enough
        "learn halten 1",
        "complete hal: halten hallo halt",
        "complete hal: Haltestelle halten hallo",      # the most typed first
    ]


def test_a_new_word_is_offered_once_typed_twice(harness, lst):
    out = run(harness, f"list:{lst}", "learn:Halligalli", "complete:hal:5", "learn:halligalli",
              "complete:hal:5", "learn:HALLIGALLI", "complete:hal:1")
    assert out[1:] == [
        "learn Halligalli 1",
        "complete hal: hallo halt halten Haltestelle",
        "learn halligalli 1",
        "complete hal: halligalli hallo halt halten Haltestelle",  # the case typed last
        "learn HALLIGALLI 1",
        "complete hal: HALLIGALLI",
    ]


def test_what_is_never_learned(harness, lst):
    out = run(harness, f"list:{lst}", "learn:ab", "learn:12345", "learn:hal1", "learn:",
              "learn:x-")
    assert out[1:] == ["learn ab 0", "learn 12345 0", "learn hal1 0", "learn  0", "learn x- 0"]


def test_learned_words_are_saved_privately_and_read_back(harness, lst, tmp_path):
    path = tmp_path / "data" / "stevia" / "words" / "de.learned"
    out = run(harness, f"list:{lst}", "learnn:halten:3", "learnn:Zwiebelringe:2",
              "learn:einmal", f"save:{path}")
    assert out[-1] == "save ok"
    assert sorted(path.read_text().splitlines()) == ["1\teinmal", "2\tZwiebelringe", "3\thalten"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    out = run(harness, f"list:{lst}", f"learned:{path}", "complete:hal", "complete:zwie")
    assert out[1:] == ["learned ok 23", "complete hal: halten hallo halt",
                       "complete zwie: Zwiebelringe"]


def test_learned_words_survive_a_new_list(harness, lst, tmp_path):
    other = tmp_path / "other.words"
    other.write_text("halten\nhallo\n")
    out = run(harness, f"list:{lst}", "learnn:Zwiebelringe:2", "learnn:halt:2",
              f"list:{other}", "complete:hal", "complete:zwi")
    # halt is no longer listed: a word of the user's own now
    assert out[1:] == ["list ok 4", "complete hal: halt halten hallo",
                       "complete zwi: Zwiebelringe"]


def test_a_broken_learned_file(harness, lst, tmp_path):
    path = tmp_path / "de.learned"
    path.write_text("x\thalten\nnotab\n3\n4\tzwiebel\n99999999999\tbig\n2x\tbad\n\tnone\n"
                    "0\tnull\n")
    out = run(harness, f"list:{lst}", f"learned:{path}", "complete:zwi", "complete:hal")
    assert out[1:] == ["learned ok 22", "complete zwi: zwiebel",
                       "complete hal: hallo halt halten"]


def test_a_missing_learned_file_is_none_an_unreadable_one_an_error(harness, lst, tmp_path):
    d = tmp_path / "dir.learned"
    d.mkdir()
    assert run(harness, f"list:{lst}", f"learned:{tmp_path}/none", f"learned:{d}")[1:] == [
        "learned ok 21", "learned error 21"]


def test_saving_where_no_directory_can_be(harness, lst, tmp_path):
    (tmp_path / "file").write_text("")
    out = run(harness, f"list:{lst}", "learn:halten", f"save:{tmp_path}/file/sub/de.learned")
    assert out[-1].startswith("save Creating ") and out[-1].endswith("failed: Not a directory")


def test_forgetting_keeps_the_list(harness, lst):
    out = run(harness, f"list:{lst}", "learnn:Zwiebelringe:2", "learnn:halten:2", "forget",
              "complete:hal", "complete:zwi")
    assert out[1:] == ["forget 21", "complete hal: hallo halt halten", "complete zwi: (none)"]


def test_the_number_of_new_words_is_bounded(harness, lst):
    # POS_WORDS_MAX_LEARNED = 5000: once full, a new word still gets in and pushes out one of
    # the least typed, so the count of words stays
    out = run(harness, f"list:{lst}", "learnmany:5000:2", "learn:Zwiebelringe", "learnmany:0:0",
              "learn:Zwiebelringe", "complete:zwi", "learn:Zwiebelkuchen", "complete:zwi",
              "learnmany:0:0")
    assert out[1:] == ["n 5021", "learn Zwiebelringe 1", "n 5021", "learn Zwiebelringe 1",
                       "complete zwi: Zwiebelringe", "learn Zwiebelkuchen 1",
                       "complete zwi: Zwiebelringe", "n 5021"]


def test_eviction_takes_the_least_typed(harness, lst):
    out = run(harness, f"list:{lst}", "learnmany:4999:3", "learnn:Zwiebelringe:2",
              "learnn:Zwiebelkuchen:3", "complete:zwi")
    assert out[-1] == "complete zwi: Zwiebelkuchen"


@pytest.fixture
def lists(tmp_path):
    d = tmp_path / "lists"
    d.mkdir()
    for lang in ("de", "en", "fr"):
        (d / f"{lang}.words").write_text("word\n")
    (d / "pt.words").mkdir()
    return d


@pytest.mark.parametrize("lang,region,session,picked", [
    ("en", "us", "de_DE.UTF-8,de_DE,de,C", "de"),      # the plain US layout: the session's
    ("en", "US", "de_DE.UTF-8", "de"),
    ("en", "-", "fr_FR@euro", "fr"),
    ("en", "us", "C.UTF-8,POSIX,C", "en"),
    ("en", "us", "it_IT.UTF-8,ru", "en"),               # no list for those
    ("en", "us", "pt_PT.UTF-8", "en"),                  # a directory is no list
    ("en", "us", "_x,.y,@z", "en"),
    ("en", "gb", "de_DE.UTF-8", "en"),                  # a layout of its own
    ("de", "de", "fr_FR.UTF-8", "de"),
    ("fr", "ch", "de_DE.UTF-8", "fr"),
    ("it", "it", "de_DE.UTF-8", "(null)"),
    ("", "us", "de_DE.UTF-8", "(null)"),
    ("../lists/de", "de", "C", "(null)"),               # never a path
])
def test_the_language_of_a_layout(harness, lists, lang, region, session, picked):
    assert run(harness, f"pick:{lists}:{lang}:{region}:{session}") == [f"pick {picked}"]


def test_the_language_without_session_languages(harness, lists):
    assert run(harness, f"picknull:{lists}:en:us") == ["pick en"]
