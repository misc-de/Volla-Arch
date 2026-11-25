# plinius-speechd: the system language from the locale, finding and choosing the voice
# (voice.json files, the choice per language, the default voice), cutting a text into clauses and
# sentences as Piper's phonemizer does (with a stand-in phonemizer, and with the host's real
# espeak-ng), and the service as it runs: Speak/Stop/Prepare over a scripted bus, the queue, the
# player (pw-cat stub on PATH), Stop mid-speech, the settings, the exit after keep-voice; Piper's
# config (phoneme ids, vowel clusters, inference values). ONNX Runtime is replaced by
# fakes/onnxruntime_c_api.h.

import functools
import json
import os
import shutil
import struct
import subprocess
import sys

import pytest

from dhw_harness import PKG, build_harness, kv, run, sandboxed_source

SRC = PKG / "plinius-speech" / "plinius-speechd.c"
PKGS = ("gio-unix-2.0", "json-glib-1.0", "espeak-ng")

# A Piper 1.5 config: markers, some phonemes, a vowel cluster
PIPER_MAP = {"_": [0], "^": [1], "$": [2], " ": [3], ".": [10], "a": [14], "ɪ": [74], "aɪ": [160],
             "n": [26], "d": [17], "e": [18], "l": [24], "ˈ": [120], "?": [13], "\u0303": [141]}


def piper_config(espeak="de", rate=22050, inference=None, map_=None, drop=()):
    c = {"audio": {"sample_rate": rate, "quality": "medium"}, "espeak": {"voice": espeak},
         "inference": inference if inference is not None else {"noise_scale": 0.5, "length_scale": 1.0,
                                                               "noise_w": 0.333},
         "phoneme_id_map": dict(map_ if map_ is not None else PIPER_MAP), "piper_version": "1.5.0"}
    for k in drop:
        c.pop(k)
    return c


def make_voice(root, vid, language, default=False, locale=None, model="m.onnx", config="m.onnx.json",
               files=True, extra=None, espeak=None, piper=None):
    d = root / vid
    d.mkdir(parents=True, exist_ok=True)
    data = {"id": vid, "name": vid.title(), "language": language, "locale": locale or language,
            "model": model, "config": config, "quality": "medium", "license": "CC0-1.0", "default": default}
    data.update(extra or {})
    (d / "voice.json").write_text(json.dumps(data))
    if files:
        (d / model.replace("/", "_")).write_text("")
        (d / config.replace("/", "_")).write_text(json.dumps(piper or piper_config(espeak or language)))
    return d


# ---- Functions ----

