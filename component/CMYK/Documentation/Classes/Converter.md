# Converter

`cs.ImageCMYK.Converter.new({target : Text})` wraps the bundled `image-cmyk`
executable. Within the component the class is `cs.Converter`. `target` may be
omitted on macOS, or be `"macOS-universal"`. Windows requires `"Windows-x64"`;
native Windows ARM64 is unsupported.

| Method | Result value |
| --- | --- |
| `inspectFile(file : 4D.File) : Object` | Image dimensions, model, channels, format, DPI, orientation and embedded ICC information |
| `convertFile(source : 4D.File; destination : 4D.File; options : Object) : Object` | ICC-managed CMYK TIFF/JPEG conversion and selected profiles |
| `version() : Object` | `{protocol:1,version:"0.1.0"}` |

These are synchronous, separate-process calls with closed stdin and a finite
30-second timeout. There is no shared mutable conversion state or asynchronous
job queue. Separate calls must not target the same destination concurrently.

## Conversion options

| Property | Type | Default / contract |
| --- | --- | --- |
| `destinationProfile` | `4D.File` | **Required**, valid CMYK ICC output profile |
| `sourceProfile` | `4D.File` | Optional fallback for an untagged source; cannot override an embedded profile |
| `allowAssumeSRGB` | Boolean | `False`; permits an explicit sRGB assumption only for an untagged RGB source |
| `intent` | Text | `"relativeColorimetric"`; also `"perceptual"`, `"saturation"`, `"absoluteColorimetric"` |
| `blackPointCompensation` | Boolean | `True` |
| `format` | Text | `"tiff"`; or `"jpeg"` |
| `quality` | Integer | `90`, range `1..100`; JPEG quality, not a TIFF compression setting |

The public parameters are received as Variants solely to reject invalid dynamic
calls with `invalid_argument`: File inputs must actually be non-null `4D.File`
instances, not Text paths, Folders or plain Objects. Scalars are rejected before
object-instance inspection. Defined options with Null or the wrong type are
errors, not requests for defaults. File paths and process arguments reject
embedded NUL using strict character comparison.

Input supports 8-bit RGB/CMYK JPEG and TIFF, and 8-bit RGB PNG. Unsupported
alpha, grayscale, bit depths and orientations are rejected rather than silently
flattened, quantized or rotated. Only top-left orientation (`1`) is supported,
including inspection. PNG must be non-interlaced and non-animated. TIFF is
classic, single-page, stripped, contiguous unsigned 8-bit RGB/four-ink CMYK,
without extra samples or sub-IFDs; unavailable compression codecs fail.
Input/encoded output is bounded to 256 MiB, profiles to
32 MiB, and decoded images to 40,000,000 pixels. Conversion uses Little CMS and the selected ICC profiles; it is
not channel relabeling or a naive RGB inversion. CMYK output has four channels,
no alpha, and embeds the actual destination ICC profile. The destination format
is explicit, not inferred from its extension. Existing destinations are not
silently overwritten. Files are processed by a local CLI, never uploaded.

An untagged source is not implicitly assigned a profile. Supply a compatible
`sourceProfile`, or explicitly permit sRGB for RGB input. CMYK input always
needs a suitable embedded or supplied CMYK profile. Assumptions are reported in
`warnings`. A test fixture CMYK profile is only deterministic plumbing data:
it is **not** a characterized press profile and makes no printing-quality claim.
Use a licensed output profile appropriate to the actual print process.

```4d
var $tool : cs.ImageCMYK.Converter
var $target : Text
var $source; $destination; $icc : 4D.File
var $result : Object
$target:="macOS-universal"
If (Is Windows)
  $target:="Windows-x64"
End if
$tool:=cs.ImageCMYK.Converter.new($target)
$source:=Folder(fk desktop folder).file("input.png")
$destination:=Folder(fk desktop folder).file("output-cmyk.tif")
$icc:=Folder(fk desktop folder).file("printer.icc")
$result:=$tool.convertFile($source; $destination; New object("destinationProfile"; $icc))
If ($result.ok)
  $result:=$tool.inspectFile($destination)
  If ($result.ok)
    ALERT($result.value.colorSpace)  // CMYK
  End if
Else
  ALERT($result.error.code+": "+$result.error.message)
End if
```

