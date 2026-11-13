<#
  生成"蓝色水波"图标 launcher.ico（16/24/32/48/64/128/256 全部 32 位 DIB 经典格式）与 icon_preview.png。
  由 build.ps1 在图标缺失时自动调用，也可手动重跑覆盖现有图标。
  注意：二进制数据全程不跨函数/管道传递，避免 PowerShell 对 byte[] 的拆包行为。
#>
$ErrorActionPreference = 'Stop'
$Dir = $PSScriptRoot
$IcoPath = Join-Path $Dir 'launcher.ico'
$PreviewPath = Join-Path $Dir 'icon_preview.png'

try {
    Add-Type -AssemblyName System.Drawing
} catch {
    # 个别环境下 pwsh 缺少 System.Drawing，退回 Windows PowerShell（.NET Framework）重跑本脚本
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath
    if ($LASTEXITCODE -ne 0) { throw }
    return
}

function New-RoundedRectPath([single]$x, [single]$y, [single]$w, [single]$h, [single]$r) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r * 2
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

function New-WavePoints([single]$size, [single]$baseY, [single]$amp, [single]$cycles, [single]$phase) {
    $pts = New-Object 'System.Collections.Generic.List[System.Drawing.PointF]'
    $n = 36
    for ($i = 0; $i -le $n; $i++) {
        $x = -3 + ($size + 6) * $i / $n
        $y = $baseY + $amp * [Math]::Sin(2 * [Math]::PI * $cycles * $i / $n + $phase)
        $pts.Add((New-Object System.Drawing.PointF -ArgumentList ([single]$x), ([single]$y)))
    }
    return $pts
}

function New-WaveBitmap([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $sc = $size / 256.0

    # 圆角矩形背景：上浅下深的蓝色渐变
    $bg = New-RoundedRectPath 2 2 ($size - 4) ($size - 4) ([single](52 * $sc))
    $rect = New-Object System.Drawing.RectangleF -ArgumentList 0, 0, ([single]$size), ([single]$size)
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush -ArgumentList $rect, `
        ([System.Drawing.Color]::FromArgb(255, 55, 180, 240)), `
        ([System.Drawing.Color]::FromArgb(255, 9, 72, 148)), 90.0
    $g.FillPath($brush, $bg)
    $g.SetClip($bg)

    # 远处浅浪（半透明白）
    $ptsA = New-WavePoints $size ([single]($size * 0.54)) ([single]($size * 0.055)) 1.15 0.6
    $ptsAF = [System.Drawing.PointF[]]$ptsA
    $pathA = New-Object System.Drawing.Drawing2D.GraphicsPath
    $pathA.AddCurve($ptsAF, 0.8)
    $pathA.AddLine($size + 6, $size * 0.54, $size + 6, $size + 6)
    $pathA.AddLine($size + 6, $size + 6, -3, $size + 6)
    $pathA.CloseFigure()
    $brushA = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(64, 255, 255, 255))
    $g.FillPath($brushA, $pathA)

    # 主浪（浅蓝白）+ 浪峰描线
    $ptsB = New-WavePoints $size ([single]($size * 0.67)) ([single]($size * 0.07)) 1.0 2.7
    $ptsBF = [System.Drawing.PointF[]]$ptsB
    $pathB = New-Object System.Drawing.Drawing2D.GraphicsPath
    $pathB.AddCurve($ptsBF, 0.8)
    $pathB.AddLine($size + 6, $size * 0.67, $size + 6, $size + 6)
    $pathB.AddLine($size + 6, $size + 6, -3, $size + 6)
    $pathB.CloseFigure()
    $brushB = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(165, 222, 244, 255))
    $g.FillPath($brushB, $pathB)

    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(235, 255, 255, 255), [single]([Math]::Max(1.2, 6.5 * $sc)))
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $g.DrawCurve($pen, $ptsBF, 0.8)

    $g.ResetClip()
    $penBorder = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(170, 7, 49, 96), [single]([Math]::Max(1.0, 4 * $sc)))
    $g.DrawPath($penBorder, $bg)

    $g.Dispose()
    return $bmp
}

# ===== 逐尺寸渲染并压入 32 位 DIB（BITMAPINFOHEADER + 自底向上 BGRA + 全零 AND 掩码）=====
$sizes = @(256, 128, 64, 48, 32, 24, 16)
$payloads = New-Object 'System.Collections.Generic.List[byte[]]'
foreach ($s in $sizes) {
    $bmp = New-WaveBitmap $s
    if ($s -eq 256) { $bmp.Save($PreviewPath, [System.Drawing.Imaging.ImageFormat]::Png) }

    $rect = New-Object System.Drawing.Rectangle(0, 0, $s, $s)
    $data = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $pix = New-Object byte[] ($data.Stride * $s)
    [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $pix, 0, $pix.Length)
    $bmp.UnlockBits($data)
    $bmp.Dispose()
    $maskStride = ((($s + 31) -shr 5) -shl 2)

    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter($ms)
    $bw.Write([uint32]40)                              # BITMAPINFOHEADER.biSize
    $bw.Write([int32]$s)                               # biWidth
    $bw.Write([int32]($s * 2))                         # biHeight（XOR+AND 两段）
    $bw.Write([uint16]1)                               # biPlanes
    $bw.Write([uint16]32)                              # biBitCount
    $bw.Write([uint32]0)                               # biCompression = BI_RGB
    $bw.Write([uint32]($pix.Length + $maskStride * $s))
    $bw.Write([int32]0); $bw.Write([int32]0); $bw.Write([uint32]0); $bw.Write([uint32]0)
    for ($y = $s - 1; $y -ge 0; $y--) { $bw.Write($pix, $y * $data.Stride, $s * 4) }   # 自底向上 BGRA
    $bw.Write((New-Object byte[] ($maskStride * $s)))  # AND 掩码（透明度由 alpha 通道表达）
    $bw.Flush()
    $payloads.Add($ms.ToArray())
}

# ===== 组装 ICO 容器 =====
$ms = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter($ms)
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$payloads.Count)
$offset = 6 + 16 * $payloads.Count
for ($i = 0; $i -lt $payloads.Count; $i++) {
    $dim = [byte]($(if ($sizes[$i] -ge 256) { 0 } else { $sizes[$i] }))
    $bw.Write($dim); $bw.Write($dim); $bw.Write([byte]0); $bw.Write([byte]0)
    $bw.Write([uint16]1); $bw.Write([uint16]32)
    $bw.Write([uint32]$payloads[$i].Length); $bw.Write([uint32]$offset)
    $offset += $payloads[$i].Length
}
for ($i = 0; $i -lt $payloads.Count; $i++) { $bw.Write($payloads[$i]) }
$bw.Flush()
[System.IO.File]::WriteAllBytes($IcoPath, $ms.ToArray())
Write-Host ("图标已生成: {0}（{1} 个尺寸，{2:N0} 字节）预览: {3}" -f $IcoPath, $payloads.Count, (Get-Item $IcoPath).Length, $PreviewPath)
