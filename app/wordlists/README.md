# Dictionary provenance

- `curated.json` contains original, reviewable phrases and solicitation templates. `curated_*.txt` are deterministic expansions produced by `python scripts/build-wordlists.py`.
- `public_ads.txt`, `public_adult.txt`, and `public_danger.txt` are unmodified raw copies of three files from [fwwdn/sensitive-stop-words](https://github.com/fwwdn/sensitive-stop-words) at revision `a7d06bb1c321e669943b6841570d9da6dad8ce2b`, under Apache-2.0. `LICENSE-Apache-2.0` preserves the upstream license. `sources.json` records original names, URLs, hashes and attribution. `.gitattributes` prevents Git newline conversion of these raw files.
- Runtime normalization, length filtering, deduplication and the 24 reviewed exclusions in `public_exclusions.json` are described in [the word library guide](../../docs/word-library.md). These transformations leave the source files unchanged. Upstream whitespace is preserved and exempt from code whitespace checks.
- No private bot dictionary, production chat corpus or measured detection rate is supplied. Every category starts disabled. Wider public categories can produce false positives, especially on quotations.
- `python scripts/update-wordlists.py` reproduces only this pinned version. Updates need explicit maintenance and review; the running bot does not download rules automatically.
