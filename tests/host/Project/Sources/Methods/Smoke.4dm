//%attributes = {"invisible":true}
var $ignored : Integer
var $parameter; $name; $target : Text
var $request; $result; $report; $options; $payload : Object
var $tool : cs.ImageCMYK.Converter
var $requestFile; $statusFile; $source; $destination; $profile : 4D.File
var $renamedExecutable : 4D.File
var $fixtures : 4D.Folder
var $invalid : Variant
var HOST_StatusFile : 4D.File
var HOST_Phase : Text
var HOST_PreviousHandler : Text
$ignored:=Get database parameter(User param value; $parameter)
If (Is Windows)
	$requestFile:=File($parameter; fk platform path)
Else
	$requestFile:=File($parameter; fk posix path)
End if
$request:=JSON Parse($requestFile.getText())
If (Is Windows)
	$statusFile:=File($request.status; fk platform path)
Else
	$statusFile:=File($request.status; fk posix path)
End if
$statusFile.setText("{\"success\":false,\"phase\":\"starting\"}")
HOST_StatusFile:=$statusFile
HOST_Phase:="constructor"
HOST_PreviousHandler:=Method called on error(ek local)
ON ERR CALL("Host_error"; ek local)
$target:="macOS-universal"
If (Is Windows)
	$target:="Windows-x64"
End if
$tool:=cs.ImageCMYK.Converter.new($target)
$report:=New object("success"; True; "finished"; False; "cases"; New collection)
$report.compiled:=$tool._cli.compiled
HOST_Phase:="fixture directory"
If (Is Windows)
	$fixtures:=Folder($request.fixtureDirectory; fk platform path)
	$renamedExecutable:=File($request.executable; fk platform path)
Else
	$fixtures:=Folder($request.fixtureDirectory; fk posix path)
	$renamedExecutable:=File($request.executable; fk posix path)
End if
$profile:=$fixtures.file("destination.icc")
$options:=New object("destinationProfile"; $profile)
HOST_Phase:="renamed executable"
$report.renamedExecutable:=cs.ImageCMYK._Process.new().execute($renamedExecutable; New collection("--version"))
If (Not($report.renamedExecutable.ok))
	$report.success:=False
