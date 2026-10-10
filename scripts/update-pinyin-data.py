"""Regenerate a pinned local reading table; never import or execute the wheel."""
import hashlib
import io
import json
from pathlib import Path
import unicodedata
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1] / "app" / "wordlists"
URL = "https://files.pythonhosted.org/packages/b9/7b/4cabc76fcc21c3c7d5c671d8783984d30ac9d3bb387c4ba784fca3cdfa3a/pypinyin-0.55.0-py2.py3-none-any.whl"
SHA256 = "d53b1e8ad2cdb815fb2cb604ed3123372f5a28c6f447571244aca36fc62a286f"


def reading(value):
    # Keep the diaeresis distinction: lü is lv, rather than lu.
    value = unicodedata.normalize("NFD", value.lower())
    value = value.replace("u\u0308", "v")
    return "".join(c for c in value if not unicodedata.combining(c))


def main():
    with urlopen(URL, timeout=30) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000 or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Pinned pypinyin archive checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        original = json.loads(archive.read("pypinyin/pinyin_dict.json"))
        table = {}
        polyphones = {}
        for key, value in original.items():
            values = list(dict.fromkeys(reading(s) for s in value.split(",")))
            values = [s for s in values if s.isascii() and s.isalpha()]
            if values:
                table[chr(int(key))] = values[0]
            if len(values) > 1:
                polyphones[chr(int(key))] = values
        phrases = {}
        for word, values in json.loads(archive.read("pypinyin/phrases_dict.json")).items():
            values = [reading(s[0]) for s in values]
            if (len(word) == len(values) and all(s.isascii() and s.isalpha() for s in values)
                    and any(s != table.get(c) for c, s in zip(word, values))):
                phrases[word] = values
        (ROOT / "pinyin-characters.json").write_text(
            json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        for name, values in (("pinyin-polyphones.json", polyphones), ("pinyin-phrases.json", phrases)):
            (ROOT / name).write_text(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))+"\n",
                                     encoding="utf-8", newline="\n")
        (ROOT / "LICENSE-pypinyin-MIT.txt").write_bytes(
            archive.read("pypinyin-0.55.0.dist-info/licenses/LICENSE.txt"))
    record = {"project": "https://github.com/mozillazg/python-pinyin", "version": "0.55.0",
              "url": URL, "sha256": SHA256, "license": "MIT", "characters": len(table),
              "generated_sha256": hashlib.sha256((ROOT / "pinyin-characters.json").read_bytes()).hexdigest(),
              "license_sha256": hashlib.sha256((ROOT / "LICENSE-pypinyin-MIT.txt").read_bytes()).hexdigest(),
              "polyphones": len(polyphones), "phrase_corrections": len(phrases),
              "polyphones_sha256": hashlib.sha256((ROOT / "pinyin-polyphones.json").read_bytes()).hexdigest(),
              "phrases_sha256": hashlib.sha256((ROOT / "pinyin-phrases.json").read_bytes()).hexdigest(),
              "conversion": "Lowercase, toneless, u-diaeresis as v. Primary table plus distinct character readings and longest-match phrase corrections. Homophones use preferred readings only."}
    (ROOT / "pinyin-source.json").write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")
    print(f"Generated {len(table)} primary readings, {len(polyphones)} polyphones, {len(phrases)} phrase corrections")


if __name__ == "__main__":
    main()
