property _cli : cs._CLI

Class constructor($target : Text)
	This._cli:=cs._CLI.new("image-cmyk"; $target)

Function inspectFile($file : Variant)->$result : Object
	If (Not(This._isFile($file)))
		return This._invalid("Source must be a non-null 4D.File without NUL")
	End if
	$result:=This._decode(This._cli.execute(New collection("inspect"; "--source"; $file)); "inspect")

Function convertFile($source : Variant; $destination : Variant; $options : Variant)->$result : Object
	var $arguments : Collection
	var $intent; $format; $assume; $bpc : Text
	var $quality : Integer
	If (Not(This._isFile($source)))
		return This._invalid("Source must be a non-null 4D.File without NUL")
	End if
	If (Not(This._isFile($destination)))
		return This._invalid("Destination must be a non-null 4D.File without NUL")
	End if
	If (Value type($options)#Is object)
		return This._invalid("Options object with destinationProfile is required")
	End if
	If ($options=Null)
		return This._invalid("Options object with destinationProfile is required")
	End if
	If (Not(This._isFile($options.destinationProfile)))
		return This._invalid("destinationProfile must be a non-null 4D.File without NUL")
	End if
	$arguments:=New collection("convert"; "--source"; $source; "--destination"; $destination; "--destination-profile"; $options.destinationProfile)
	If (OB Is defined($options; "sourceProfile"))
		If (Not(This._isFile($options.sourceProfile)))
			return This._invalid("sourceProfile must be a non-null 4D.File without NUL")
		End if
		$arguments.push("--source-profile"; $options.sourceProfile)
	End if
	$assume:="false"
	If (OB Is defined($options; "allowAssumeSRGB"))
		If (Value type($options.allowAssumeSRGB)#Is Boolean)
			return This._invalid("allowAssumeSRGB must be Boolean")
		End if
		If ($options.allowAssumeSRGB)
			$assume:="true"
		End if
	End if
	$bpc:="true"
	If (OB Is defined($options; "blackPointCompensation"))
		If (Value type($options.blackPointCompensation)#Is Boolean)
			return This._invalid("blackPointCompensation must be Boolean")
		End if
		If (Not($options.blackPointCompensation))
			$bpc:="false"
		End if
	End if
	$intent:="relativeColorimetric"
	If (OB Is defined($options; "intent"))
		If (Value type($options.intent)#Is text)
			return This._invalid("intent must be a standard ICC rendering intent")
		End if
		$intent:=$options.intent
		If (New collection("relativeColorimetric"; "perceptual"; "saturation"; "absoluteColorimetric").indexOf($intent)<0)
			return This._invalid("intent must be a standard ICC rendering intent")
		End if
	End if
	$format:="tiff"
	If (OB Is defined($options; "format"))
		If (Value type($options.format)#Is text)
			return This._invalid("format must be tiff or jpeg")
		End if
		$format:=$options.format
		If (($format#"tiff") & ($format#"jpeg"))
			return This._invalid("format must be tiff or jpeg")
		End if
	End if
	$quality:=90
	If (OB Is defined($options; "quality"))
		If (Not(This._integer($options.quality; 1; 100)))
			return This._invalid("quality must be an integer from 1 to 100")
		End if
		$quality:=$options.quality
	End if
	$arguments.push("--allow-assume-srgb"; $assume; "--intent"; $intent; "--black-point-compensation"; $bpc; "--format"; $format; "--quality"; String($quality))
	$result:=This._decode(This._cli.execute($arguments); "convert")

Function version()->$result : Object
	$result:=This._decode(This._cli.execute(New collection("--version")); "version")

Function _invalid($message : Text)->$result : Object
	$result:=New object("ok"; False; "error"; New object("code"; "invalid_argument"; "message"; $message))

Function _isFile($value : Variant)->$valid : Boolean
	$valid:=False
	If (Value type($value)#Is object)
		return False
	End if
	If ($value=Null)
		return False
	End if
	If (Not(OB Instance of($value; 4D.File)))
		return False
	End if
	If (Position(Char(0); $value.platformPath; *)>0)
		return False
	End if
	$valid:=True

Function _integer($value : Variant; $minimum : Real; $maximum : Real)->$valid : Boolean
	$valid:=False
	If ((Value type($value)#Is real) & (Value type($value)#Is longint))
		return False
	End if
	If (($value<$minimum) | ($value>$maximum))
		return False
	End if
	$valid:=Int($value)=$value

Function _number($value : Variant)->$valid : Boolean
	$valid:=False
	If ((Value type($value)#Is real) & (Value type($value)#Is longint))
		return False
	End if
	$valid:=$value>=0

Function _profile($value : Variant)->$valid : Boolean
	$valid:=False
	If (Value type($value)#Is object)
		return False
	End if
	If ($value=Null)
		return False
	End if
	If (Value type($value.colorSpace)#Is text)
		return False
	End if
	If (New collection("RGB"; "CMYK").indexOf($value.colorSpace)<0)
		return False
	End if
	If (Value type($value.description)#Is text)
		return False
	End if
	If (Not(This._integer($value.bytes; 0; 33554432)))
		return False
	End if
	$valid:=True

Function _inspection($payload : Object)->$valid : Boolean
	$valid:=False
	If (Not(This._integer($payload.width; 1; 2147483647)))
		return False
	End if
	If (Not(This._integer($payload.height; 1; 2147483647)))
		return False
	End if
	If (Not(This._integer($payload.channels; 3; 4)))
		return False
	End if
	If (Not(This._integer($payload.bitDepth; 8; 8)))
		return False
	End if
	If (Value type($payload.colorSpace)#Is text)
		return False
	End if
	If (New collection("RGB"; "CMYK").indexOf($payload.colorSpace)<0)
		return False
	End if
	If (Value type($payload.alpha)#Is Boolean)
		return False
	End if
	If ($payload.alpha)
		return False
	End if
	If (($payload.colorSpace="RGB") & ($payload.channels#3))
		return False
	End if
	If (($payload.colorSpace="CMYK") & ($payload.channels#4))
		return False
	End if
	If (Value type($payload.format)#Is text)
		return False
	End if
	If (New collection("png"; "tiff"; "jpeg").indexOf($payload.format)<0)
		return False
	End if
	If (Not(This._integer($payload.orientation; 1; 1)))
		return False
	End if
	If (Not(This._number($payload.dpiX)))
		return False
	End if
	If (Not(This._number($payload.dpiY)))
		return False
	End if
	If (Value type($payload.embeddedProfile)#Is object)
		return False
	End if
	If ($payload.embeddedProfile=Null)
		return False
	End if
	If (Value type($payload.embeddedProfile.present)#Is Boolean)
		return False
	End if
	If ($payload.embeddedProfile.present)
		If (Not(This._profile($payload.embeddedProfile)))
			return False
		End if
		If (Not(This._integer($payload.embeddedProfile.bytes; 128; 33554432)))
			return False
		End if
		If ($payload.embeddedProfile.colorSpace#$payload.colorSpace)
			return False
		End if
	End if
	$valid:=True

Function _conversion($payload : Object)->$valid : Boolean
	var $warning : Variant
	$valid:=False
	If (Not(This._integer($payload.width; 1; 2147483647)))
		return False
	End if
	If (Not(This._integer($payload.height; 1; 2147483647)))
		return False
	End if
	If (Not(This._integer($payload.channels; 4; 4)))
		return False
	End if
	If (Value type($payload.colorSpace)#Is text)
		return False
	End if
	If ($payload.colorSpace#"CMYK")
		return False
	End if
	If (Value type($payload.format)#Is text)
		return False
	End if
	If (New collection("tiff"; "jpeg").indexOf($payload.format)<0)
		return False
	End if
	If (Not(This._profile($payload.sourceProfile)))
		return False
	End if
	If (Value type($payload.sourceProfile.origin)#Is text)
		return False
	End if
	If (New collection("embedded"; "supplied"; "assumed-srgb").indexOf($payload.sourceProfile.origin)<0)
		return False
	End if
	If (Not(This._profile($payload.destinationProfile)))
		return False
	End if
	If (Not(This._integer($payload.destinationProfile.bytes; 128; 33554432)))
		return False
	End if
	If ($payload.destinationProfile.colorSpace#"CMYK")
		return False
	End if
	If (Value type($payload.warnings)#Is collection)
		return False
	End if
	For each ($warning; $payload.warnings)
		If (Value type($warning)#Is text)
			return False
		End if
	End for each
	$valid:=True

Function _decode($result : Object; $operation : Text)->$decoded : Object
	var $payload : Variant
	var $previous : Text
	var $parseErrors : Collection
	var $valid : Boolean
	var CLI_LastErrors : Collection
	If (Not($result.ok))
		return $result
	End if
	$previous:=Method called on error(ek local)
	CLI_LastErrors:=Null
	ON ERR CALL("_CaptureError"; ek local)
	$payload:=JSON Parse($result.stdout)
	ON ERR CALL($previous; ek local)
	$parseErrors:=CLI_LastErrors
	CLI_LastErrors:=Null
	If (($parseErrors#Null) | (Value type($payload)#Is object))
		$result.ok:=False
		$result.error:=New object("code"; "invalid_protocol"; "message"; "CLI response must be a JSON object"; "details"; $parseErrors)
		return $result
	End if
	If ($payload=Null)
		$result.ok:=False
		$result.error:=New object("code"; "invalid_protocol"; "message"; "CLI response must be a JSON object")
		return $result
	End if
	If (Not(This._integer($payload.protocol; 1; 1)))
		$result.ok:=False
		$result.error:=New object("code"; "invalid_protocol"; "message"; "Unsupported CLI protocol version")
		return $result
	End if
	$valid:=False
	Case of
		: ($operation="version")
			If (Value type($payload.version)=Is text)
				$valid:=Length($payload.version)>0
			End if
		: ($operation="inspect")
			$valid:=This._inspection($payload)
		: ($operation="convert")
			$valid:=This._conversion($payload)
	End case
	If (Not($valid))
		$result.ok:=False
		$result.error:=New object("code"; "invalid_protocol"; "message"; "Invalid "+$operation+" response fields")
		return $result
	End if
	$result.value:=$payload
	$decoded:=$result
