"""Reproduce the reviewed, pinned public dictionaries; never run upstream code."""
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
import requests

REVISION = "a7d06bb1c321e669943b6841570d9da6dad8ce2b"
REPOSITORY = "https://github.com/fwwdn/sensitive-stop-words"
FILES = {"public_ads": "广告.txt", "public_adult": "色情类.txt",
         "public_danger": "涉枪涉爆违法信息关键词.txt"}


def main():
    root = Path(__file__).resolve().parents[1] / "app" / "wordlists"
    root.mkdir(parents=True, exist_ok=True)
    sources = []
    for pack, name in {**FILES, "LICENSE-Apache-2.0": "LICENSE"}.items():
        url = f"https://raw.githubusercontent.com/fwwdn/sensitive-stop-words/{REVISION}/{quote(name)}"
        response = requests.get(url, timeout=25)
        response.raise_for_status()
        data = response.content
        data.decode("utf-8-sig")  # Reject a changed/non-text upstream response.
        filename = pack + (".txt" if name != "LICENSE" else "")
        (root / filename).write_bytes(data)
        sources.append({"file": filename, "upstream_file": name, "repository": REPOSITORY,
                        "revision": REVISION, "url": url, "license": "Apache-2.0",
                        "sha256": hashlib.sha256(data).hexdigest()})
    (root / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("Saved pinned public lists and Apache-2.0 license; no upstream code executed.")


if __name__ == "__main__":
    main()