UNIT = r"""
#include <glib.h>
#include <stdio.h>
#define main real_main
#include "plinius-speechd.c"
#undef main

static const char *fake_phonemize(const char *clause, gpointer data)
{
	GString *out = data;
	g_string_printf(out, "<%s>", clause);
	if (!strcmp(clause, "silent"))
		g_string_truncate(out, 0);   /* a clause espeak-ng has nothing for */
	return out->str;
}

static void print_sentences(GArray *cp, GArray *lens)
{
	printf("sentences=");
	gsize at = 0;
	for (guint i = 0; i < lens->len; i++) {
		gint32 n = g_array_index(lens, gint32, i);
		g_autoptr(GString) s = g_string_new(NULL);
		for (gint32 k = 0; k < n; k++)
			g_string_append_unichar(s, g_array_index(cp, gint32, at + k));
		at += n;
		printf("%s|", s->str);
	}
	printf("\n");
}

int main(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		g_auto(GStrv) a = g_strsplit(argv[i], ":", 5);
		if (!strcmp(a[0], "lang")) {
			g_autofree char *l = language_of_locale(a[1] && *a[1] ? a[1] : NULL);
			printf("lang=%s\n", l);
		} else if (!strcmp(a[0], "system-lang")) {
			g_autofree char *l = system_language();
			printf("system-lang=%s\n", l);
		} else if (!strcmp(a[0], "phon")) {
			/* phon:<text>, the text may hold colons */
			g_autoptr(GArray) cp = g_array_new(FALSE, FALSE, sizeof(gint32));
			g_autoptr(GArray) lens = g_array_new(FALSE, FALSE, sizeof(gint32));
			g_autoptr(GString) buf = g_string_new(NULL);
			phonemize_text(argv[i] + 5, fake_phonemize, buf, cp, lens);
			print_sentences(cp, lens);
		} else if (!strcmp(a[0], "espeak")) {
			Speech s = {0};
			if (!worker_espeak(&s, a[1])) {
				printf("espeak=failed\n");
				continue;
			}
			g_autoptr(GArray) cp = g_array_new(FALSE, FALSE, sizeof(gint32));
			g_autoptr(GArray) lens = g_array_new(FALSE, FALSE, sizeof(gint32));
			g_autoptr(GString) buf = g_string_new(NULL);
			phonemize_text(a[2], espeak_phonemize, buf, cp, lens);
			print_sentences(cp, lens);
			/* The same voice again keeps espeak-ng's voice */
			printf("again=%d\n", worker_espeak(&s, a[1]));
		} else if (!strcmp(a[0], "voices")) {
			GList *v = voices_installed(a[1]);
			printf("voices=");
			for (GList *l = v; l; l = l->next)
				printf("%s/%s/%d,", ((Voice *)l->data)->id, ((Voice *)l->data)->language,
				       ((Voice *)l->data)->is_default);
			printf("\n");
			g_list_free_full(v, (GDestroyNotify)voice_free);
		} else if (!strcmp(a[0], "pick")) {
			/* pick:<dir>:<language>,<locale>:<chosen>:<wanted> */
			GList *v = voices_installed(a[1]);
			g_auto(GStrv) ll = g_strsplit(a[2], ",", 2);
			Voice *p = voice_pick(v, ll[0], ll[1] && *ll[1] ? ll[1] : NULL, *a[3] ? a[3] : NULL,
					      *a[4] ? a[4] : NULL);
			printf("pick=%s\n", p ? p->id : "(none)");
			g_list_free_full(v, (GDestroyNotify)voice_free);
		} else if (!strcmp(a[0], "territory")) {
			g_autofree char *t = territory_of_locale(a[1] && *a[1] ? a[1] : NULL);
			printf("territory=%s\n", t);
		} else if (!strcmp(a[0], "config")) {
			VoiceConfig *c = voice_config_load(a[1]);
			if (!c) {
				printf("config=none\n");
				continue;
			}
			printf("config espeak=%s rate=%d noise=%.3f length=%.2f noise_w=%.3f phonemes=%u\n", c->espeak,
			       c->sample_rate, c->noise_scale, c->length_scale, c->noise_w, g_hash_table_size(c->ids));
			if (a[2]) {
				/* config:<path>:<phonemes> -> the ids of one sentence */
				g_autoptr(GArray) cp = g_array_new(FALSE, FALSE, sizeof(gint32));
				for (const char *q = a[2]; *q; q = g_utf8_next_char(q)) {
					gint32 u = g_utf8_get_char(q);
					g_array_append_val(cp, u);
				}
				g_autoptr(GArray) ids = phonemes_to_ids(c, (gint32 *)cp->data, cp->len);
				printf("ids=");
				for (guint k = 0; k < ids->len; k++)
					printf("%ld,", (long)g_array_index(ids, gint64, k));
				printf("\n");
			}
			voice_config_free(c);
		} else if (!strcmp(a[0], "peak")) {
			/* peak:<v1,v2,...> */
			g_auto(GStrv) vs = g_strsplit(a[1], ",", -1);
			guint n = g_strv_length(vs);
			float *f = g_new(float, n);
			for (guint k = 0; k < n; k++)
				f[k] = g_ascii_strtod(vs[k], NULL);
			normalize_peak(f, n);
			printf("peak=");
			for (guint k = 0; k < n; k++)
				printf("%.3f,", f[k]);
			printf("\n");
			g_free(f);
		} else if (!strcmp(a[0], "marks")) {
			g_autoptr(GString) m = g_string_new(a[1]);
			strip_language_marks(m);
			printf("marks=%s\n", m->str);
		} else if (!strcmp(a[0], "rate")) {
			printf("rate=%.2f\n", clamp_rate(g_ascii_strtod(a[1], NULL)));
		}
	}
	return 0;
}
"""


@pytest.fixture(scope="module")
def unit(tmp_path_factory):
    d = tmp_path_factory.mktemp("speech-unit")
    sandboxed_source(SRC, d, [("/usr/share/plinius-speech/voices", d / "voices")])
    exe = build_harness(d, "speech_unit", UNIT, pkgs=PKGS)
    return functools.partial(run, exe)


@pytest.mark.parametrize("locale,lang", [
    ("de_DE.UTF-8", "de"), ("pt_BR@euro", "pt"), ("en", "en"), ("RU_ru", "ru"),
    ("C", "en"), ("POSIX", "en"), ("C.UTF-8", "en"), ("", "en"),
])
def test_language_of_locale(unit, locale, lang):
    assert kv(unit(f"lang:{locale}").stdout)["lang"] == lang


def test_system_language_follows_the_session(unit):
    env = dict(os.environ, LANGUAGE="fr_FR:de", LC_ALL="", LANG="de_DE.UTF-8")
    assert kv(unit("system-lang", env=env).stdout)["system-lang"] == "fr"


