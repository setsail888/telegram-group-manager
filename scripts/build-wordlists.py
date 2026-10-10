"""Export original reviewed templates as downloadable, deterministic TXT lists."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from app.word_library import catalog


def main():
    for pack in catalog().values():
        if not pack.id.startswith("public_"):
            path = root / "app" / "wordlists" / ("curated_"+pack.id+".txt")
            path.write_text("\n".join(pack.words)+"\n", encoding="utf-8", newline="\n")
    print("Exported original categories; public Apache-2.0 data is unchanged.")


if __name__ == "__main__":
    main()
