param([string]$ProjectPath)
$ErrorActionPreference='Stop'
$launcher=Join-Path (Split-Path $PSScriptRoot -Parent) '启动_QGIS344.bat'
if ($ProjectPath) {
    $projectItem=Get-Item -LiteralPath $ProjectPath
    if ($projectItem.PSIsContainer -or $projectItem.Extension -notin @('.qgs','.qgz')) { throw 'Expected a QGIS project file.' }
    & $launcher $projectItem.FullName
} else {
    & $launcher
}
exit $LASTEXITCODE