def test_voices_installed_skips_broken_ones(unit, tmp_path):
    make_voice(tmp_path, "de-b", "de", default=True)
    make_voice(tmp_path, "de-a", "de")
    make_voice(tmp_path, "en-a", "en", default=True)
    make_voice(tmp_path, "no-model", "de", files=False)          # files missing
    make_voice(tmp_path, "slash", "de", model="../m.onnx")         # outside its directory
    make_voice(tmp_path, "no-config", "de", extra={"config": 5})   # wrong type
    (tmp_path / "bad-json").mkdir()
    (tmp_path / "bad-json/voice.json").write_text("{")
    (tmp_path / "array").mkdir()
    (tmp_path / "array/voice.json").write_text("[]")
    (tmp_path / "empty").mkdir()
    make_voice(tmp_path, "not-default", "en", extra={"default": "yes"})
    out = kv(unit(f"voices:{tmp_path}", f"voices:{tmp_path / 'missing'}").stdout)
    # sorted by id; a missing directory has none
    assert out["voices"] == ""
    first = unit(f"voices:{tmp_path}").stdout
    assert kv(first)["voices"] == "de-a/de/0,de-b/de/1,en-a/en/1,not-default/en/0,"


@pytest.mark.parametrize("lang,chosen,wanted,picked", [
    ("de,de_DE", "", "", "de-b"),        # the default voice of the language
    ("de,", "de-a", "", "de-a"),         # the choice for the language
    ("de,de_DE", "en-a", "", "de-b"),    # a choice of another language does not count
    ("de,de_DE", "gone", "", "de-b"),    # a chosen voice that is gone
    ("fr,fr_FR", "", "", "fr-a"),        # no default voice: the first
    ("it,it_IT", "", "", "(none)"),      # no voice for the language
    ("de,de_DE", "de-a", "en-a", "en-a"),  # the preview's voice, any language
    ("de,", "", "gone", "(none)"),       # a preview voice that is not installed
    ("pt,pt_BR", "", "", "pt-faber"),    # the default of the locale before the language's
    ("pt,pt_PT", "", "", "pt-tugao"),
    ("pt,pt_AO", "", "", "pt-faber"),    # another locale: the language's first default
    ("en,en_GB", "", "", "en-b"),        # a voice of the locale, though not a default
    ("pt,pt_BR", "pt-tugao", "", "pt-tugao"),  # the choice still wins
])
def test_voice_pick(unit, tmp_path, lang, chosen, wanted, picked):
    make_voice(tmp_path, "de-a", "de", locale="de_DE")
    make_voice(tmp_path, "de-b", "de", default=True, locale="de_DE")
    make_voice(tmp_path, "en-a", "en", default=True, locale="en_US")
    make_voice(tmp_path, "en-b", "en", locale="en_GB")
    make_voice(tmp_path, "fr-b", "fr", locale="fr_FR")
    make_voice(tmp_path, "fr-a", "fr", locale="fr_FR")
    make_voice(tmp_path, "pt-tugao", "pt", default=True, locale="pt_PT")
    make_voice(tmp_path, "pt-faber", "pt", default=True, locale="pt_BR")
    assert kv(unit(f"pick:{tmp_path}:{lang}:{chosen}:{wanted}").stdout)["pick"] == picked


def test_locale_without_field_is_the_language(unit, tmp_path):
    d = make_voice(tmp_path, "x", "de")
    data = json.loads((d / "voice.json").read_text())
    del data["locale"]
    (d / "voice.json").write_text(json.dumps(data))
    assert kv(unit(f"pick:{tmp_path}:de,de::").stdout)["pick"] == "x"


@pytest.mark.parametrize("locale,territory", [
    ("pt_br.UTF-8", "pt_BR"), ("de_DE@euro", "de_DE"), ("de", "de"), ("C", "en"), ("", "en"),
])
def test_territory_of_locale(unit, locale, territory):
    assert kv(unit(f"territory:{locale}").stdout)["territory"] == territory


def test_piper_config_and_ids(unit, tmp_path):
    path = tmp_path / "v.onnx.json"
    path.write_text(json.dumps(piper_config()))
    out = unit(f"config:{path}:ˈdaɪn.", f"config:{path}:aɪ", f"config:{path}:xa", f"config:{path}:").stdout
    assert "config espeak=de rate=22050 noise=0.500 length=1.00 noise_w=0.333 phonemes=15" in out
    ids = [line[4:].rstrip(",").split(",") for line in out.splitlines() if line.startswith("ids=")]
    # "^" "_", every phoneme and "_", "$"; the cluster "aɪ" is one id; unknown "x" is left out
    assert ids[0] == "1 0 120 0 17 0 160 0 26 0 10 0 2".split()
    assert ids[1] == "1 0 160 0 2".split()
    assert ids[2] == "1 0 14 0 2".split()
    assert ids[3] == "1 0 2".split()


