param([Parameter(Mandatory=$true)][string]$Request,[Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference='Stop'
$r=Get-Content -LiteralPath $Request -Raw -Encoding UTF8|ConvertFrom-Json
$ids=@($r.ids|ForEach-Object {[int]$_})
if($ids.Count-eq 0){throw 'No selected soil IDs'}
$list=$ids -join ','
$connection=New-Object -ComObject ADODB.Connection
$connection.Mode=1
try {
 $connection.Open('Provider=Microsoft.Jet.OLEDB.4.0;Data Source='+$r.database+';Mode=Read;')
 $data=[ordered]@{}
 foreach($table in @('HWSD2_SMU','HWSD2_LAYERS','HWSD2_LAYERS_METADATA')) {
  $sql='SELECT * FROM ['+$table+']'
  if($table-ne 'HWSD2_LAYERS_METADATA'){$sql+=' WHERE HWSD2_SMU_ID IN ('+$list+')'}
  $records=$connection.Execute($sql);$rows=[Collections.Generic.List[object]]::new()
  while(-not $records.EOF){$row=[ordered]@{};foreach($f in $records.Fields){$row[$f.Name]=if($f.Value-is [DBNull]){$null}else{$f.Value}};$rows.Add($row);$records.MoveNext()}
  $records.Close();$data[$table]=@($rows.ToArray())
 }
 [IO.File]::WriteAllText($Output,($data|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
} finally {$connection.Close()}
