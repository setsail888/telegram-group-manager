"""Fetch pinned OpenCC data only; never execute upstream code."""
import hashlib
import json
from pathlib import Path
import requests

REVISION = "556ed22496d650bd0b13b6c163be9814637970ae"  # ver.1.1.9
FILES = {"opencc-TSCharacters.txt": "data/dictionary/TSCharacters.txt", "LICENSE-OpenCC": "LICENSE"}


def main():
    root = Path(__file__).resolve().parents[1] / "app" / "wordlists"
    records, downloads = [], []
    for local, upstream in FILES.items():
        url = f"https://raw.githubusercontent.com/BYVoid/OpenCC/{REVISION}/{upstream}"
        response = requests.get(url, timeout=25)
        response.raise_for_status()
        data = response.content
        data.decode("utf-8")
        records.append({"file": local, "upstream_file": upstream,
                        "repository": "https://github.com/BYVoid/OpenCC", "revision": REVISION,
                        "url": url, "license": "Apache-2.0", "sha256": hashlib.sha256(data).hexdigest()})
        downloads.append((root / local, data))
    for path, data in downloads:
        path.write_bytes(data)
    (root / "variant-sources.json").write_text(json.dumps(records, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")
    print("Saved pinned OpenCC character table and license.")


if __name__ == "__main__":
    main()