def test_piper_config_defaults_and_errors(unit, tmp_path):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(piper_config(inference={"noise_scale": 1})))   # integers count too
    cases = {
        "no-map": piper_config(drop=("phoneme_id_map",)),
        "no-espeak": piper_config(drop=("espeak",)),
        "bad-rate": piper_config(rate=100),
        "no-bos": piper_config(map_={"_": [0], "$": [2]}),
        "list": [],
    }
    for name, c in cases.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(c))
    (tmp_path / "broken.json").write_text("{")
    m = piper_config()
    m["phoneme_id_map"]["z"] = 5                                              # not a list: skipped
    m["inference"]["noise_w"] = "x"                                           # wrong type: default
    (tmp_path / "odd.json").write_text(json.dumps(m))
    out = unit(f"config:{ok}", *[f"config:{tmp_path / n}.json" for n in cases],
               f"config:{tmp_path / 'broken.json'}", f"config:{tmp_path / 'missing.json'}",
               f"config:{tmp_path / 'odd.json'}").stdout.splitlines()
    assert out[0] == "config espeak=de rate=22050 noise=1.000 length=1.00 noise_w=0.800 phonemes=15"
    assert out[1:8] == ["config=none"] * 7
    assert out[8] == "config espeak=de rate=22050 noise=0.500 length=1.00 noise_w=0.800 phonemes=15"


def test_peak_normalization(unit):
    assert kv(unit("peak:0.5,-0.25,0").stdout)["peak"] == "0.950,-0.475,0.000,"
    # Near silence is not blown up beyond 100 times
    assert kv(unit("peak:0.001,0").stdout)["peak"] == "0.095,0.000,"


@pytest.mark.parametrize("text,stripped", [
    ("(en)hˈɛloʊ(de) ja", "hˈɛloʊ ja"), ("a(b", "a(b"), ("()", "()"), ("(x1)", "(x1)"), ("(en-us)", ""),
])
def test_language_marks(unit, text, stripped):
    assert unit(f"marks:{text}").stdout == f"marks={stripped}\n"


@pytest.mark.parametrize("value,rate", [("1.2", 1.2), ("0.1", 0.5), ("9", 2.0), ("nan", 1.2)])
def test_rate_is_clamped(unit, value, rate):
    assert float(kv(unit(f"rate:{value}").stdout)["rate"]) == rate


@pytest.mark.parametrize("text,sentences", [
    ("Daniel Müller ist dran", "<Daniel Müller ist dran>.|"),               # a "." is added
    ("Hello. How are you?", "<Hello>.|<How are you>?|"),
    ("Yes, no; maybe: fine!", "<Yes>, <no>; <maybe>: <fine>!|"),
    ("Wait… now", "<Wait>.|<now>.|"),                                         # an ellipsis ends one
    ("  ...  ", ""),                                                          # only punctuation
    ("", ""),
    ("One,", "<One>.|"),                                                      # a comma at the end
    ("One, ", "<One>.|"),
    ("silent. Word", "<Word>.|"),                                             # a clause without phonemes
    ("a\nb", "<a b>.|"),                                                      # line ends are spaces
])
def test_clauses_and_sentences(unit, text, sentences):
    out = unit(f"phon:{text}").stdout
    assert out.split("sentences=", 1)[1].rstrip("\n") == sentences


def test_espeak_phonemizes_german(unit):
    out = unit("espeak:de:Daniel Müller ruft an. Hallo").stdout
    sentences = out.split("sentences=", 1)[1].split("\n")[0].split("|")
    assert len(sentences) == 3 and sentences[2] == ""
    first, second = sentences[0], sentences[1]
    # IPA, a full stop each, no spaces at the edges
    assert first.endswith(".") and second.endswith(".")
    # NFD: "ʏ" is one codepoint, decomposed letters stay apart
    assert "ʏ" in first and "ˈ" in first and first == first.strip()
    assert kv(out)["again"] == "1"


def test_espeak_unknown_voice(unit):
    assert kv(unit("espeak:xx-nonexistent:Hallo").stdout)["espeak"] == "failed"


# ---- The service ----

