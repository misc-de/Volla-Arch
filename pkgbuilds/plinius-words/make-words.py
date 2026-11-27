#!/usr/bin/env python3
# Makes a word list for the keyboard's completion (stevia-plinius, pos-words.c) from a
# FrequencyWords list ("<word> <count>" per line, most frequent first, all in lower case).
#
# With a hunspell dictionary each word gets the case it is written in: kept if hunspell accepts
# it in lower case ("hallo"), capitalized if only that is accepted ("Bahnhof"), left out if
# neither is (names, typos and fragments of the subtitles the counts come from). Without one
# (Arch has none for Portuguese) the words stay as they are.
#
# Output: one word per line, most frequent first, NFC, words of at least MIN_LEN characters
# (a suggestion is longer than the three characters typed before it).
#
#   make-words.py <FrequencyWords list> <output> [<hunspell dictionary without .dic/.aff>]

import subprocess
import sys
import unicodedata

MIN_LEN = 4
APOSTROPHES = "'’"


def is_word(word):
    """Letters (with their marks), apostrophes and inner hyphens: what pos-words.c accepts."""
    if not word or word[0] in "-" + APOSTROPHES or word[-1] == "-":
        return False
    prev = ""
    for c in word:
        if c == "-" or c in APOSTROPHES:
            if prev == "-" or prev in APOSTROPHES:
                return False
        elif not (c.isalpha() or unicodedata.category(c).startswith("M")):
            return False
        prev = c
    return True


def is_stutter(word):
    """A word said twice, as subtitles write a stutter ("ich-ich", "no-no-no")."""
    parts = word.casefold().split("-")
    return len(parts) > 1 and len(set(parts)) == 1


def read_list(path):
    words = []
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            fields = line.split()
            if not fields:
                continue
            word = unicodedata.normalize("NFC", fields[0])
            if len(word) < MIN_LEN or not is_word(word) or is_stutter(word) or word in seen:
                continue
            seen.add(word)
            words.append(word)
    return words


def accepted(words, dictionary):
    """The words hunspell accepts as written (every token of one). -l -L prints the input lines
    with a misspelled word, one word per line here; unlike pipe mode (-a) it looks for no
    suggestions, which take minutes for 50000 words."""
    text = "".join(w + "\n" for w in words)
    out = subprocess.run(["hunspell", "-i", "utf-8", "-l", "-L", "-d", dictionary], input=text,
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    rejected = set(out.splitlines())
    return {w for w in words if w not in rejected}


def capitalize(word):
    return word[:1].upper() + word[1:]


def cased(words, dictionary):
    lower = accepted(words, dictionary)
    rest = [capitalize(w) for w in words if w not in lower]
    upper = accepted(rest, dictionary)
    result = []
    seen = set()
    for w in words:
        word = w if w in lower else capitalize(w)
        if (w in lower or word in upper) and word not in seen:
            seen.add(word)
            result.append(word)
    return result


def main(argv):
    if len(argv) not in (3, 4):
        print(f"usage: {argv[0]} <FrequencyWords list> <output> [<hunspell dictionary>]",
              file=sys.stderr)
        return 2
    words = read_list(argv[1])
    if len(argv) == 4:
        words = cased(words, argv[3])
    with open(argv[2], "w", encoding="utf-8") as f:
        f.write("".join(w + "\n" for w in words))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
