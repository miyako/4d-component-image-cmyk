# Native dependencies and provenance

The C++17 executable statically links the selected codec libraries and Little
CMS. It does not run ImageMagick, PHP, an installed helper, or a runtime
downloader. Build-time downloads are separate from component execution.

| Library | Version | License |
| --- | --- | --- |
| Little CMS | 2.17 | MIT |
| libjpeg-turbo | 3.1.1 | IJG, BSD-3-Clause and zlib |
| libtiff | 4.7.0 | libtiff permissive license |
| libpng | 1.6.47 | PNG Reference Library License 2.0 |
| zlib | 1.3.1 | zlib |
| nlohmann JSON | 3.12.0 | MIT |

This software is based in part on the work of the Independent JPEG Group.

The exact HTTPS archives and SHA-256 checksums are recorded in
[`cli/dependencies.json`](cli/dependencies.json). `scripts/build_dependencies.py`
verifies each complete archive before extraction, rejects links and unsafe
archive paths, and builds static libraries in target-specific directories.
JPEG SIMD is disabled to avoid an undeclared assembler dependency. TIFF's
optional JPEG, JBIG, LZMA, LERC, WebP, Zstandard and libdeflate integrations are
disabled; a format's existence does not imply support for all its compressions.
The executable links the pinned static archives, not Homebrew/host-installed
codec libraries.

The original unmodified upstream license bytes are retained under
`component/CMYK/Resources/licenses/`. Its `provenance.json`
binds each notice to its source archive, version, license and SHA-256.
Every native build checks these bytes against the verified source archives.
For a deliberate dependency update, update the lock, review the new license
obligations, then regenerate notices explicitly:

```sh
python3 scripts/build_dependencies.py --target macos-arm64 --write-notices
```

`scripts/build_cli.py` records the source-input digest, Git revision, target,
binary digest, actual CMake/compiler evidence and native smoke execution in
`build/<target>/build-manifest.json`. Codec source pins and build recipes are
included in the source-input digest. Resource assembly rejects missing,
modified or mixed-revision binaries/manifests. Source-pinned rebuildability is
not a claim of byte-for-byte identical output across compiler versions.

macOS uses Apple's system C/C++ runtime; audit the final executable with
`otool -L`. Windows uses MSVC's static CRT; native Windows execution and
dependency inspection remain required release evidence. Compiler/SDK
redistribution requirements must be reviewed independently of the codec
licenses. No vendor tool4d runtime is bundled.

## Profiles

No proprietary printer/press ICC profile is included. Callers must provide a
CMYK destination profile appropriate to their actual printing conditions, with
permission to embed it in their resulting image.

Test profiles generated with the native fixture tool are synthetic transform
plumbing fixtures, not measured print characterizations or recommended press
profiles. Their purpose is deterministic codec/profile integration and
comparison with an independent direct Little CMS transform. They do not prove
press accuracy, gamut suitability, soft-proof accuracy or pure-black
preservation. See the fixture documentation for the exact generated model.

## Official free compiler download

The unchanged tool4d 20.7 build 101905 archive pin is in `package.json.tool`.
As observed on November 4, 2026, the official
`https://resources-download.4d.com/` endpoint redirects to the vendor CDN
`https://builds2.4d.com/`. This generated repository explicitly permits these
exact HTTPS hostnames on **every** redirect hop, with no arbitrary suffix
matching, credentials or nonstandard ports. Authentication/cookies are not
forwarded, signed redirect query strings are not saved to installation
metadata, and the complete archive must still match the original pinned
SHA-256 before safe extraction. This does not change the original template
repository or authorize a different compiler build.

An explicitly approved **diagnostic-only** override also permits the exact
vendor host `product-download.4d.com`. The approved tool4d 21.0 build 100301
ARM64 archive SHA-256 is
`e15500e59ead66abab6f8c592643bd2e648429aaa0df5b700bd898e2f6043b8b`,
independently published in the vendor's
[immutable Homebrew cask](https://github.com/4d/homebrew-tap/blob/df1666e7a3bd4342abfee2ae0189d223b4de05f4/Casks/tool4d.rb).
It does not replace the default 20.7 pin, establish 20-compatible compiled
output, or authorize a published runtime-baseline change.