SERVICE = r"""
#define G_DISABLE_CAST_CHECKS
#include <glib.h>
#include <gio/gio.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include "dhw_scripted_gdbus.h"

static void script_loop(GMainLoop *loop);
#define g_main_loop_run script_loop
#define main real_main
#include "plinius-speechd.c"
#undef main
#undef g_main_loop_run

static char **script;
static Speech *S;

static gboolean quit_loop(gpointer loop)
{
	g_main_loop_quit(loop);
	return G_SOURCE_REMOVE;
}

static void run_for(unsigned ms)
{
	GMainLoop *loop = g_main_loop_new(NULL, FALSE);
	g_timeout_add(ms, quit_loop, loop);
	(g_main_loop_run)(loop);
	g_main_loop_unref(loop);
}

#define PATH "/org/plinius/Speech1"
#define IFACE_ "org.plinius.Speech1"

static void command(const char *a)
{
	g_auto(GStrv) w = g_strsplit(a, ":", 2);
	if (!strcmp(w[0], "Speak") || !strcmp(w[0], "Stop") || !strcmp(w[0], "Prepare") || !strcmp(w[0], "Bogus")) {
		/* Method::1.42:params (the sender's unique name starts with a colon) */
		const char *sep = strchr(w[1] + 1, ':');
		g_autofree char *sender = sep ? g_strndup(w[1], sep - w[1]) : g_strdup(w[1]);
		sg_invoke(PATH, IFACE_, sender, w[0], sep ? sep + 1 : NULL);
	} else if (!strcmp(w[0], "get")) {
		sg_get(PATH, IFACE_, w[1]);
	} else if (!strcmp(w[0], "run")) {
		run_for(atoi(w[1]));
	} else if (!strcmp(w[0], "wait-idle")) {
		gint64 end = g_get_monotonic_time() + 10 * G_TIME_SPAN_SECOND;
		while (g_get_monotonic_time() < end && (S->current || S->queue.length))
			g_main_context_iteration(NULL, TRUE);
		printf("idle current=%d queued=%u\n", S->current != NULL, S->queue.length);
	} else if (!strcmp(w[0], "wait-player")) {
		/* until the current job's player runs */
		gint64 end = g_get_monotonic_time() + 10 * G_TIME_SPAN_SECOND;
		while (g_get_monotonic_time() < end) {
			g_mutex_lock(&S->lock);
			gboolean up = S->current && S->current->player;
			g_mutex_unlock(&S->lock);
			if (up)
				break;
			g_main_context_iteration(NULL, FALSE);
			g_usleep(1000);
		}
	} else if (!strcmp(w[0], "delay")) {
		fake_ort_delay_ms = atoi(w[1]);
	} else if (!strcmp(w[0], "value")) {
		fake_ort_value = g_ascii_strtod(w[1], NULL);
	} else if (!strcmp(w[0], "fail")) {
		/* fail:env|memory|api|tensor|run|empty */
		int *flags[] = {&fake_ort_fail_env, &fake_ort_fail_memory, &fake_ort_no_api, &fake_ort_fail_tensor,
				&fake_ort_fail_run, &fake_ort_empty};
		const char *names[] = {"env", "memory", "api", "tensor", "run", "empty"};
		for (int k = 0; k < 6; k++)
			if (!strcmp(w[1], names[k]))
				*flags[k] = 1;
	} else if (!strcmp(w[0], "heal")) {
		fake_ort_fail_env = fake_ort_fail_memory = fake_ort_no_api = fake_ort_fail_tensor = fake_ort_fail_run = 0;
	} else if (!strcmp(w[0], "state")) {
		printf("state current=%d queued=%u keep=%d loaded=%s sessions=%d released=%d threads=%d runs=%d "
		       "values=%d model=%s scales=%.3f,%.3f,%.3f ids=%s settings=%d\n",
		       S->current != NULL, S->queue.length, S->keep_source != 0, S->loaded_id ? S->loaded_id : "-",
		       fake_ort_sessions, fake_ort_released, fake_ort_threads, fake_ort_runs, fake_ort_values,
		       fake_ort_model ? fake_ort_model : "-", fake_ort_scales[0], fake_ort_scales[1], fake_ort_scales[2],
		       fake_ort_ids && fake_ort_ids->len ? fake_ort_ids->str : "-", S->settings != NULL);
	} else if (!strcmp(w[0], "keep-timeout")) {
		/* keep-voice is at least 10 s: the timer comes sooner */
		g_source_remove(S->keep_source);
		S->keep_source = g_timeout_add(50, on_keep_timeout, S);
	} else if (!strcmp(w[0], "wait-exit")) {
		(g_main_loop_run)(S->loop);
		printf("loop quit\n");
	} else if (!strcmp(w[0], "sigterm")) {
		raise(SIGTERM);
	} else if (!strcmp(w[0], "register-fails")) {
		sg.register_fails = TRUE;
	} else if (!strcmp(w[0], "no-name")) {
		sg.no_bus = TRUE;
	} else {
		g_error("unknown command %s", a);
	}
	fflush(stdout);
}

static gboolean started;
static gboolean startup_done(gpointer loop)
{
	started = TRUE;
	g_main_loop_quit(loop);
	return G_SOURCE_REMOVE;
}

static void script_loop(GMainLoop *loop)
{
	g_idle_add_full(G_PRIORITY_LOW, startup_done, loop, NULL);
	(g_main_loop_run)(loop);
	if (!started) {
		printf("quit at start\n");
		return;
	}
	S = sg.objects->len ? ((SgObject *)sg.objects->pdata[0])->data : NULL;
	if (!S) {
		printf("not registered\n");
		return;
	}
	for (; script && *script; script++)
		command(*script);
}

int main(int argc, char **argv)
{
	g_autofree char *pid = g_strdup_printf("%d", (int)getpid());
	g_setenv("FAKE_PW_PARENT", pid, TRUE);
	sg_init();
	script = argv + 1;
	while (*script && strcmp(*script, "start")) {
		command(*script);
		script++;
	}
	if (*script)
		script++;
	int r = real_main(1, argv);
	printf("exit %d\n", r);
	return r;
}
"""

