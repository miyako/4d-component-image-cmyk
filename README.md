# CMYK

A **real 4D component** for local, ICC-managed RGB/CMYK image conversion to
CMYK TIFF or JPEG. Its C++17 executable, `image-cmyk`, uses Little CMS for color
transforms and libpng, libjpeg-turbo and libtiff for image I/O. It is not a native
4D plug-in, an image uploader or a naive RGB-to-CMYK channel inversion.

**Draft release candidate, not fully cross-platform accepted.** Historical native CLI tests
pass on all three targets. Source and compiled-mode harness assertions pass on
hosted macOS 15 ARM64 with the unchanged pinned tool4d 20.7; an unsigned compiled
ZIP is available as a CI artifact. **tool4d does not establish normal external
component installation/loading in the actual 4D application.** That check,
Intel/Windows 4D and published installation remain unvalidated.
The current `0.1.0-rc.2` candidate targets the prospective signed macOS release
flow below; no successful signed/notarized run is claimed yet. Prerelease
acceptance remains partial with `release_ready:false`.
See [current acceptance evidence](#release-candidate-evidence-2026-11-04).

| Identity | Value |
| --- | --- |
| Repository / component / package | `miyako/CMYK` / `CMYK` |
| Host namespace / class | `cs.CMYK.Converter` |
| CLI / version / protocol | `image-cmyk` / `0.1.0-rc.2` / `1` |
| Implementation | C++17, CMake; statically built image/color dependencies |
| Native targets | macOS x86_64, macOS arm64, Windows x64 |

Target support is not evidence that a particular revision has passed native
tests. Windows ARM64 is unsupported. macOS resources contain a universal
executable; Windows resources contain `image-cmyk.exe`.

## 4D API

See the full [Converter contract](component/CMYK/Documentation/Classes/Converter.md)
and generic [_CLI](component/CMYK/Documentation/Classes/_CLI.md)
and [_Process](component/CMYK/Documentation/Classes/_Process.md)
contracts. Component-internal code uses `cs.Converter`; host code uses the
`CMYK` namespace.

```4d
var $tool : cs.CMYK.Converter
var $source; $destination; $profile : 4D.File
var $result; $options : Object
var $target : Text
$target:="macOS-universal"
If (Is Windows)
  $target:="Windows-x64"
End if
$tool:=cs.CMYK.Converter.new($target)
$source:=Folder(fk desktop folder).file("tagged-input.png")
$destination:=Folder(fk desktop folder).file("output-cmyk.tif")
$profile:=Folder(fk desktop folder).file("printer.icc")
$options:=New object("destinationProfile"; $profile; "intent"; "relativeColorimetric")
$result:=$tool.convertFile($source; $destination; $options)
If ($result.ok)
  $result:=$tool.inspectFile($destination)
  If ($result.ok)
    ALERT($result.value.colorSpace)  // CMYK
  End if
Else
  ALERT($result.error.code+": "+$result.error.message)
End if
```

`inspectFile(file)` returns dimensions, channels, model, bit depth, format,
orientation, DPI and embedded profile information. `convertFile(source;
destination; options)` performs the transform. `version()` returns the CLI
version. Parameters are actual `4D.File` objects, not Text paths. Calls are
synchronous, close stdin, and have a finite **30-second timeout**. There is no
shared conversion state or asynchronous job queue; do not concurrently target
the same destination.

Success is `{ok:true,stdout,stderr,exitCode:0,value}`. Failures are
`{ok:false,error:{code,message}}`, with available process streams/status and
diagnostic details retained. The wrapper distinguishes argument, resource,
launch, process, nonzero CLI and malformed-protocol failures. A native nonzero
status is reported as `cli_error`; its JSON stderr contains the native code and
message. Prelaunch failures do not invent an exit code. Nonempty stderr alone
is not failure. There is no PATH fallback, shell, runtime download or chmod.

## Image and profile contract

* Input: **8-bit RGB PNG**, **8-bit RGB or CMYK JPEG/TIFF**. Output:
  **8-bit, four-channel CMYK TIFF/JPEG**, no alpha.
* PNG must be non-interlaced and non-animated. TIFF is classic (not BigTIFF),
  single-page, stripped (not tiled), contiguous unsigned 8-bit RGB or
  four-ink CMYK without extra samples or sub-IFDs. Unsupported compression
  codecs fail explicitly; see [dependency build restrictions](DEPENDENCIES.md).
* Alpha, grayscale, unsupported bit depths and orientations other than
  top-left (`1`) are rejected rather than silently flattened, quantized or
  rotated. This is not a general-purpose document/multipage image editor.
* A valid **CMYK destination ICC profile is required** and is embedded in the
  output. Supply a licensed profile appropriate to the actual print process.
* An embedded source profile is used by default. `sourceProfile` is only for
  untagged input: it must match the source model and cannot override tagged
  input. Profile mismatches and malformed profiles fail.
* Untagged input is rejected by default. `allowAssumeSRGB:true` explicitly
  permits sRGB for untagged **RGB only**, with a warning in the result. Untagged
  CMYK always needs a suitable supplied CMYK profile.
* Existing destinations are not overwritten and sources are not modified.
  The selected format, not the destination extension, controls encoding.
* Input is bounded to **256 MiB**, ICC profiles to **32 MiB**, images to
  **40 million pixels**, and decoded pixel storage to **256 MiB**. These limits
  do not promise a 30-second completion time for every permitted input.
* Dimensions and supported DPI metadata are carried through conversion.
  Arbitrary EXIF, XMP, text and application-specific metadata preservation is
  not promised. JPEG is lossy; pixel equality is not a JPEG round-trip promise.
  Staging files are removed on handled failures. A forced OS termination or
  process timeout can leave a private `.image-cmyk-*.partial` file in the
  destination directory; it is not the published output and can be removed
  after confirming the corresponding process has exited.

Conversion options:

| 4D option | CLI option | Default / values |
| --- | --- | --- |
| `destinationProfile` | `--destination-profile` | Required CMYK ICC File/path |
| `sourceProfile` | `--source-profile` | Optional untagged-source ICC File/path |
| `allowAssumeSRGB` | `--allow-assume-srgb` | `false`; `true` or `false` |
| `intent` | `--intent` | `relativeColorimetric`; also `perceptual`, `saturation`, `absoluteColorimetric` |
| `blackPointCompensation` | `--black-point-compensation` | `true`; `true` or `false` |
| `format` | `--format` | `tiff`; `tiff` or `jpeg` |
| `quality` | `--quality` | `90`; integer `1..100`, affects JPEG, not TIFF |

The generated test CMYK ICC fixture is deterministic transform plumbing,
**not a characterized press profile**. Its test results make no printing-quality
or colorimetric-accuracy claim for a physical device.

## CLI protocol

```text
image-cmyk --version
image-cmyk inspect --source PATH
image-cmyk convert --source PATH --destination PATH --destination-profile PATH
  [--source-profile PATH] [--allow-assume-srgb true|false]
  [--intent relativeColorimetric|perceptual|saturation|absoluteColorimetric]
  [--black-point-compensation true|false] [--format tiff|jpeg] [--quality N]
```

Each result is exactly one UTF-8 JSON object plus LF. Success exits `0`, writes
stdout and leaves stderr empty. Version returns
`{"protocol":1,"version":"0.1.0-rc.2"}`. Inspection includes:

```json
{
  "protocol": 1,
  "width": 8,
  "height": 6,
  "channels": 3,
  "colorSpace": "RGB",
  "bitDepth": 8,
  "alpha": false,
  "format": "png",
  "orientation": 1,
  "dpiX": 0,
  "dpiY": 0,
  "embeddedProfile": {"present": false}
}
```

Dimensions/DPI above are illustrative, not fixed fixture expectations. A present
profile includes its color space and native metadata. Conversion includes
`protocol`, dimensions, `channels:4`, `colorSpace:"CMYK"`, output `format`,
`sourceProfile`, `destinationProfile` and a Text-array `warnings`.

Errors exit **2**, leave stdout empty and write one stderr object:

```json
{"protocol":1,"error":{"code":"invalid_arguments","message":"missing --source"}}
```

Invalid, missing, duplicate and unknown options are errors, not ignored
configuration. `read_failed` identifies the failing `source`, `sourceProfile`
or `destinationProfile`, includes its path, and retains available OS error
category/code/message. A readable image does not imply its separately supplied
ICC file exists: check the profile File's `.path` and `.exists` too. A missing
profile never creates the output or modifies the source.
Messages may depend on the failing operation. The 4D adapter
quotes literal paths into SystemWorker command-line Text without invoking a
shell; spaces, Unicode and OS-valid quotes are data. CLI subprocess tests do
not establish 4D's native quoting behavior; real host tests remain required.

## Build and tests

Read [AGENTS.md](AGENTS.md) before modifying identity, process execution,
packaging or acceptance tooling. This repository is already initialized:
do not rerun initialization to change its identity.

Dependencies, versions, source URLs and SHA256 pins are in
[`cli/dependencies.json`](cli/dependencies.json). See
[native dependency provenance and licenses](DEPENDENCIES.md) for verified notice
generation, static build restrictions and the official tool4d redirect policy.
The build uses pinned upstream
sources, not Homebrew/Pillow or an arbitrary system image library. Required
redistribution notices belong in component `Resources/licenses/`; the MIT
scaffold license does not replace dependency licenses or ICC profile licenses.
Build prerequisites are Python 3.10+, CMake and a C++17 compiler (Xcode/Clang
on macOS, MSVC on Windows). Windows builds use a static CRT. Compiler patch
versions must be reported from the actual build, not inferred from a CI image.

```sh
python3 scripts/doctor.py --root .
python3 scripts/build_cli.py --root . --target macos-arm64
python3 -m unittest discover -s tests -p test_cli.py -v
python3 -m unittest discover -s tests -v
python3 scripts/generate_icons.py --check
```

Choose the **native** target: `macos-arm64`, `macos-x64` or `windows-x64`.
C++ cross mode is unsupported. `build_cli.py` builds native CTest checks and
CLI smoke vectors. CTest independently compares transform pixels with direct
Little CMS reference calls; Python tests exercise the built executable's
protocol, files, options and path handling without installing Pillow.
Python integration expects `build/<target>/image-cmyk[.exe]` and the native
fixture generator under `build/<target>/cmake/` (or its Windows `Release/`
directory). It detects the native target using the build script;
`CLI_TARGET`, if set, must match that native target.
It does not rebuild a copied starter or silently skip a missing
selected C++ executable. `TEST_CLI_LANGUAGE=none` explicitly skips native CLI
execution for infrastructure-only checks; a skip is **not** feature acceptance.
The retained infrastructure suites validate packaging/process tooling, not a
claim that the former example feature is still this component's API.

## Release-candidate evidence (2026-11-04)

The predecessor package `4d-component-image-cmyk` first passed tool4d harness assertions in
[CI run 41776439377](https://github.com/miyako/CMYK/actions/runs/41776439377)
at commit `d4d21e84e5d69f7b42a549c798363388365c8da7`, on hosted macOS 15 ARM64
with tool4d **20.7 build 101905**. Those historical artifacts do not validate the
renamed `CMYK` package. The latest tool4d-tested `CMYK` revision, artifact hash and run
are recorded in [the follow-up pull request](https://github.com/miyako/CMYK/pulls)
and each new artifact's adjacent acceptance JSON. Before the first public release,
the host namespace was also changed from `ImageCMYK` to `CMYK`: use
`cs.CMYK.Converter` in host declarations and constructor calls. There is no legacy
namespace alias. Class `Converter`, CLI `image-cmyk` and the existing catalog UUID
remain unchanged. Earlier artifacts do not validate the new namespace.
The namespace is locally unique within this component;
uniqueness among a host's future components cannot be proven here.

The harness stages the component under the isolated host's
`Components/CMYK.4dbase`. The host's Sources contains only the `Smoke` and
`Host_error` methods plus settings/dependency metadata, with no copied feature
classes; `Smoke` calls `cs.CMYK` classes directly. This is observed **tool4d
harness execution and compiled-mode assertions**, not a normal 4D application
external component installation/loading test. The historical `0.1.0-rc.1`
tool4d report has this same limited scope: it is **not normal app validation**,
regardless of older “host acceptance” labels. Published historical JSON/assets
are not rewritten; interpret them with this limitation.

| Check | Status | Observed evidence / limitation |
| --- | --- | --- |
| Native ARM64/Intel/Windows x64 CLI | `passed` | Three native CI jobs, 131 native assertions and 14 Python CLI integration tests per target; independent Little CMS reference pixel equality |
| Codec runtime closure and resource assembly | `passed` | Static codecs; allowlisted system imports; Windows DLL/static-CRT audit; universal macOS plus Windows resources from same-revision manifests |
| Source tool4d harness process and feature assertions | `passed` on hosted ARM64 | Clean process exit, actual `cs.CMYK.Converter` calls, 11 feature cases and full generic/negative contract; not normal app loading |
| 4D compilation acceptance | `passed` on hosted ARM64 | Fresh success/empty diagnostics, both targets, real CompiledCode/Libraries and input/output digest marker |
| Source-stripped ZIP / unpacked tool4d compiled-mode harness | `passed` on hosted ARM64 | Exact final ZIP unpacked and staged; tool4d reports compiled mode true with 11 cases and full contract; not normal app loading |
| Actual 4D application external component installation/loading | `not_run` | Requires separate testing in the normal 4D application; tool4d does not establish this |
| Earlier local macOS 26.7 runtime | `failed` locally | Shutdown SIGSEGV even in a component-free project; not waived or relabeled as success |
| Intel/Windows 4D, published dependency installation, signing | `not_run` | No native runtime, publication or signing claim |

The earlier local environment was macOS 26.7 ARM64, Python 3.12.12,
CMake 4.4.3 and Apple Clang 21.0.0. Its shutdown defect was reproduced in a
minimal component-free project using
both explicit `QUIT 4D` and the documented automatic shutdown after `return`.
An independently approved diagnostic tool4d **21.0 build 100301** ARM64 pin also
crashes on the minimal `return` probe. The default compiler remains **20.7**;
the diagnostic download does not change the supported runtime baseline.
The clean hosted environment passes with the same pinned 20.7 binary without
accepting signal exits, weakening gates or changing the runtime baseline.

Reproducible commands include the native build and test commands above and the
tool4d commands below. Raw local reports/logs are under `build/`, including
`host-status.json`, `compile-status.json`, `source-host.log`, `compile.log`,
and `shutdown-probe/result{,-return,-return21}.json`. A successful JSON report
from a crashed process is **not** a passed acceptance check.

The historical compiled candidate is unsigned and available for review/testing; CI artifacts
are distinct from published experimental prereleases under GitHub Releases.
Intel/Windows 4D acceptance and actual published installation
remain separate checks. Any proposed compiler-baseline change requires a
separate explicit decision.

### Source and compiled-mode tool4d harness checks

For an isolated hosted attempt without changing repository variables, dispatch
**Actions > CI > Run workflow**, select the implementation branch and enable
`accept_4d`. From the CLI:

```sh
gh workflow run ci.yml --ref main -f accept_4d=true
```

This rebuilds all three native targets at the selected commit, then runs the
existing strict acceptance helper on hosted macOS 15 ARM64. The explicit opt-in
uses the default `package.json.tool` checksum pin, not repository URL overrides.
It uploads `four-d-acceptance` containing reports/logs even on failure, and the
ZIP plus adjacent evidence JSON only if packaging reaches those stages.
No tag, publication, signing or persistent `FOURD_ENABLED` change is performed.
A failed process remains a failed job; a single component-free shutdown probe
is retained after acceptance failure. The helper's `release_ready:false` and
untested normal 4D application/Intel/Windows 4D/install/signing checks must not
be relabeled as passed.

The pinned free tool4d in `package.json.tool` is **20.7 build 101905,
macOS ARM64**. Unsigned local component compilation needs no license activation,
signing secret or release publication. Do not bundle tool4d.

```sh
python3 scripts/ci_config.py download-tool4d --root .
python3 scripts/test_4d.py --root . --tool4d .tools/tool4d.app/Contents/MacOS/tool4d
python3 scripts/compile_component.py --root . --tool4d .tools/tool4d.app/Contents/MacOS/tool4d
python3 scripts/package_component.py --root . --repository miyako/CMYK \
  --tag v0.1.0-rc.2 --unpack-test
python3 scripts/test_4d.py --root . --tool4d .tools/tool4d.app/Contents/MacOS/tool4d \
  --component build/release-test/CMYK.4dbase
```

Before assembly, obtain **all three native executable artifacts and manifests
from the same commit/input revision**, then use
`python3 scripts/ci_config.py assemble --root .`. Do not mix stale binaries.
Compilation must produce fresh successful evidence and genuine CompiledCode
and native Libraries. Packaging strips class/method source; the last tool4d
harness test must stage the exact unpacked ZIP and assert compiled mode,
not use the source folder. These strict checks do not substitute for external
component installation/loading in the normal 4D application.
`scripts/accept_local.py` can orchestrate these stages when all artifacts are
available. Read its help and [the acceptance playbook](AGENTS.md).

**Validation status:** use the exact revision and bytes in the selected CI
artifact's evidence, not this README alone. The helper correctly reports
`result:"partial"`, `finished:true`, `release_ready:false` after successful
ARM64 tool4d harness checks; separate native CI provides Intel/Windows CLI
evidence, not Intel/Windows 4D or normal application loading evidence.
Legacy `source_host` / `compiled_host` report keys identify tool4d harness steps,
now labeled `execution_scope:"tool4d_harness"` and
`normal_app_loading:"not_run"`. The separate `real_4d_application` acceptance
check remains `not_run` even when both harness steps pass.
The unsigned artifact/evidence pair is
`dist/CMYK.zip` and adjacent
`dist/CMYK.acceptance.json`. Check `build/acceptance.json`,
`build/source-host-status.json`, `build/compiled-host-status.json` and
`build/compile-status.json` for the actual revision, hashes, tools and results.
No signing, notarization, publication or download/install success is implied.

## Bump and release

After this workflow is merged, open **Actions > Bump and release > Run workflow**
on `main`. Choose `major`, `minor`, `patch` or `promote`. **Dry run is enabled
by default**: it builds a disposable, unpushed version candidate, executes the
three native CLI targets and pinned 20.7 tool4d/compiler/package gates, then
downloads and verifies that exact compiled ZIP in a separate job. It uses no
signing secrets and changes no remote branch, tag or release.

| Choice | Rule | From `0.1.0-rc.2` |
| --- | --- | --- |
| `major` | Increment major, reset minor/patch | `1.0.0` |
| `minor` | Increment minor, reset patch | `0.2.0` |
| `patch` | Increment the numeric patch | `0.1.1` |
| `promote` | Remove prerelease/build metadata without changing the core | `0.1.0` |

These choices produce stable versions and discard build metadata; `promote`
rejects an already stable version. They do not increment an RC counter.
`package.json.version` is authoritative, while `template.json`, native CLI
version, host version assertions and the current class-documentation example
must remain synchronized. Historical release facts are not rewritten.
For a read-only local plan, run `python3 scripts/bump_release.py plan --bump promote`.

Unchecking **Dry run** explicitly requests production signing and publication.
Production dispatch is restricted to `main`. The workflow resolves one base
snapshot and transports a deterministic version-only commit through a
checksum-verified Git bundle; every target builds the same unpushed commit.
Only after all native, compiler, exact-package, signature and Apple notarization
gates pass does it atomically fast-forward `main` and create the matching tag.
It then publishes the already tested assets directly, without relying on a
`GITHUB_TOKEN` tag push to trigger another workflow.

Runs are serialized. A moved `main`, conflicting tag, dirty checkout or branch
protection rejection fails closed; no force push, protection bypass or existing
asset overwrite is used. Candidate source and accepted artifacts are retained
for 30 days. After a publication interruption, retry the failed publication job
with its original candidate/assets, not a second version bump: resume accepts
only the identical source, channel and asset hashes.

New SemVer prereleases publish as `prerelease:true`, never Latest. Stable tags
publish as `prerelease:false` and explicitly Latest; a hyphen in build metadata
alone is not a prerelease. This feature does not alter the existing rc2 tag,
assets or externally edited release flags. Workflow validation/merge is not
permission to execute a production bump.

## Signed macOS release workflow

After the reviewed workflow is merged to `main`, manually dispatch **Actions >
Release compiled component > Run workflow**, select `main`, set `preflight:true`
and leave `version_tag` empty:

```sh
gh workflow run release.yml --ref main -f preflight=true
```

Preflight cannot reuse a version tag already bound to another commit. After
changes to an already tagged version, use the **Bump and release** dry run to
validate a new candidate; never move the existing tag.

This preflight is intended to build, sign and notarize without creating a tag
or publishing a release. It still requires the hosted 4D opt-in
`FOURD_ENABLED=true`. The six required signing/notarization secrets are already
registered; registration is **not** evidence that Apple accepted a submission
or that any signed run passed.

The production flow signs the macOS universal CLI and compiled native
library, then produces a signed, notarized and stapled DMG separately from
`CMYK.zip`. The ZIP contains the identical component payload, but **a ZIP
cannot be stapled**; the stapled ticket belongs to the DMG. Windows binaries
remain unsigned. Acceptance and native build manifests describe **presign**
bytes; final distribution and signed-payload hashes belong to
`CMYK.signing.json`. Do not treat presign hashes as hashes of final signed
assets. The final signed package must undergo another strict tool4d harness
run; that rerun still does **not** validate normal external component
installation/loading in the actual 4D application.

Payload trust checks reuse the exact commands verified against the published
[Text-to-SVG v0.1.1 release](https://github.com/miyako/Text-to-SVG/releases/tag/v0.1.1)
and its [same-revision signer](https://github.com/miyako/Text-to-SVG/blob/620cd1e5987cf93812aab4d932a8beee81dd60c7/scripts/sign_release.py#L708-L719):
`codesign --verify --strict --all-architectures --check-notarization -R=notarized`
for **each** Mach-O payload, plus `spctl --type open` for the signed DMG.
The reference's genuinely notarized bare CLI also produces `spctl`'s
"the code is valid but does not seem to be an app" rejection. CMYK records that
exact diagnostic as a failed, non-applicable CLI probe, never as acceptance;
any other rejection remains fatal. Direct online notarization and DMG Gatekeeper
checks remain mandatory, not fallback successes.

The compiler harness runs from an isolated copy under `build/`, not its
versioned project: tool4d creates a catalog and DerivedData on opening a project.
The post-compiler candidate checkpoint still requires a clean source checkout;
runtime caches are not an excuse to ignore or reset source changes.

Only a completed successful preflight establishes the recorded gates. Evidence
retains partial acceptance and `release_ready:false` because the automated
harness does not establish full normal-application/cross-platform acceptance.
The existing-tag release entrypoint accepts an already existing matching
SemVer tag; the bump entrypoint above prepares a new version/tag only after
its gates. Preflight creates no tag, and merging source changes alone is not
permission to tag or publish. Signed `v0.1.0-rc.2` and unsigned rc1 remain intact.
`CMYK.acceptance.json`, `CMYK.native-evidence.json` and `CMYK.signing.json`
distinguish the presign observations from final signed-byte evidence.

## Installation instructions (normal application validation still required)

In the host Dependency manager, add GitHub repository
`miyako/CMYK` and a published matching version tag.
The release asset must be `CMYK.zip`. Dependency-manager
GitHub support requires a compatible 4D release (20 R6 or later/current LTS).
For a separate normal 4D application installation/loading test, manually copy
the unpacked
`CMYK.4dbase` into the host's `Components/` folder and reload.
These are installation instructions, not observed installation/loading evidence.
Manual local loading is not evidence of published dependency-manager install.