End if
HOST_Phase:="feature cases"
If ($request.expectCompiled#$report.compiled)
	$report.success:=False
End if
For each ($name; New collection("input.png"; "input.tif"; "input.jpg"))
	$source:=$fixtures.file($name)
	$result:=$tool.inspectFile($source)
	$report.cases.push($result)
	If (Not($result.ok))
		$report.success:=False
	End if
	$destination:=$fixtures.file($name+".cmyk.tif")
	$result:=$tool.convertFile($source; $destination; $options)
	$report.cases.push($result)
	If (Not($result.ok))
		$report.success:=False
	End if
	$result:=$tool.inspectFile($destination)
	$report.cases.push($result)
	If (Not($result.ok))
		$report.success:=False
	End if
End for each
HOST_Phase:="JPEG conversion"
$source:=$fixtures.file("input.png")
$destination:=$fixtures.file("output.cmyk.jpg")
$result:=$tool.convertFile($source; $destination; New object("destinationProfile"; $profile; "intent"; "perceptual"; "blackPointCompensation"; False; "allowAssumeSRGB"; False; "format"; "jpeg"; "quality"; 95))
$report.cases.push($result)
If (Not($result.ok))
	$report.success:=False
End if
$result:=$tool.inspectFile($destination)
$report.cases.push($result)
If (Not($result.ok))
	$report.success:=False
End if
HOST_Phase:="feature errors"
$report.invalid:=$tool.inspectFile($fixtures.file("missing.png"))
$report.profileOverride:=$tool.convertFile($source; $fixtures.file("override.tif"); New object("destinationProfile"; $profile; "sourceProfile"; $profile))
$report.invalidOptions:=New collection
For each ($invalid; New collection(Null; New object; New object("destinationProfile"; $profile; "allowAssumeSRGB"; "true"); New object("destinationProfile"; $profile; "blackPointCompensation"; 1); New object("destinationProfile"; $profile; "intent"; "invalid"); New object("destinationProfile"; $profile; "format"; "png"); New object("destinationProfile"; $profile; "quality"; 0); New object("destinationProfile"; $profile; "quality"; 1.5); New object("destinationProfile"; $profile; "quality"; 101); New object("destinationProfile"; $profile; "sourceProfile"; Null)))
	$result:=$tool.convertFile($source; $fixtures.file("invalid.tif"); $invalid)
	$report.invalidOptions.push($result)
	If ($result.ok)
		$report.success:=False
	End if
End for each
$report.invalidFiles:=New collection
For each ($invalid; New collection(123; 1.25; True; False; Null; Current date; Current time; New collection; New object))
	$report.invalidFiles.push($tool.inspectFile($invalid))
	$report.invalidFiles.push($tool.convertFile($invalid; $destination; $options))
	$report.invalidFiles.push($tool.convertFile($source; $invalid; $options))
End for each
$result:=$tool.version()
$report.version:=$result
If (Not($result.ok))
	$report.success:=False
End if
HOST_Phase:="missing executable"
$result:=cs.ImageCMYK._CLI.new("missing-cli"; $target).execute(New collection("--version"))
$report.missing:=$result
If ($result.error.code#"missing_executable")
	$report.success:=False
End if
$result:=cs.ImageCMYK.Converter.new("unsupported-arm-target").version()
$report.unsupported:=$result
If ($result.error.code#"unsupported_architecture")
	$report.success:=False
End if
HOST_Phase:="launch failure"
$result:=cs.ImageCMYK._Process.new().execute($requestFile; New collection)
$report.launch:=$result
If ($result.error.code#"launch_failed")
	$report.success:=False
End if
HOST_Phase:="malformed protocol"
$result:=$tool._decode(New object("ok"; True; "stdout"; "not json"; "stderr"; ""; "exitCode"; 0); "inspect")
$report.protocol:=$result
$report.protocolTypes:=New collection
For each ($name; New collection("{\"protocol\":true}"; "{\"protocol\":false}"))
	$result:=$tool._decode(New object("ok"; True; "stdout"; $name; "stderr"; ""; "exitCode"; 0); "inspect")
	$report.protocolTypes.push($result)
End for each
$report.protocolNull:=$tool._decode(New object("ok"; True; "stdout"; "{\"protocol\":null}"; "stderr"; ""; "exitCode"; 0); "inspect")
$report.protocolScalars:=New collection
For each ($name; New collection("true"; "false"; "null"; "1"; "[]"))
	$report.protocolScalars.push($tool._decode(New object("ok"; True; "stdout"; $name; "stderr"; ""; "exitCode"; 0); "inspect"))
End for each
$report.protocolFields:=New collection
For each ($name; New collection("{\"protocol\":1}"; "{\"protocol\":1,\"width\":true}"; "{\"protocol\":1,\"width\":2,\"height\":2,\"channels\":3,\"colorSpace\":false}"; "{\"protocol\":1,\"width\":2,\"height\":2,\"channels\":4,\"colorSpace\":\"CMYK\",\"format\":\"tiff\",\"sourceProfile\":null,\"destinationProfile\":null,\"warnings\":[]}"))
	$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; $name; "stderr"; ""; "exitCode"; 0); "inspect"))
	$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; $name; "stderr"; ""; "exitCode"; 0); "convert"))
End for each
$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; "{\"protocol\":1,\"version\":true}"; "stderr"; ""; "exitCode"; 0); "version"))
For each ($name; New collection("width"; "height"; "channels"; "colorSpace"; "bitDepth"; "alpha"; "format"; "orientation"; "dpiX"; "dpiY"; "embeddedProfile"))
	$payload:=JSON Parse(JSON Stringify($report.cases[0].value))
	$payload[$name]:=Null
	$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; JSON Stringify($payload); "stderr"; ""; "exitCode"; 0); "inspect"))