# pw-cat: logs its arguments and how many float samples came on stdin; "hang" stays after the
# end of its input until killed (or the service ends), "fail" exits 1
PW_CAT = """#!{python}
import os, select, sys
log = os.environ["FAKE_PW_LOG"]
with open(log, "a") as f:
    f.write("start " + " ".join(sys.argv[1:]) + "\\n")
data = sys.stdin.buffer.read()
with open(log, "a") as f:
    f.write("samples %d first %s\\n" % (len(data) // 4, data[:4].hex()))
mode = os.environ.get("FAKE_PW_MODE", "")
if mode == "fail":
    sys.exit(1)
if mode == "hang":
    p = select.poll()
    p.register(os.pidfd_open(int(os.environ["FAKE_PW_PARENT"])), select.POLLIN)
    p.poll()
"""


@pytest.fixture(scope="module")
def service_built(tmp_path_factory):
    d = tmp_path_factory.mktemp("speech-service")
    sandboxed_source(SRC, d, [("/usr/share/plinius-speech/voices", d / "voices")])
    exe = build_harness(d, "speech_service", SERVICE, pkgs=PKGS)
    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("no glib-compile-schemas")
    (d / "schemas").mkdir()
    shutil.copy(PKG / "plinius-speech" / "org.plinius.speech.gschema.xml", d / "schemas")
    subprocess.run(["glib-compile-schemas", str(d / "schemas")], check=True)
    (d / "no-schemas").mkdir()
    (d / "bin").mkdir()
    (d / "bin/pw-cat").write_text(PW_CAT.format(python=sys.executable))
    (d / "bin/pw-cat").chmod(0o755)
    return d, exe


@pytest.fixture
def service(service_built):
    d, exe = service_built
    for sub in ("voices", "config"):
        shutil.rmtree(d / sub, ignore_errors=True)
    make_voice(d / "voices", "de-thorsten", "de", default=True, locale="de_DE")
    make_voice(d / "voices", "de-kerstin", "de", locale="de_DE", piper=piper_config(rate=16000))
    make_voice(d / "voices", "en-amy", "en", default=True, espeak="en-us", locale="en_US")
    make_voice(d / "voices", "de-broken", "de", model="broken.onnx", locale="de_DE")
    make_voice(d / "voices", "de-noconfig", "de", piper={"espeak": {"voice": "de"}}, locale="de_DE")
    make_voice(d / "voices", "de-noespeak", "de", espeak="xx-none", locale="de_DE")
    (d / "config/glib-2.0/settings").mkdir(parents=True)
    (d / "pw.log").write_text("")

    def binary(*args, settings="", schema=True, mode="", lang="de_DE", path=True, **kw):
        (d / "config/glib-2.0/settings/keyfile").write_text(f"[org/plinius/speech]\n{settings}\n")
        env = dict(os.environ, GSETTINGS_SCHEMA_DIR=str(d / ("schemas" if schema else "no-schemas")),
                   XDG_DATA_DIRS=str(d / "no-schemas"), GSETTINGS_BACKEND="keyfile",
                   XDG_CONFIG_HOME=str(d / "config"), FAKE_PW_LOG=str(d / "pw.log"),
                   FAKE_PW_MODE=mode, LANGUAGE=lang,
                   PATH=str(d / "bin") if path else str(d / "no-schemas"))
        return run(exe, *args, env=env, **kw)
    return d, binary


def lines(out, prefix):
    return [line for line in out.splitlines() if line.startswith(prefix)]


def pw_log(d):
    return (d / "pw.log").read_text().splitlines()


SPEAK = "Speak::1.42:('Daniel Müller ruft an', @a{sv} {})"


def state(out, n=-1):
    return dict(x.split("=", 1) for x in lines(out, "state ")[n].split()[1:])


