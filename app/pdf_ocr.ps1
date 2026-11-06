param([Parameter(Mandatory=$true)][string]$Document, [Parameter(Mandatory=$true)][string]$Pages)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument,Windows.Data.Pdf,ContentType=WindowsRuntime]
$null = [Windows.Data.Pdf.PdfPageRenderOptions,Windows.Data.Pdf,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
$asTasks = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1
}
$resultTask = $asTasks | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }
$actionTask = $asTasks | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' }
function Wait-Result($operation, $type) {
    $task = $resultTask.MakeGenericMethod($type).Invoke($null, @($operation))
    return $task.GetAwaiter().GetResult()
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (!$engine) { throw 'No OCR language pack installed. Install an OCR language in Windows language settings.' }
$file = Wait-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Document)) ([Windows.Storage.StorageFile])
$pdf = Wait-Result ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
foreach ($index in $Pages.Split(',')) {
    $page = $pdf.GetPage([uint32]$index)
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    $bitmap = $null
    try {
        $width = $page.Size.Width
        $height = $page.Size.Height
        $longSide = [Math]::Min(2400, [Windows.Media.Ocr.OcrEngine]::MaxImageDimension)
        $scale = [Math]::Min(3, $longSide / [Math]::Max($width, $height))
        $options = [Windows.Data.Pdf.PdfPageRenderOptions]::new()
        $options.DestinationWidth = [uint32][Math]::Max(1, $width * $scale)
        $options.DestinationHeight = [uint32][Math]::Max(1, $height * $scale)
        $render = $actionTask.Invoke($null, @($page.RenderToStreamAsync($stream, $options)))
        $render.Wait()
        $stream.Seek(0)
        $decoder = Wait-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Wait-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        [Console]::WriteLine((@{page = [int]$index; text = ($result.Lines.Text -join "`n")} | ConvertTo-Json -Compress))
    } finally {
        if ($bitmap) { $bitmap.Dispose() }
        $stream.Dispose()
        $page.Dispose()
    }
}