End for each
For each ($name; New collection("width"; "height"; "channels"; "colorSpace"; "format"; "sourceProfile"; "destinationProfile"; "warnings"))
	$payload:=JSON Parse(JSON Stringify($report.cases[1].value))
	$payload[$name]:=Null
	$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; JSON Stringify($payload); "stderr"; ""; "exitCode"; 0); "convert"))
End for each
For each ($name; New collection("colorSpace"; "description"; "bytes"; "origin"))
	$payload:=JSON Parse(JSON Stringify($report.cases[1].value))
	$payload.sourceProfile[$name]:=Null
	$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; JSON Stringify($payload); "stderr"; ""; "exitCode"; 0); "convert"))
End for each
For each ($name; New collection("colorSpace"; "description"; "bytes"))
	$payload:=JSON Parse(JSON Stringify($report.cases[1].value))
	$payload.destinationProfile[$name]:=Null
	$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; JSON Stringify($payload); "stderr"; ""; "exitCode"; 0); "convert"))
End for each
For each ($name; New collection("present"; "colorSpace"; "description"; "bytes"))
	$payload:=JSON Parse(JSON Stringify($report.cases[0].value))
	$payload.embeddedProfile[$name]:=Null
	$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; JSON Stringify($payload); "stderr"; ""; "exitCode"; 0); "inspect"))
End for each
$payload:=JSON Parse(JSON Stringify($report.cases[1].value))
$payload.warnings:=New collection(True)
$report.protocolFields.push($tool._decode(New object("ok"; True; "stdout"; JSON Stringify($payload); "stderr"; ""; "exitCode"; 0); "convert"))
HOST_Phase:="File argument"
$result:=cs.ImageCMYK._CLI.new("image-cmyk"; $target).execute(New collection("inspect"; "--source"; $source))
$report.fileArgument:=$result
If (Not($result.ok))
	$report.success:=False
End if
$report.rejectedArguments:=New collection
HOST_Phase:="scalar arguments"
For each ($invalid; New collection(123; 1.25; True; False; Null; Current date; Current time; New collection; New object))
	$result:=cs.ImageCMYK._Process.new().execute($requestFile; New collection($invalid))
	$report.rejectedArguments.push(New object("type"; Value type($invalid); "result"; $result))
	If ($result.ok | ($result.error.code#"invalid_argument"))
		$report.success:=False
	End if
End for each
$report.rejectedNul:=New collection
HOST_Phase:="NUL arguments"
For each ($name; New collection(Char(0)+"prefix"; "middle"+Char(0)+"suffix"; "trailing"+Char(0)))
	$result:=cs.ImageCMYK._Process.new().execute($requestFile; New collection($name))
	$report.rejectedNul.push($result)
	If ($result.ok | ($result.error.code#"invalid_argument"))
		$report.success:=False
	End if
End for each
$report.missingFile:=cs.ImageCMYK._Process.new().execute(Null; New collection)
$report.missingArguments:=cs.ImageCMYK._Process.new().execute($requestFile; Null)
If ($report.missingFile.ok | ($report.missingFile.error.code#"invalid_argument"))
	$report.success:=False
End if
If ($report.missingArguments.ok | ($report.missingArguments.error.code#"invalid_argument"))
	$report.success:=False
End if
HOST_Phase:="Folder argument"
$report.folderArgument:=cs.ImageCMYK._CLI.new("image-cmyk"; $target).execute(New collection("inspect"; "--source"; $fixtures))
HOST_Phase:="renamed executable File roundtrip"
$report.renamedInspect:=cs.ImageCMYK._Process.new().execute($renamedExecutable; New collection("inspect"; "--source"; $source))
If (Not($report.renamedInspect.ok))
	$report.success:=False
End if
$report.finished:=True
ON ERR CALL(HOST_PreviousHandler; ek local)
$statusFile.setText(JSON Stringify($report))
HOST_StatusFile:=Null
HOST_Phase:=""
HOST_PreviousHandler:=""
QUIT 4D