def test_speak_plays_every_sentence(service):
    d, binary = service
    out = binary("start", "Speak::1.42:('Hallo. Wie geht es?', @a{sv} {})", "wait-idle", "state").stdout
    assert "own-name session org.plinius.Speech1" in out
    assert "reply Speak ok (uint32 1,)" in out or "reply Speak ok (1,)" in out
    sig = lines(out, "signal ")
    assert [x.split()[1] for x in sig] == [":1.42", ":1.42"]          # to the caller only
    assert sig[0].endswith("Started(1,)") and sig[1].endswith("Finished(1, true)")
    st = state(out)
    assert st["model"] == "m.onnx" and st["loaded"] == "de-thorsten"   # the default German voice
    assert st["threads"] == "4" and st["runs"] == "2"                  # one run per sentence
    assert st["values"] == "0"                                         # every tensor released
    # Piper's noise values; the length scale over the default rate 1.2
    assert st["scales"] == "0.500,0.833,0.333"
    assert st["keep"] == "1"                                           # idle: the exit timer runs
    runs = st["ids"].rstrip(";").split(";")
    assert len(runs) == 2 and all(r.startswith("1,0,") and r.endswith(",2") for r in runs)
    log = pw_log(d)
    assert log[0].split()[1:] == ["--playback", "--raw", "--rate", "22050", "--channels", "1", "--format",
                                  "f32", "--media-role", "Notification", "-P",
                                  '{', 'application.name', '=', '"Speech"', 'node.description', '=',
                                  '"Speech"', 'media.role', '=', '"Notification"', '}', "-"]
    n = sum(len(r.split(",")) for r in runs) * 10 + int(0.15 * 22050)  # samples + one gap
    # Every sentence normalized to the peak
    assert log[1] == "samples %d first %s" % (n, struct.pack("<f", 0.95).hex())


def test_options_voice_rate_target_role(service):
    d, binary = service
    out = binary("start", "Speak::1.42:('Hi', {'voice': <'en-amy'>, 'rate': <3.0>, 'target': "
                 "<'bluez_output.X'>, 'role': <'Ringtone'>})", "wait-idle", "state").stdout
    st = state(out)
    assert st["loaded"] == "en-amy" and st["scales"] == "0.500,0.500,0.333"   # clamped to 2.0
    args = pw_log(d)[0]
    assert "--media-role Ringtone --target bluez_output.X" in args
    assert 'media.role = "Ringtone"' in args


def test_bad_role_falls_back(service):
    d, binary = service
    binary("start", "Speak::1.42:('Hi', {'role': <'x\" }'>})", "wait-idle")
    assert "--media-role Notification" in pw_log(d)[0]


def test_choice_and_rate_from_the_settings(service):
    d, binary = service
    out = binary("start", SPEAK, "wait-idle", "state", "get:Voice", "get:Language",
                 settings="voices={'de': 'de-kerstin'}\nrate=0.8").stdout
    st = state(out)
    assert st["loaded"] == "de-kerstin" and st["scales"] == "0.500,1.250,0.333" and st["settings"] == "1"
    assert "--rate 16000" in pw_log(d)[0]                              # the voice's own rate
    assert "get Voice='de-kerstin'" in out and "get Language='de'" in out


def test_without_schema(service):
    d, binary = service
    st = state(binary("start", SPEAK, "wait-idle", "state", schema=False).stdout)
    assert st["settings"] == "0" and st["scales"] == "0.500,0.833,0.333" and st["loaded"] == "de-thorsten"


def test_no_voice_for_the_language(service):
    d, binary = service
    out = binary("start", SPEAK, "Prepare::1.42", "get:Voice", "Speak::1.42:('x', {'voice': <'nope'>})",
                 lang="it_IT").stdout
    assert "reply Speak error org.plinius.Speech1.Error.NoVoice: No voice for the language it" in out
    assert "reply Prepare error org.plinius.Speech1.Error.NoVoice" in out
    assert "get Voice=''" in out
    assert "Voice nope is not installed" in out
    assert not lines(out, "signal ")


def test_invalid_text(service):
    d, binary = service
    long = "a" * 5000
    out = binary("start", f"Speak::1.42:('{long}', @a{{sv}} {{}})", "Bogus::1.42:()", "get:Nothing").stdout
    assert "reply Speak error org.plinius.Speech1.Error.InvalidArgs" in out
    assert "UnknownMethod" in out and "get Nothing error Unknown property Nothing" in out


def test_queue_in_order_and_voice_reloaded(service):
    d, binary = service
    out = binary("start", SPEAK, "Speak::1.43:('Hi', {'voice': <'en-amy'>})", "state", "wait-idle",
                 "state").stdout
    sig = [x.split(None, 4)[4] for x in lines(out, "signal ")]
    assert sig == ["Started(1,)", "Finished(1, true)", "Started(2,)",
                   "Finished(2, true)"]
    assert state(out, 0)["queued"] == "1"
    last = state(out)
    assert last["sessions"] == "2" and last["released"] == "1"         # one voice loaded at a time
    assert [x.split()[0] for x in pw_log(d)] == ["start", "samples", "start", "samples"]