## Results and strict protocol decoding

Success is `{ok:true,stdout:Text,stderr:Text,exitCode:0,value:Object}`.
The value is the decoded protocol-1 JSON object:

* Inspection: `protocol`, positive integer `width` and `height`, `channels`
  (`3` for RGB, `4` for CMYK), `colorSpace` (`"RGB"` or `"CMYK"`),
  `bitDepth:8`, `alpha:false`, `format` (`"png"`, `"tiff"` or `"jpeg"`),
  integer `orientation`, nonnegative numeric `dpiX` and `dpiY`, and
  `embeddedProfile` with Boolean `present`. A present embedded profile includes
  its `colorSpace`, Text `description` and integer `bytes`.
* Conversion: `protocol`, positive integer `width` and `height`, `channels:4`,
  `colorSpace:"CMYK"`, `format` (`"tiff"` or `"jpeg"`), `sourceProfile` and
  `destinationProfile` descriptor Objects including `colorSpace`, Text
  `description` and integer `bytes`, and a Collection of Text `warnings`.
  `sourceProfile.origin` is `"embedded"`, `"supplied"` or `"assumed-srgb"`;
  an assumed profile has no source-file bytes.
* Version: numeric `protocol:1` and nonempty Text `version`.

The decoder validates the top-level Object, numeric protocol before comparing
it, and every required field's type and accepted range. Boolean/Null protocol
versions, scalar/Collection payloads, malformed JSON, mismatched channels/model,
wrong profile descriptors and malformed warning arrays fail with
`invalid_protocol`. Type-sensitive guards are separate statements because 4D
Boolean expressions do not safely short-circuit. Extra native metadata is
preserved, not discarded.

Failure is `{ok:false,error:{code,message{,details}}}`. Available stdout, stderr
and exitCode remain separate; prelaunch validation/resource failures do not
invent an exitCode. A nonzero native status (currently `2`) returns `cli_error`;
the native JSON stderr contains the feature-specific diagnostic. Nonempty stderr
alone is not failure. Other generic codes are `missing_executable`,
`unsupported_platform`, `unsupported_architecture`, `invalid_argument`,
`launch_failed`, `process_error`, `process_terminated`, and `invalid_protocol`.
Local error handlers are restored immediately after narrow JSON parsing and
launch capture. There is no PATH fallback, shell, runtime download or chmod.

## CLI correspondence

```text
image-cmyk inspect --source PATH
image-cmyk convert --source PATH --destination PATH --destination-profile PATH
  [--source-profile PATH] [--allow-assume-srgb true|false]
  [--intent relativeColorimetric|perceptual|saturation|absoluteColorimetric]
  [--black-point-compensation true|false] [--format tiff|jpeg] [--quality N]
image-cmyk --version
```

Each stream uses UTF-8 JSON with LF framing. The component passes File objects
to the generic adapter, which expands `.platformPath` with the correct 4D path
kind and quotes each literal into SystemWorker command-line Text. Spaces,
Unicode, quotes and shell metacharacters are data, not shell instructions.

`_cli`, `_decode` and the validation helpers are internal by convention, not a
4D access-control boundary. The real host fixture requires an isolated
`request.fixtureDirectory`, inspects all three input formats, converts and
re-inspects CMYK TIFF/JPEG outputs, and independently checks the recorded
envelopes against `tests/host/contract.json`. It also retains generic process
type/NUL/resource/launch checks and a renamed executable in quoted/Unicode paths.
Passing a Folder as an inspect source deliberately reaches the CLI and fails
as a directory; it is distinct from the adapter rejecting an unsupported type.