def test_stop_mid_speech_is_immediate(service):
    d, binary = service
    out = binary("delay:300", "start", "Speak::1.42:('Eins. Zwei. Drei. Vier. Fünf.', @a{sv} {})",
                 "wait-player", "run:450", "Stop::1.42:(uint32 1,)", "wait-idle", "state", mode="hang").stdout
    assert "Finished(1, false)" in out
    st = state(out)
    # The second run was terminated in the middle, nothing after it ran
    assert st["runs"] == "1" and len(st["ids"].rstrip(";").split(";")) == 2 and st["values"] == "0"
    # The killed player never logged the end of its input
    assert [x.split()[0] for x in pw_log(d)] == ["start"]


def test_stop_all_drops_the_queue(service):
    d, binary = service
    out = binary("delay:100", "start", SPEAK, "Speak::1.43:('Zwei', @a{sv} {})", "Prepare::1.44",
                 "Stop::1.50:(uint32 0,)", "wait-idle", "state", mode="hang").stdout
    assert "signal :1.43 /org/plinius/Speech1 org.plinius.Speech1 Finished(2, false)" in out
    assert "Finished(1, false)" in out
    assert state(out)["queued"] == "0"
    assert "Started(2" not in out and "(3," not in out   # Prepare sends nothing


def test_stop_of_another_id_leaves_it(service):
    d, binary = service
    out = binary("start", SPEAK, "Stop::1.42:(uint32 7,)", "wait-idle").stdout
    assert "Finished(1, true)" in out


def test_prepare_loads_without_signals(service):
    d, binary = service
    out = binary("start", "Prepare::1.42", "wait-idle", "state", SPEAK, "wait-idle", "state").stdout
    assert "reply Prepare ok ()" in out
    first, second = state(out, 0), state(out)
    assert first["sessions"] == "1" and first["loaded"] == "de-thorsten" and first["runs"] == "0"
    assert second["sessions"] == "1"                 # still loaded: no second load
    assert [x for x in lines(out, "signal ") if "Started" in x] == [
        "signal :1.42 /org/plinius/Speech1 org.plinius.Speech1 Started(2,)"]


@pytest.mark.parametrize("voice", ["de-broken", "de-noconfig"])
def test_voice_that_does_not_load(service, voice):
    d, binary = service
    out = binary("start", f"Speak::1.42:('x', {{'voice': <'{voice}'>}})", "wait-idle", "state").stdout
    assert "Finished(1, false)" in out
    assert state(out)["loaded"] == "-"
    assert pw_log(d) == []


@pytest.mark.parametrize("what", ["env", "memory", "api"])
def test_onnx_runtime_does_not_start(service, what):
    d, binary = service
    out = binary(f"fail:{what}", "start", SPEAK, "wait-idle", "heal", SPEAK, "wait-idle", "state").stdout
    # Not loaded the first time, the next request tries again
    assert "Finished(1, false)" in out and "Finished(2, true)" in out
    assert state(out)["loaded"] == "de-thorsten"


@pytest.mark.parametrize("what", ["tensor", "run", "empty"])
def test_model_run_fails(service, what):
    d, binary = service
    out = binary(f"fail:{what}", "start", SPEAK, "wait-idle", "state").stdout
    # The player gets nothing and ends normally
    assert "Finished(1, true)" in out
    assert pw_log(d)[1].startswith("samples 0 ")
    assert state(out)["values"] == "0"


def test_espeak_without_the_voice(service):
    d, binary = service
    out = binary("start", "Speak::1.42:('x', {'voice': <'de-noespeak'>})", "wait-idle").stdout
    assert "Finished(1, true)" in out          # nothing to say
    assert pw_log(d) == []


def test_player_fails_or_is_missing(service):
    d, binary = service
    out = binary("start", SPEAK, "wait-idle", mode="fail").stdout
    assert "Finished(1, false)" in out
    out = binary("start", SPEAK, "wait-idle", path=False).stdout
    assert "Finished(1, false)" in out


def test_nothing_to_say(service):
    d, binary = service
    out = binary("start", "Speak::1.42:(' ... ', @a{sv} {})", "wait-idle").stdout
    assert "Finished(1, true)" in out
    assert pw_log(d) == []


def test_exit_after_keep_voice_and_on_sigterm(service):
    d, binary = service
    out = binary("start", SPEAK, "wait-idle", "keep-timeout", "wait-exit").stdout
    assert "loop quit" in out.splitlines() and out.splitlines()[-1] == "exit 0"
    (d / "pw.log").write_text("")
    out = binary("delay:200", "start", SPEAK, "wait-player", "sigterm", "wait-exit", mode="hang").stdout
    # The service ends at once: the player is killed (maybe before it even logged its start), its
    # input never ended
    assert "loop quit" in out
    assert [x.split()[0] for x in pw_log(d)] in (["start"], [])


def test_bus_problems(service):
    d, binary = service
    assert "quit at start" in binary("no-name", "start").stdout
    assert "quit at start" in binary("register-fails", "start").stdout
